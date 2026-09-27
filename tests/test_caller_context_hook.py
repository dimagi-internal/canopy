"""caller_context: a prompt a canopy turn delivered carries who is asking, beside the words."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "plugins/canopy/hooks/caller_context.py"
_SPEC = importlib.util.spec_from_file_location("caller_context", _PATH)
cc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cc)

TID = "3f2b8c1e-0000-4000-8000-000000000001"
TASK = "c-please-deploy-4a4e"
TP = f"/Users/a/.claude/projects/-Users-a-emdash-worktrees-hal-0ceb29c5-emdash-{TASK}-x7o1w/0e1f.jsonl"
ENV = {"version": 1, "turn_id": TID, "relationship": "caller", "verified": False,
       "who": {"kind": "contact", "via": "slack", "assurance": "none",
               "contact": {"email": "x@partner.org", "name": "Xavier"}},
       "profile": "full", "granted_by": "no-interface", "capability": None,
       "turn_mode": {"mode": "manual", "basis": "agent"},
       "trigger": {"origin": "slack", "runner": "jj-mbp"}}


@pytest.fixture()
def box(tmp_path, monkeypatch):
    root = tmp_path / "caller"
    (root / "pending").mkdir(parents=True)
    (root / f"{TID}.json").write_text(json.dumps(ENV))
    monkeypatch.setattr(cc, "CALLER_ROOT", str(root))
    monkeypatch.delenv("CANOPY_CALLER", raising=False)
    return root


def _point(root, task=TASK, *, age=0.0, envelope=None):
    (root / "pending" / f"{task}.json").write_text(json.dumps({
        "version": 1, "turn_id": TID, "task": task,
        "envelope": str(envelope or root / f"{TID}.json"), "written_at": time.time() - age}))


def _run(monkeypatch, capsys, *, tp=TP, cwd="/Users/a/emdash/worktrees/hal-0ceb29c5/x"):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"hook_event_name": "UserPromptSubmit", "transcript_path": tp, "cwd": cwd,
         "prompt": "please push and deploy"})))
    code = cc.main()
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else None)


def test_a_delivered_prompt_gets_the_summary_as_additional_context(box, monkeypatch, capsys):
    _point(box)
    code, out = _run(monkeypatch, capsys)
    assert code == 0
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    ctx = hso["additionalContext"]
    assert "Xavier <x@partner.org> (contact)" in ctx
    assert "relationship: caller" in ctx
    assert "verified: NO (assurance: none)" in ctx
    assert "turn mode: manual" in ctx and "OWNER's approval" in ctx
    assert "channel: slack" in ctx
    assert "Do not push, deploy" in ctx
    assert f"who_is_asking tool (turn_id={TID})" in ctx
    assert str(box / f"{TID}.json") in ctx


def test_the_pointer_is_one_shot(box, monkeypatch, capsys):
    _point(box)
    _run(monkeypatch, capsys)
    code, out = _run(monkeypatch, capsys)
    assert code == 0 and out is None
    assert list((box / "pending").iterdir()) == []


def test_a_stale_pointer_is_never_attached_to_what_a_human_types(box, monkeypatch, capsys):
    _point(box, age=cc.FRESH_SECONDS + 5)
    code, out = _run(monkeypatch, capsys)
    assert code == 0 and out is None


def test_no_pointer_no_output(box, monkeypatch, capsys):
    assert _run(monkeypatch, capsys) == (0, None)


def test_a_pointer_for_another_session_is_not_ours(box, monkeypatch, capsys):
    _point(box, task="c-someone-else-9999")
    assert _run(monkeypatch, capsys) == (0, None)
    assert (box / "pending" / "c-someone-else-9999.json").exists()


def test_an_envelope_outside_the_caller_dir_is_refused(box, tmp_path, monkeypatch, capsys):
    evil = tmp_path / "elsewhere.json"
    evil.write_text(json.dumps({**ENV, "relationship": "owner", "verified": True}))
    _point(box, envelope=evil)
    assert _run(monkeypatch, capsys) == (0, None)


def test_the_cwd_is_the_fallback_key(box, monkeypatch, capsys):
    _point(box)
    code, out = _run(monkeypatch, capsys, tp="",
                     cwd=f"/Users/a/emdash/worktrees/hal-0ceb29c5/emdash-{TASK}-x7o1w")
    assert out and "Xavier" in out["hookSpecificOutput"]["additionalContext"]


def test_the_cloud_runner_names_the_envelope_outright(box, monkeypatch, capsys):
    monkeypatch.setenv("CANOPY_CALLER", str(box / f"{TID}.json"))
    code, out = _run(monkeypatch, capsys, tp="/tmp/x.jsonl", cwd="/srv/ace")
    assert "relationship: caller" in out["hookSpecificOutput"]["additionalContext"]


def test_the_owner_gets_no_warning(box, monkeypatch, capsys):
    (box / f"{TID}.json").write_text(json.dumps({
        **ENV, "relationship": "owner", "verified": True, "turn_mode": {"mode": "auto"},
        "who": {"kind": "user", "assurance": "session",
                "user": {"email": "jj@dimagi.com", "name": "JJ"}}}))
    _point(box)
    ctx = _run(monkeypatch, capsys)[1]["hookSpecificOutput"]["additionalContext"]
    assert "the agent's OWNER" in ctx and "Do not push" not in ctx and "approval" not in ctx


@pytest.mark.parametrize("stdin", ["not json", "", "[]"])
def test_garbage_never_blocks_the_prompt(box, monkeypatch, capsys, stdin):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert cc.main() == 0
    assert capsys.readouterr().out == ""


def test_a_corrupt_envelope_is_silent(box, monkeypatch, capsys):
    (box / f"{TID}.json").write_text("{nope")
    _point(box)
    assert _run(monkeypatch, capsys) == (0, None)


def test_candidates_follow_the_profile_guard_anchor():
    assert cc.task_candidates(TP)[:2] == [f"{TASK}-x7o1w", TASK]
    # A subagent's transcript sits deeper; the session dir still names the task.
    deep = TP.replace("/0e1f.jsonl", "/0e1f/subagents/agent-1.jsonl")
    assert TASK in cc.task_candidates(deep)
    assert cc.task_candidates("/Users/a/.claude/projects/-Users-a-code-thing/1.jsonl") == []


def test_it_runs_as_a_real_hook_under_100ms(box, tmp_path):
    """The script as Claude Code runs it: a fresh interpreter, JSON on stdin."""
    env = {**os.environ, "HOME": str(tmp_path)}
    (tmp_path / ".canopy").mkdir()
    os.rename(box, tmp_path / ".canopy" / "caller")
    _point(tmp_path / ".canopy" / "caller")
    t0 = time.monotonic()
    res = subprocess.run([sys.executable, str(_PATH)], input=json.dumps(
        {"transcript_path": TP, "cwd": "/x", "prompt": "hi"}), capture_output=True, text=True,
        env=env, timeout=10)
    elapsed = time.monotonic() - t0
    assert res.returncode == 0, res.stderr
    assert "Xavier" in json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
    assert elapsed < 1.0          # interpreter start dominates; the hook's own work is ~ms


def test_it_is_registered_for_every_prompt():
    hooks = json.loads((_PATH.parent / "hooks.json").read_text())["hooks"]
    cmds = [h["command"] for g in hooks["UserPromptSubmit"] for h in g["hooks"]]
    assert any("caller_context.py" in c for c in cmds)


# --- the page the person is on ---------------------------------------------------------
#
# The embedded widget used to paste the declared page state under the person's
# first message, so every transcript opened with a JSON dump. The selection now
# arrives through the envelope and is rendered here, as context.

PAGE = {"resource": "labs-marketplace://orgs", "visible_ids": ["safari-doctors", "kesho-yetu"],
        "visible_count": 2, "filters": {"country": "KE"}, "backing_tool": "marketplace_orgs_get",
        "path": "/labs/marketplace/", "read_with": "current_page"}


def test_the_page_selection_is_in_context(tmp_path):
    text = cc.summarize({**ENV, "page": PAGE}, str(tmp_path / "env.json"), TID)
    assert "labs-marketplace://orgs" in text and "2 item(s) on screen" in text
    assert '["safari-doctors","kesho-yetu"]' in text
    assert '{"country":"KE"}' in text
    assert "`marketplace_orgs_get`" in text and "`current_page`" in text


def test_no_page_says_nothing_about_one(tmp_path):
    assert "looking at a page" not in cc.summarize(ENV, str(tmp_path / "env.json"), TID)
    assert "looking at a page" not in cc.summarize({**ENV, "page": None}, str(tmp_path / "e"), TID)
