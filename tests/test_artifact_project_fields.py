"""Uploaded artifacts say which project they belong to (canopy-web T76).

0 of 63 supply walkthroughs carried a project: the DDD uploader never sent one,
and the only default anywhere was the cwd's directory name — which inside an
emdash worktree is `emdash-<task>-<suffix>` and names nothing.
"""
from __future__ import annotations

import subprocess

from orchestrator import provenance


def _repo(tmp_path, remote):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", remote], check=True)
    return tmp_path


def test_repo_slug_comes_from_the_origin_remote_not_the_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPY_PROJECT_SLUG", raising=False)
    monkeypatch.delenv("CANOPY_AGENT_PROJECT", raising=False)
    wt = _repo(tmp_path / "emdash-task-abc12", "git@github.com:dimagi/connect-labs.git")
    assert provenance.artifact_project_fields(str(wt)) == {"project_slug": "connect-labs"}
    https = _repo(tmp_path / "x", "https://github.com/dimagi-internal/canopy-web")
    assert provenance.artifact_project_fields(str(https))["project_slug"] == "canopy-web"


def test_env_overrides_and_agent_project_rides_along(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_PROJECT_SLUG", "supply-demo")
    monkeypatch.setenv("CANOPY_AGENT_PROJECT", "hal/P5")
    assert provenance.artifact_project_fields(str(tmp_path)) == {
        "project_slug": "supply-demo", "agent_project": "hal/P5"}


def test_nothing_knowable_means_nothing_sent(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPY_PROJECT_SLUG", raising=False)
    monkeypatch.setenv("CANOPY_AGENT_PROJECT", "bad value\nX-Evil: 1")
    assert provenance.artifact_project_fields(str(_repo(tmp_path, None))) == {}

