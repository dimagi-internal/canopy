"""Tests for the doc-regeneration skill's helper scripts.

The skill tells agents to run these instead of hand-rolled greps, so the load-
bearing behaviors are: references resolve in every form docs actually use
(including the dotted `apps/agents/access.decide` form an ad-hoc checker
mis-reported), history is found by date and PR number and not just by phrase,
and an already-regenerated repo is detected so the run is incremental.

Each script is exercised through its CLI via subprocess against a temp repo.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "canopy" / "skills" / "doc-regeneration" / "scripts"
)


def _run(script: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True, text=True, check=False, cwd=cwd,
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True, capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "apps" / "agents").mkdir(parents=True)
    (repo / "apps" / "agents" / "__init__.py").write_text("")
    (repo / "apps" / "agents" / "access.py").write_text(
        "LIMIT: int = 3\n\ndef decide(x):\n    return x\n\nclass Gate:\n    def check(self):\n        pass\n"
    )
    (repo / "apps" / "tokens").mkdir()
    (repo / "apps" / "tokens" / "__init__.py").write_text("")
    (repo / "apps" / "tokens" / "middleware.py").write_text("def mw():\n    pass\n")
    (repo / "docs" / "architecture").mkdir(parents=True)
    (repo / "docs" / "architecture" / "sibling.md").write_text("# s\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


# ---- check_refs.py ---------------------------------------------------------

def _check(repo: Path, body: str) -> subprocess.CompletedProcess[str]:
    doc = repo / "docs" / "architecture" / "doc.md"
    doc.write_text(body)
    return _run("check_refs.py", "--root", str(repo), str(doc))


def test_refs_resolve_in_every_form(tmp_path):
    repo = _repo(tmp_path)
    result = _check(repo, (
        "- `apps/agents/access.py` and `apps/agents/` exist\n"
        "- `apps/agents/access.py::decide`, `apps/agents/access.py::Gate.check`, `apps/agents/access.py::LIMIT`\n"
        "- `apps/agents/access.decide` is the one rule\n"
        "- `apps.agents.access.decide` and the module `apps.tokens.middleware`\n"
        "- `sibling.md` beside the doc, and [a link](sibling.md)\n"
    ))
    assert result.returncode == 0, result.stdout
    assert "checked 10, broken 0" in result.stdout


def test_broken_refs_are_reported(tmp_path):
    repo = _repo(tmp_path)
    result = _check(repo, (
        "- `apps/agents/gone.py`\n"
        "- `apps/agents/access.py::vanished`\n"
        "- `apps/agents/access.vanished`\n"
        "- `apps.agents.access.vanished`\n"
        "- [dead](missing.md)\n"
    ))
    assert result.returncode == 1
    out = result.stdout
    assert "MISSING docs/architecture/doc.md:1 `apps/agents/gone.py`" in out
    assert out.count("SYMBOL") == 3
    assert "MISSING docs/architecture/doc.md:5 `missing.md`" in out
    assert "checked 5, broken 5" in out


def test_non_references_are_skipped(tmp_path):
    repo = _repo(tmp_path)
    result = _check(repo, (
        "- placeholders `docs/<topic>.md`, `apps/*/api.py`, `/api/w/{ws}/x`, `…/x.py`, `$HOME/x.py`\n"
        "- routes `agents/new`, `chat/:id`, `/api/openapi.json`, a MIME type `application/problem+json`\n"
        "- migration labels `contacts/0022`, a runtime file `ready.json`, prose `perms.can`\n"
        "- a URL `https://example.com/a.py` and a command `uv run pytest`\n"
        "```\n`apps/agents/gone.py` inside a fence\n```\n"
    ))
    assert result.returncode == 0, result.stdout
    assert "checked 0, broken 0" in result.stdout


# ---- find_history.py -------------------------------------------------------

def test_history_found_by_date_pr_and_phrase(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text(
        "Observed on 2026-07-26 that runs stalled.\n"
        "Fixed in a follow-up (#1234).\n"
        "See canopy-web#987 for the chain.\n"
        "Routing used to key off capabilities.\n"
        "The old poller was replaced by push.\n"
        "Owner decision: keep it.\n"
        "It was a cron, now it is a push.\n"
    )
    result = _run("find_history.py", str(doc))
    assert result.returncode == 1
    out = result.stdout
    assert f"{doc}:1: [date]" in out
    assert f"{doc}:2: [pr-ref,phrase]" in out
    assert f"{doc}:3: [pr-ref]" in out
    for line in (4, 5, 6, 7):
        assert f"{doc}:{line}: [phrase]" in out
    assert "total flagged: 7" in out


def test_history_ignores_specs_sections_and_present_tense(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text(
        "Design: docs/superpowers/specs/2026-10-06-huddle-design.md\n"
        "Design: [the spec](docs/superpowers/specs/2026-10-06-huddle-design.md)\n"
        "Per section (4.1.5.1) of the contract.\n"
        "Seam #2 is the one allowlisted exception.\n"
        "The token is used to sign requests.\n"
        "Send `Action=GetCallerIdentity&Version=2011-06-15`.\n"
        "## Heading\n"
        "```\nreleased 2026-01-01 (#12)\n```\n"
    )
    result = _run("find_history.py", str(doc))
    assert result.returncode == 0, result.stdout
    assert "total flagged: 0" in result.stdout


# ---- regen_status.py -------------------------------------------------------

def test_status_detects_incremental_mode_and_growth(tmp_path):
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_text("# R\n\n> How to write this file: current state only.\n")
    (repo / "docs" / "architecture" / "big.md").write_text("x" * 600)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs: tidy", "-m", "Doc-Regen: full")
    (repo / "CLAUDE.md").write_text("# R\n\n> How to write this file: current state only.\nmore\n")
    (repo / "docs" / "architecture" / "sibling.md").write_text("# s\nchanged\n")

    result = _run("regen_status.py", "--topic-warn", "500", cwd=repo)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "mode: incremental" in out
    assert "last regen: " in out and "docs: tidy" in out
    assert "growth:" in out
    assert "docs/architecture/sibling.md" in out  # changed since the last regen
    assert "big.md  WARN" in out


def test_status_full_mode_without_prior_regen(tmp_path):
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_text("# R\n\nA long file with history.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat: something")
    result = _run("regen_status.py", cwd=repo)
    assert result.returncode == 0, result.stderr
    assert "mode: full" in result.stdout
    assert "last regen: none found" in result.stdout
