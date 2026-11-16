"""Tests e2e du workflow `change-to-mr` sur Temporal time-skipping.

Les effets externes sont mockés au niveau des modules d'activités :
- `agent_runner.run_agent` (appels LLM) -> réponses scénarisées,
- `test_runner.ensure_deps` / `test_runner._run_logged` -> gate de tests fictive.
Les activités git tournent VRAIMENT contre un repo git local temporaire
(chemin local = pas de réseau, pas de push ni MR en dry_run).
"""

import json
import subprocess

import pytest

from mistralai.workflows.testing.test_worker import create_test_worker

from worker.activities import agent_runner, git_tools, test_runner
from worker.workflows.change_to_mr import ChangeRequest, ChangeToMr

ACTIVITIES = [
    git_tools.ensure_workspace,
    git_tools.create_branch,
    git_tools.commit_all,
    git_tools.push_branch,
    git_tools.diff_against_base,
    git_tools.repo_tree,
    git_tools.open_merge_request,
    test_runner.run_tests,
    agent_runner.run_agent_activity,
]

PLAN_JSON = json.dumps({"summary": "ajouter un test", "files": ["a.py"],
                        "approach": ["step1"], "verification": "pytest"})


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def make_repo(tmp_path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(str(repo), "init", "-b", "develop")
    (repo / "README.md").write_text("# demo\n")
    _git(str(repo), "add", "-A")
    _git(str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    return str(repo)


def mock_agents(monkeypatch, reviewer_verdicts: list[bool], tests_pass: bool = True):
    """Remplace la boucle LLM par des réponses scénarisées (par nom d'agent)."""
    verdicts = list(reviewer_verdicts)

    async def fake_run_agent(agent, user_message, tool_impls):
        if agent.name == "planner-agent":
            return PLAN_JSON
        if agent.name == "dev-agent":
            return "implémentation terminée"
        if agent.name == "reviewer-agent":
            approve = verdicts.pop(0) if verdicts else True
            return json.dumps({"approve": approve,
                               "findings": [] if approve else ["corriger X"]})
        raise AssertionError(f"agent inattendu: {agent.name}")

    async def fake_ensure_deps(worktree):
        return {"log": ""}

    async def fake_run_logged(cmd, cwd, timeout):
        return {"passed": tests_pass, "cmd": " ".join(cmd), "log": "OK" if tests_pass else "FAIL"}

    monkeypatch.setattr(agent_runner, "run_agent", fake_run_agent)
    monkeypatch.setattr(test_runner, "ensure_deps", fake_ensure_deps)
    monkeypatch.setattr(test_runner, "_run_logged", fake_run_logged)


async def run_workflow(env, request: ChangeRequest, exec_id: str) -> dict:
    async with create_test_worker(env, workflows=[ChangeToMr],
                                  activities=ACTIVITIES) as _worker:
        # execute_workflow attend la fin et retourne {"result": <retour du workflow>}
        raw = await env.client.execute_workflow(
            "change-to-mr", request, id=exec_id, task_queue="test-task-queue")
        return raw.get("result", raw) if isinstance(raw, dict) else raw


async def test_dry_run_converge_en_une_iteration(temporal_env_with_converter, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    mock_agents(monkeypatch, reviewer_verdicts=[True])

    result = await run_workflow(
        temporal_env_with_converter,
        ChangeRequest(request="ajouter un test unitaire", repo=repo, dry_run=True),
        "exec-dry-1",
    )

    assert result["status"] == "ready_for_review"
    assert result["dry_run"] is True
    assert result["mr_url"] is None                      # dry_run : pas de MR
    assert result["base"] == "develop"
    assert result["branch"].startswith("feat/")
    assert len(result["iterations"]) == 1
    assert result["iterations"][0]["tests_passed"] is True
    assert result["iterations"][0]["verdict"]["approve"] is True
    assert result["final_tests"]["passed"] is True
    # la branche de change existe vraiment dans le repo local
    assert "feat/" in _git(repo, "branch", "--list")


async def test_boucle_jusqu_a_approbation(temporal_env_with_converter, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    mock_agents(monkeypatch, reviewer_verdicts=[False, False, True])

    result = await run_workflow(
        temporal_env_with_converter,
        ChangeRequest(request="corriger le tri", repo=repo, max_iterations=5, dry_run=True),
        "exec-loop-1",
    )

    assert result["status"] == "ready_for_review"
    assert len(result["iterations"]) == 3
    assert [r["verdict"]["approve"] for r in result["iterations"]] == [False, False, True]


async def test_sans_plafond_illimite_par_defaut(temporal_env_with_converter, tmp_path, monkeypatch):
    """max_iterations=None (défaut) : la boucle continue au-delà de toute borne
    jusqu'à convergence (ici 3 itérations avant approve)."""
    repo = make_repo(tmp_path)
    mock_agents(monkeypatch, reviewer_verdicts=[False, False, True])

    result = await run_workflow(
        temporal_env_with_converter,
        ChangeRequest(request="évolution longue", repo=repo, dry_run=True),  # max_iterations=None
        "exec-unbounded-1",
    )

    assert result["status"] == "ready_for_review"
    assert len(result["iterations"]) == 3


async def test_max_iterations_atteint(temporal_env_with_converter, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    mock_agents(monkeypatch, reviewer_verdicts=[False], tests_pass=False)

    result = await run_workflow(
        temporal_env_with_converter,
        ChangeRequest(request="grosse évolution", repo=repo, max_iterations=2, dry_run=True),
        "exec-max-1",
    )

    assert result["status"] == "max_iterations_reached"
    assert len(result["iterations"]) == 2
    assert result["mr_url"] is None


async def test_query_state_expose_la_phase(temporal_env_with_converter, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    mock_agents(monkeypatch, reviewer_verdicts=[True])
    async with create_test_worker(temporal_env_with_converter, workflows=[ChangeToMr],
                                  activities=ACTIVITIES) as _worker:
        raw = await temporal_env_with_converter.client.execute_workflow(
            "change-to-mr",
            ChangeRequest(request="x", repo=repo, dry_run=True),
            id="exec-query-1", task_queue="test-task-queue",
        )
        result = raw.get("result", raw) if isinstance(raw, dict) else raw
        assert result["status"] == "ready_for_review"
