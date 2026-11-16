"""Workflow `change-to-mr` — évolution/correction d'un repo front depuis une
demande développeur, jusqu'à la Merge Request.

Déroulement :
    1. SPEC       — l'entrée `request` est la demande du développeur (ce qu'il
                    veut faire, en langage naturel).
    2. PLAN       — agent planner : analyse le repo cloné et produit un plan.
    3. BOUCLE     — itérations bornées entre agents : dev (implémente/corrige)
                    -> tests (gate déterministe) -> reviewer (verdict JSON).
                    La boucle s'arrête quand les tests passent ET que le
                    reviewer approuve, ou à `max_iterations`.
    4. MR         — push de la branche `feat/...` et ouverture d'une Merge
                    Request GitLab pour validation humaine (jamais de merge
                    automatique).

Le repo cible par défaut est le front toollive-c8y sur le GitLab toollive.
`dry_run=true` exécute tout sauf le push et la MR (vérification de bout en bout
sans effet de bord sur le forge).

Toute la logique non déterministe (LLM, git réseau, tests) est dans des
activités ; le workflow ne fait qu'orchestrer (déterministe, rejouable).
"""

import json
import re
from datetime import timedelta

from pydantic import BaseModel, Field

import mistralai.workflows as workflows
from mistralai.workflows import workflow as wf

from worker.agents.definitions import MAX_DEV_ITERATIONS

with wf.unsafe.imports_passed_through():
    from worker.activities import agent_runner, git_tools, test_runner

DEFAULT_REPO = ("https://gitlab.toollive.bearstech.com/"
                "toollive-team/toollive-business/toollive-c8y.git")

EXECUTION_TIMEOUT = timedelta(hours=8)
TEST_LOG_TAIL = 3000
REPORT_DIFF_MAX = 6000


class ChangeRequest(BaseModel):
    """Entrée du workflow — étape 1 : le développeur explique ce qu'il veut."""
    request: str = Field(description="Demande du développeur, en langage naturel "
                                     "(évolution ou correction à apporter au repo cible).")
    repo: str = Field(default=DEFAULT_REPO,
                      description="URL git du repo cible (GitLab toollive-c8y par défaut) "
                                  "ou chemin local (mode dev).")
    base_branch: str = Field(default="", description="Branche de base de la MR "
                              "(défaut : auto-détection develop/main/master).")
    test_cmd: str = Field(default="", description="Commande de test override "
                           "(défaut : auto-détection selon la stack du repo).")
    max_iterations: int = Field(default=MAX_DEV_ITERATIONS, ge=1, le=20,
                                description="Borne de la boucle dev -> tests -> review.")
    dry_run: bool = Field(default=False,
                          description="Si true : tout sauf push + MR (pas d'effet de bord "
                                      "sur le forge).")


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "change"


def _extract_json(text: str) -> dict:
    """Extrait le JSON d'une réponse d'agent (prose ou blocs ```json``` autour)."""
    text = (text or "").strip()
    if not text:
        raise RuntimeError("réponse de l'agent vide (MAX_AGENT_TURNS atteint ?)")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise RuntimeError(f"aucun JSON dans la réponse de l'agent: {text[:300]!r}")


@workflows.workflow.define(
    name="change-to-mr",
    workflow_display_name="Change request → Dev/Test loop → MR",
    workflow_description="Demande développeur -> plan -> itérations dev/tests entre "
                         "agents (bornées) -> Merge Request GitLab pour validation humaine.",
    execution_timeout=EXECUTION_TIMEOUT,
)
class ChangeToMr:

    def __init__(self) -> None:
        self.phase: str = "init"
        self.iteration: int = 0
        self.mr_url: str | None = None

    @workflows.workflow.query(name="state")
    def get_state(self) -> dict:
        """Lecture seule de la progression — interrogeable à tout moment."""
        return {"phase": self.phase, "iteration": self.iteration, "mr_url": self.mr_url}

    @workflows.workflow.entrypoint
    async def run(self, input: ChangeRequest) -> dict:
        # ------------------------------------------------------------ 0. workspace
        self.phase = "workspace"
        ws = await git_tools.ensure_workspace(input.repo)
        workspace, repo_url = ws["path"], ws["url"]
        base = input.base_branch or ws["base"]
        execution_id = workflows.get_execution_id()
        branch = f"feat/{_slugify(input.request)}-{execution_id[:8]}"
        await git_tools.create_branch(workspace, branch, base)

        # ------------------------------------------------------------ 1. plan
        self.phase = "plan"
        tree = await git_tools.repo_tree(workspace)
        tree_txt = "\n".join(tree["files"])
        if tree["truncated"]:
            tree_txt += "\n... (tronqué)"
        plan = await agent_runner.run_agent_activity(
            "planner",
            f"Change request from the developer:\n{input.request}\n\n"
            f"Repository file tree:\n{tree_txt}\n\n"
            f"Base branch: {base}\n"
            "Produce the JSON plan now (see your instructions).",
            workspace,
        )

        # ------------------------------------------------------------ 2. boucle dev/tests/review
        self.phase = "dev-loop"
        context = (f"Change request:\n{input.request}\n\nPlan:\n{plan}\n\n"
                   f"Base branch: {base}. You are on branch {branch}. "
                   "Implement the change now.")
        reports: list[dict] = []
        status = "max_iterations_reached"
        for i in range(1, input.max_iterations + 1):
            self.iteration = i

            self.phase = f"dev:{i}"
            await agent_runner.run_agent_activity("dev", context, workspace)
            await git_tools.commit_all(workspace, f"feat: {input.request[:80]} (iteration {i})")

            self.phase = f"test:{i}"
            tests = await test_runner.run_tests(workspace, input.test_cmd)

            self.phase = f"review:{i}"
            diff = await git_tools.diff_against_base(workspace, base)
            review_raw = await agent_runner.run_agent_activity(
                "reviewer",
                f"Change request:\n{input.request}\n\nPlan:\n{plan}\n\n"
                f"Current diff vs {base} (truncated):\n{diff['diff'][:REPORT_DIFF_MAX]}\n\n"
                f"Test run (cmd: {tests.get('cmd')}):\n"
                f"passed={tests['passed']}\n{tests['log'][-TEST_LOG_TAIL:]}\n\n"
                "Return the JSON verdict now (see your instructions).",
                workspace,
            )
            try:
                verdict = _extract_json(review_raw)
            except (RuntimeError, json.JSONDecodeError) as exc:
                verdict = {"approve": False,
                           "findings": [f"verdict illisible ({exc}); réponse: {review_raw[:300]!r}"]}

            report = {
                "iteration": i,
                "tests_passed": tests["passed"],
                "verdict": verdict,
                "test_log_tail": tests["log"][-TEST_LOG_TAIL:],
            }
            reports.append(report)

            if tests["passed"] and verdict.get("approve") is True:
                status = "ready_for_review"
                break

            findings = verdict.get("findings") or []
            context += (
                f"\n\nITERATION {i} FEEDBACK — tests_passed={tests['passed']}, "
                f"review findings: {json.dumps(findings, ensure_ascii=False)[:2000]}\n"
                f"Test log tail:\n{tests['log'][-TEST_LOG_TAIL:]}\n"
                "Fix the findings and make the tests pass."
            )

        # ------------------------------------------------------------ 3. MR
        self.phase = "mr"
        final_tests = await test_runner.run_tests(workspace, input.test_cmd)
        diff = await git_tools.diff_against_base(workspace, base)

        mr_url = None
        mr_iid = None
        mr_note = ""
        if input.dry_run:
            mr_note = "dry_run=true : push et MR ignorés"
        elif not repo_url:
            mr_note = "repo local (pas d'URL) : pas de push ni de MR"
        else:
            await git_tools.push_branch(workspace, branch)
            title = f"[pipeline] {input.request[:100]}"
            description = self._mr_description(input, base, branch, plan, reports,
                                                final_tests, diff)
            mr = await git_tools.open_merge_request(repo_url, branch, base, title, description)
            if mr.get("mr"):
                mr_url = mr["mr"].get("web_url")
                mr_iid = mr["mr"].get("iid")
                self.mr_url = mr_url
            else:
                mr_note = f"échec ouverture MR: {str(mr.get('raw'))[:500]}"

        self.phase = "done"
        return {
            "status": status,
            "branch": branch,
            "base": base,
            "repo": repo_url or workspace,
            "mr_url": mr_url,
            "mr_iid": mr_iid,
            "mr_note": mr_note,
            "dry_run": input.dry_run,
            "plan": plan[:4000],
            "iterations": reports,
            "final_tests": {"passed": final_tests["passed"],
                            "cmd": final_tests.get("cmd"),
                            "log_tail": final_tests["log"][-TEST_LOG_TAIL:]},
        }

    @staticmethod
    def _mr_description(input: ChangeRequest, base: str, branch: str, plan: str,
                        reports: list[dict], final_tests: dict, diff: dict) -> str:
        lines = [
            "## Demande initiale (développeur)",
            input.request,
            "",
            "## Plan (agent planner)",
            plan[:2000],
            "",
            f"## Itérations dev/tests ({len(reports)})",
        ]
        for r in reports:
            v = r["verdict"] if isinstance(r["verdict"], dict) else {}
            lines.append(f"- itération {r['iteration']}: tests_passed={r['tests_passed']}, "
                         f"approve={v.get('approve')}, findings={v.get('findings')}")
        lines += [
            "",
            "## Tests finaux",
            f"commande: `{final_tests.get('cmd')}` — passed={final_tests['passed']}",
            "```",
            final_tests["log"][-2000:],
            "```",
            "",
            f"## Diff vs `{base}` (stat)",
            "```",
            diff["stat"],
            "```",
            "",
            "Généré automatiquement par le workflow Mistral `change-to-mr` "
            "(boucle dev/tests entre agents). Validation humaine requise avant merge.",
        ]
        return "\n".join(lines)[:8000]
