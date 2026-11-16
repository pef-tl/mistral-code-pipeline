"""Tests unitaires des helpers (pas de réseau, pas de LLM)."""

import json
import os

import pytest

from worker.activities import git_tools, test_runner
from worker.workflows.change_to_mr import _extract_json, _slugify


# ------------------------------------------------------------- git_tools ---

def test_normalize_repo_url():
    assert git_tools.normalize_repo_url("git@gitlab.example.com:a/b.git") \
        == "https://gitlab.example.com/a/b.git"
    assert git_tools.normalize_repo_url("ssh://git@gitlab.example.com/a/b.git") \
        == "https://gitlab.example.com/a/b.git"
    assert git_tools.normalize_repo_url("https://gitlab.example.com/a/b.git") \
        == "https://gitlab.example.com/a/b.git"


def test_parse_repo_url():
    assert git_tools._parse_repo_url("https://gitlab.example.com/a/b/c.git") == ("gitlab.example.com", "a/b/c")
    assert git_tools._parse_repo_url("git@gitlab.example.com:a/b.git") == ("gitlab.example.com", "a/b")


def test_gitlab_api_encoding():
    api, project = git_tools._gitlab_api("https://gitlab.toollive.bearstech.com/toollive-team/toollive-business/toollive-c8y.git")
    assert api == "https://gitlab.toollive.bearstech.com/api/v4"
    assert project == "toollive-team%2Ftoollive-business%2Ftoollive-c8y"


def test_extract_url_token_never_leaks(monkeypatch):
    monkeypatch.delenv("gitlab_token", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("pipeline_git_token", raising=False)
    clean, token = git_tools.extract_url_token("https://user:SECRET@gitlab.example.com/a/b.git")
    assert clean == "https://gitlab.example.com/a/b.git"
    assert token == "SECRET"
    assert "SECRET" not in clean


def test_git_token_case_insensitive(monkeypatch):
    for k in ("gitlab_token", "GITLAB_TOKEN", "pipeline_git_token", "PIPELINE_GIT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GITLAB_TOKEN", "  PAT123 \n")
    assert git_tools.git_token() == "PAT123"
    assert git_tools.git_token_source() == "GITLAB_TOKEN"
    monkeypatch.delenv("GITLAB_TOKEN")
    assert git_tools.git_token() is None


def test_scrub_masks_token(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "PATXYZ")
    out = git_tools._scrub("fatal: https://oauth2:PATXYZ@host/x.git refused")
    assert "PATXYZ" not in out
    assert "****" in out


def test_is_url():
    assert git_tools.is_url("https://gitlab.example.com/a/b.git")
    assert git_tools.is_url("git@gitlab.example.com:a/b.git")
    assert not git_tools.is_url("/tmp/local-repo")


async def test_detect_base_branch(tmp_path):
    import subprocess
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=repo, check=True, capture_output=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "i"],
                   cwd=repo, check=True, capture_output=True)
    assert await git_tools.detect_base_branch(str(repo)) == "master"
    assert await git_tools.detect_base_branch(str(repo), override="develop") == "develop"


# ----------------------------------------------------------- test_runner ---

def test_test_cmd_for_detection(tmp_path, monkeypatch):
    monkeypatch.delenv("pipeline_test_cmd", raising=False)
    monkeypatch.delenv("PIPELINE_TEST_CMD", raising=False)
    (tmp_path / "package.json").write_text("{}")
    assert test_runner.test_cmd_for(str(tmp_path)) == ["npm", "run", "test-unit", "--", "--ci", "--silent"]

    py = tmp_path / "py"
    py.mkdir()
    (py / "pyproject.toml").write_text("[project]")
    assert test_runner.test_cmd_for(str(py)) == ["uv", "run", "pytest", "-x", "-q"]

    empty = tmp_path / "empty"
    empty.mkdir()
    assert test_runner.test_cmd_for(str(empty)) == test_runner.TEST_CMD

    assert test_runner.test_cmd_for(str(empty), override="make check") == ["make", "check"]


# ------------------------------------------------------------- workflow ----

def test_slugify():
    assert _slugify("Ajouter un filtre par date !") == "ajouter-un-filtre-par-date"
    assert _slugify("???") == "change"


def test_extract_json():
    assert _extract_json('{"approve": true}') == {"approve": True}
    assert _extract_json('prose\n```json\n{"approve": false, "findings": ["x"]}\n```') \
        == {"approve": False, "findings": ["x"]}
    assert _extract_json('voici {"a": 1} et la suite') == {"a": 1}
    with pytest.raises(RuntimeError):
        _extract_json("pas de json ici")
    with pytest.raises(RuntimeError):
        _extract_json("")


def test_change_request_defaults():
    from worker.workflows.change_to_mr import ChangeRequest
    req = ChangeRequest(request="x")
    assert "toollive-c8y" in req.repo
    assert req.max_iterations == 5
    assert req.dry_run is False
    json.dumps(req.model_dump())  # sérialisable (entrée du workflow)
