"""Quality gate déterministe : installe les dépendances d'un clone frais et
exécute la suite de tests du repo cible.

La commande de test est auto-détectée selon la stack (override possible par
input du workflow ou par env `pipeline_test_cmd`) :
- package.json à la racine (front Angular toollive-c8y, Jest) :
  `npm run test-unit -- --ci --silent` (fallback `npm test -- --watch=false`)
- pyproject.toml à la racine (repo Python) : `uv run pytest -x -q`
- sinon : `pytest -x -q`
"""

from __future__ import annotations

import asyncio
import os
import shlex

import mistralai.workflows as workflows
from datetime import timedelta

TEST_CMD = ["pytest", "-x", "-q"]       # fallback si la stack n'est pas détectée
TEST_TIMEOUT_SECONDS = 1800             # la suite Jest d'un gros front peut être longue
DEP_TIMEOUT_SECONDS = 1800              # npm ci / uv sync sur un clone frais
TEST_ACTIVITY_TIMEOUT = timedelta(minutes=40)


def test_cmd_for(worktree: str, override: str = "") -> list[str]:
    """Commande de test : override explicite (input workflow, puis env
    `pipeline_test_cmd`), sinon auto-détection selon la stack du repo cible."""
    explicit = override or _env("pipeline_test_cmd", "PIPELINE_TEST_CMD")
    if explicit:
        return shlex.split(explicit)
    if os.path.isfile(os.path.join(worktree, "package.json")):
        return ["npm", "run", "test-unit", "--", "--ci", "--silent"]
    if os.path.isfile(os.path.join(worktree, "pyproject.toml")):
        return ["uv", "run", "pytest", "-x", "-q"]
    return TEST_CMD


def _env(*names: str) -> str | None:
    for n in names:
        val = os.environ.get(n)
        if val:
            return val
    return None


async def _run_logged(cmd: list[str], cwd: str, timeout: int) -> dict:
    if os.name == "nt" and cmd[0] in ("npm", "npx", "ng", "uv"):
        cmd = ["cmd", "/c", *cmd]      # npm/ng/uv sont des .cmd sous Windows
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return {"passed": proc.returncode == 0, "cmd": " ".join(cmd),
            "log": out.decode(errors="replace")}


async def ensure_deps(worktree: str) -> dict:
    """Installe les dépendances d'un clone frais (le worker clone à vide) :
    - package.json sans node_modules -> `npm ci` (fallback `npm install`)
    - pyproject.toml sans .venv      -> `uv sync`
    Helper interne (appelé par run_tests), pas une activité séparée.
    """
    logs = []
    if (os.path.isfile(os.path.join(worktree, "package.json"))
            and not os.path.isdir(os.path.join(worktree, "node_modules"))):
        res = await _run_logged(["npm", "ci", "--no-audit", "--no-fund"],
                                worktree, DEP_TIMEOUT_SECONDS)
        if not res["passed"]:    # lockfile absent ou incompatible -> install
            res2 = await _run_logged(["npm", "install", "--no-audit", "--no-fund"],
                                     worktree, DEP_TIMEOUT_SECONDS)
            res = res2 if res2["passed"] else res
        logs.append(res["log"][-2000:])
    if (os.path.isfile(os.path.join(worktree, "pyproject.toml"))
            and not os.path.isdir(os.path.join(worktree, ".venv"))):
        res = await _run_logged(["uv", "sync"], worktree, DEP_TIMEOUT_SECONDS)
        logs.append(res["log"][-2000:])
    return {"log": "\n".join(logs)}


@workflows.activity(start_to_close_timeout=TEST_ACTIVITY_TIMEOUT)
async def run_tests(worktree: str, test_cmd_override: str = "") -> dict:
    """Gate de tests : échec -> `passed: false` (pas une exception, la boucle
    dev/tests du workflow décide)."""
    deps = await ensure_deps(worktree)
    result = await _run_logged(test_cmd_for(worktree, test_cmd_override), worktree,
                               TEST_TIMEOUT_SECONDS)
    result["log"] = (deps["log"] + "\n" + result["log"]) if deps["log"] else result["log"]
    return result
