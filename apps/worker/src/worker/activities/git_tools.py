"""Activities Git : clone du repo cible (URL GitLab/GitHub ou chemin local),
branche de change, commits, push et Merge Request GitLab.

Chaque activité est durable : retries configurés par le décorateur, état
repris au dernier pas complété en cas de crash du worker.

Auth forge — 100% PAT (`gitlab_token` / `pipeline_git_token`), pas de clé SSH :
- Opérations git (ls-remote/clone/fetch/push) : les URL SSH (`git@host:...`)
  sont converties en HTTPS (`normalize_repo_url`) et le PAT est embarqué dans
  l'URL passée à la commande git : `https://<username>:<PAT>@host/<ns>/<proj>.git`.
  L'URL authentifiée n'est JAMAIS logguée ni persistée : argument transitoire
  des commandes réseau uniquement, le remote `origin` gardant l'URL nettoyée,
  et toute sortie d'erreur est masquée (`_scrub`).
- API REST GitLab (Merge Request) : même PAT en `Authorization: Bearer`.
- Fallback si le secret n'est pas bindé au déploiement : le PAT peut être
  embarqué dans l'URL du repo passée à l'exécution
  (`https://<username>:<PAT>@gitlab.../....git`) — il est extrait
  (`extract_url_token`), jamais loggué, et réinjecté uniquement via
  `_authed_url` au moment des commandes réseau.
Dans tous les cas : GIT_TERMINAL_PROMPT=0 (pas de prompt interactif).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import mistralai.workflows as workflows

DIFF_MAX_CHARS = 20000


async def _sh(cmd: list[str], cwd: str, extra_env: dict | None = None) -> dict:
    """Exécute une commande, renvoie un résultat JSON-sérialisable."""
    env = {**os.environ, **(extra_env or {})}
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
    return {"code": proc.returncode, "output": out.decode(errors="replace")}


def _env(*names: str) -> str | None:
    """Première variable d'env non vide parmi `names` — convention minuscule,
    avec fallback sur le nom en majuscules (bindings de déploiement)."""
    for n in names:
        val = os.environ.get(n)
        if val:
            return val
    return None


_URL_TOKEN: str | None = None  # PAT extrait d'une URL `https://user:pass@...` en entrée


def extract_url_token(repo: str) -> tuple[str, str | None]:
    """Si l'URL embarque des credentials (`https://user:pass@host/...`), renvoie
    (URL nettoyée, pass). Sinon (repo, None). Le token n'est jamais loggué."""
    m = re.match(r"^(https?://)[^@/\s]+:([^@/\s]+)@(.+)$", repo)
    if m:
        return f"{m.group(1)}{m.group(3)}", m.group(2)
    return repo, None


_TOKEN_ENV_NAMES = ("gitlab_token", "pipeline_git_token")


def _token_from_env() -> tuple[str | None, str | None]:
    """(PAT, nom de la variable d'env source) — recherche insensible à la casse
    (un déploiement peut binder le secret sous `GITLAB_TOKEN`). Le token est
    nettoyé (strip) ; la valeur n'est jamais logguée, seul le nom de la variable."""
    for key, val in os.environ.items():
        if key.lower() in _TOKEN_ENV_NAMES and val and val.strip():
            return val.strip(), key
    return None, None


def git_token() -> str | None:
    """PAT du pipeline : secret env `gitlab_token` (toute casse tolérée), avec
    fallback sur le PAT embarqué dans l'URL du repo (voir extract_url_token)."""
    token, _src = _token_from_env()
    token = token or _URL_TOKEN
    return token.strip() if token else token


def git_token_source() -> str | None:
    """Nom de la variable d'env ayant fourni le PAT (diagnostic uniquement)."""
    _tok, src = _token_from_env()
    return src


def _gitlab_username() -> str:
    """Username de l'URL authentifiée : env `gitlab_username`/`GITLAB_USERNAME`,
    défaut `oauth2` (GitLab accepte tout username non vide avec un PAT)."""
    return _env("gitlab_username", "GITLAB_USERNAME") or "oauth2"


def _authed_url(repo: str) -> str:
    """URL git avec credentials embarqués. Sert UNIQUEMENT d'argument aux
    commandes git réseau (jamais logguée, jamais persistée dans .git/config)."""
    token = git_token()
    m = re.match(r"^(https?://)([^/]+)(/.*)$", repo)
    if not token or not m:
        return repo
    user = urllib.parse.quote(_gitlab_username(), safe="")
    tok = urllib.parse.quote(token, safe="")
    return f"{m.group(1)}{user}:{tok}@{m.group(2)}{m.group(3)}"


def _scrub(text: str) -> str:
    """Masque le PAT dans tout texte destiné à une erreur / un log."""
    token = git_token()
    return text.replace(token, "****") if token else text


def _git_env() -> dict:
    return {"GIT_TERMINAL_PROMPT": "0"}


def is_url(repo: str) -> bool:
    return bool(re.match(r"^(?:https?|ssh)://|^[^@/\s]+@[^:/\s]+:", repo))


def normalize_repo_url(repo: str) -> str:
    """Convertit une URL git SSH en HTTPS (`git@host:group/proj.git` /
    `ssh://git@host/group/proj.git` -> `https://host/group/proj.git`)."""
    if repo.startswith(("http://", "https://")):
        return repo
    m = re.match(r"^(?:ssh://)?[^@/\s]+@([^:/\s]+)(?::\d+)?[/:](.+)$", repo)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return repo


def _parse_repo_url(repo: str) -> tuple[str, str]:
    """(host, path) depuis une URL git quelconque (https, ssh, scp-like)."""
    m = re.match(r"^https?://(?:[^@/\s]+@)?([^/]+)/(.+?)(?:\.git)?/?$", repo)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^(?:ssh://[^@/\s]+@|[^@/\s]+@)([^:/\s]+)(?::\d+)?[/:](.+?)(?:\.git)?/?$", repo)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"URL git non reconnue: {repo}")


async def detect_base_branch(workdir: str, override: str = "") -> str:
    """Branche de base : override (input ou env `pipeline_base_branch`), sinon la
    première de (develop, main, master) qui existe — convention toollive :
    `develop`. Dernier recours : HEAD."""
    env = override or _env("pipeline_base_branch", "PIPELINE_BASE_BRANCH")
    if env:
        return env
    for candidate in ("develop", "main", "master"):
        res = await _sh(["git", "rev-parse", "--verify", "--quiet", candidate], workdir)
        if res["code"] == 0:
            return candidate
    return "HEAD"


async def _probe_remote_auth(repo: str) -> None:
    """Sonde `git ls-remote` avec les mêmes credentials que le clone : échoue
    vite et précisément si aucun PAT n'est configuré ou si le forge le refuse."""
    res = await _sh(["git", "ls-remote", _authed_url(repo), "HEAD"],
                    os.getcwd(), extra_env=_git_env())
    if res["code"] == 0:
        return
    out = _scrub(res["output"])
    if not git_token():
        raise RuntimeError(
            f"auth git vers {repo} impossible : aucun PAT configuré — binder le secret "
            f"`gitlab_token` au déploiement (env `gitlab_token`/`GITLAB_TOKEN`) ou passer "
            f"le PAT dans l'URL du repo (https://<username>:<PAT>@host/...git) : {out}")
    raise RuntimeError(
        f"PAT refusé par le forge pour {repo} (lu depuis la variable "
        f"`{git_token_source()}`) — vérifier le secret `gitlab_token` (expiré/révoqué ?) "
        f"et ses scopes read_repository/write_repository/api : {out}")


@workflows.activity()
async def ensure_workspace(repo: str) -> dict:
    """Si `repo` est une URL (GitLab/GitHub), clone le repo dans un workspace
    local (`pipeline_workspace`, défaut /tmp/pipeline-workspaces) — ou le met à
    jour si déjà cloné — et retourne le chemin + la branche de base. Si c'est un
    chemin local, le retourne tel quel (mode dev/test)."""
    global _URL_TOKEN
    if not is_url(repo):
        return {"path": repo, "url": None,
                "base": await detect_base_branch(repo),
                "token_present": bool(git_token())}
    repo, url_token = extract_url_token(repo)  # PAT embarqué dans l'URL (jamais loggué)
    if url_token:
        _URL_TOKEN = url_token
    repo = normalize_repo_url(repo)   # git@... -> https://... (auth PAT, pas SSH)
    await _probe_remote_auth(repo)
    name = repo.rstrip("/").split("/")[-1].removesuffix(".git")
    digest = hashlib.md5(repo.encode()).hexdigest()[:8]
    root = _env("pipeline_workspace", "PIPELINE_WORKSPACE") \
        or os.path.join(tempfile.gettempdir(), "pipeline-workspaces")
    dest = os.path.join(root, f"{name}-{digest}")
    if os.path.isdir(os.path.join(dest, ".git")):
        await _sh(["git", "fetch", _authed_url(repo), "+refs/heads/*:refs/remotes/origin/*"],
                  dest, extra_env=_git_env())
        base = await detect_base_branch(dest)
        await _sh(["git", "checkout", "-f", base], dest)
        await _sh(["git", "reset", "--hard", f"origin/{base}"], dest)
    else:
        os.makedirs(root, exist_ok=True)
        res = await _sh(["git", "clone", _authed_url(repo), dest], root, extra_env=_git_env())
        if res["code"] != 0:
            out = _scrub(res["output"])
            hints = []
            if "could not read Username" in out or "Authentication failed" in out \
                    or "HTTP Basic: Access denied" in out:
                src = git_token_source()
                if src:
                    hints.append(f"PAT refusé par le forge (secret lu depuis `{src}`) — "
                                 f"vérifier la valeur du secret `gitlab_token` et ses scopes")
                else:
                    hints.append("aucun PAT dans l'env du worker — binder le secret "
                                 "`gitlab_token` au déploiement, ou passer le PAT dans l'URL "
                                 "du repo : https://<username>:<PAT>@host/...git")
            raise RuntimeError(f"clone failed: {out}"
                               + (f" — {' | '.join(hints)}" if hints else ""))
        await _sh(["git", "remote", "set-url", "origin", repo], dest)
        await _sh(["git", "checkout", "-f", await detect_base_branch(dest)], dest)
    return {"path": dest, "url": repo,
            "base": await detect_base_branch(dest),
            "token_present": bool(git_token())}


@workflows.activity()
async def create_branch(workspace: str, branch: str, base: str) -> dict:
    """Crée (ou recrée, idempotent) la branche de change à partir de `base`."""
    res = await _sh(["git", "checkout", "-B", branch, base], workspace)
    if res["code"] != 0:
        raise RuntimeError(f"checkout -B {branch} failed: {_scrub(res['output'])}")
    return res


@workflows.activity()
async def commit_all(workspace: str, message: str) -> dict:
    """Commit tout le diff courant (tolère un arbre propre : 'nothing to commit')."""
    res = await _sh(
        ["bash", "-lc", "git add -A && (git commit -m \"$1\" || echo 'nothing to commit')", "_", message],
        workspace)
    return res


@workflows.activity()
async def push_branch(workspace: str, branch: str) -> dict:
    """Push la branche vers le forge : URL authentifiée passée en argument
    (jamais persistée ni logguée — le remote `origin` reste l'URL nettoyée)."""
    url = (await _sh(["git", "config", "remote.origin.url"], workspace))["output"].strip()
    res = await _sh(["git", "push", _authed_url(url), f"HEAD:refs/heads/{branch}"],
                    workspace, extra_env=_git_env())
    if res["code"] != 0:
        raise RuntimeError(f"push {branch} failed: {_scrub(res['output'])}")
    return {"branch": branch}


@workflows.activity()
async def diff_against_base(workspace: str, base: str) -> dict:
    """Diff de la branche courante contre la branche de base (tronqué)."""
    res = await _sh(["git", "diff", f"{base}...HEAD", "--stat"], workspace)
    stat = res["output"]
    res = await _sh(["git", "diff", f"{base}...HEAD"], workspace)
    return {"base": base, "stat": stat[:4000],
            "diff": res["output"][:DIFF_MAX_CHARS]}


@workflows.activity()
async def repo_tree(workspace: str, max_entries: int = 400) -> dict:
    """Liste des fichiers suivis par git (pour donner au planner une vue du repo)."""
    res = await _sh(["git", "ls-files"], workspace)
    files = res["output"].splitlines()
    return {"files": files[:max_entries], "truncated": len(files) > max_entries}


# ------------------------------------------------------------- API GitLab ---

def _gitlab_api(repo_url: str) -> tuple[str, str]:
    """(api_base, project encodé) depuis une URL git quelconque :
    https://gitlab.com/<group>/<projet>.git -> https://gitlab.com/api/v4, <group>%2F<projet>"""
    host, path = _parse_repo_url(repo_url)
    return f"https://{host}/api/v4", urllib.parse.quote(path, safe="")


async def _gitlab_request(method: str, url: str, body: dict | None = None) -> dict:
    token = git_token()
    if not token:
        return {"error": "GITLAB_TOKEN non défini"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with await asyncio.to_thread(urllib.request.urlopen, req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}"}
    except urllib.error.URLError as exc:
        return {"error": f"URLError: {exc.reason}"}


@workflows.activity()
async def open_merge_request(repo_url: str, source_branch: str, target_branch: str,
                             title: str, description: str = "") -> dict:
    """Ouvre une MR sur GitLab — la validation humaine se fait dans la review MR."""
    if not is_url(repo_url):
        return {"mr": None, "reason": "repo local (pas d'URL)"}
    api, project = _gitlab_api(repo_url)
    mr = await _gitlab_request(
        "POST", f"{api}/projects/{project}/merge_requests",
        {"source_branch": source_branch, "target_branch": target_branch,
         "title": title, "description": description, "remove_source_branch": True},
    )
    return {"mr": mr if isinstance(mr, dict) and "iid" in mr else None, "raw": mr}
