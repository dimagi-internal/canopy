"""Tests for the harvest corpus engine (cross-user, origin-anchored, blindness-flagging)."""
import json
import os

import pytest

from orchestrator.harvest import (
    assemble_corpus,
    find_initiative_sessions,
    human_messages,
    user_session_roots,
)


def _session(path, prompts):
    """Write a minimal Claude jsonl with the given human prompts."""
    lines = [json.dumps({"type": "user", "message": {"content": p}}) for p in prompts]
    path.write_text("\n".join(lines) + "\n")


def _fake_machine(tmp_path):
    """Two macOS users; one readable, mimic the cross-user setup."""
    users = tmp_path / "Users"
    # user A: has a ddd initiative origin + recent
    a = users / "ace" / ".claude" / "projects" / "-x-emdash-ddd-origin"
    a.mkdir(parents=True)
    _session(a / "old.jsonl", ["build the demo-driven-development video pipeline", "keep going"])
    b = users / "ace" / ".claude" / "projects" / "-x-emdash-other"
    b.mkdir(parents=True)
    _session(b / "x.jsonl", ["totally unrelated task about billing"])
    # user J: has the EARLIEST ddd session (origin lives on the other account)
    j = users / "jjk" / ".claude" / "projects" / "-x-emdash-ddd-seed"
    j.mkdir(parents=True)
    _session(j / "seed.jsonl", ["I want to record demo videos / synthetic walkthroughs"])
    return users


def _roots(users):
    return [
        {"user": "ace", "path": str(users / "ace" / ".claude" / "projects"), "readable": True},
        {"user": "jjk", "path": str(users / "jjk" / ".claude" / "projects"), "readable": True},
    ]


def test_finds_across_users_oldest_first(tmp_path):
    users = _fake_machine(tmp_path)
    roots = _roots(users)
    # make the jjk seed the OLDEST
    import os, time
    seed = users / "jjk" / ".claude" / "projects" / "-x-emdash-ddd-seed" / "seed.jsonl"
    old = users / "ace" / ".claude" / "projects" / "-x-emdash-ddd-origin" / "old.jsonl"
    os.utime(seed, (time.time() - 3000, time.time() - 3000))
    os.utime(old, (time.time() - 1000, time.time() - 1000))
    refs = find_initiative_sessions("ddd", ["ddd", "demo-driven", "walkthrough", "demo video"], roots=roots)
    users_in_order = [r.user for r in refs]
    assert users_in_order[0] == "jjk", "origin (oldest) must come first, and it's on the OTHER user"
    assert "ace" in users_in_order
    # the unrelated billing session is excluded
    assert all("other" not in r.project for r in refs)


def test_corpus_flags_half_blind(tmp_path):
    users = _fake_machine(tmp_path)
    roots = _roots(users)
    roots[1]["readable"] = False  # jjk unreadable
    corpus = assemble_corpus("ddd", ["ddd", "demo", "walkthrough"], roots=roots)
    assert corpus["confidence"] == "half-blind"
    assert corpus["unreadable_users"] == ["jjk"]


def test_corpus_whole_when_all_readable(tmp_path):
    users = _fake_machine(tmp_path)
    corpus = assemble_corpus("ddd", ["ddd", "demo", "walkthrough"], roots=_roots(users))
    assert corpus["confidence"] == "whole-corpus"
    assert corpus["by_user"].get("jjk", 0) >= 1 and corpus["by_user"].get("ace", 0) >= 1
    assert corpus["span"] is not None


def test_human_messages_filters_noise(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "real intent here"}}),
        json.dumps({"type": "user", "message": {"content": "<system-reminder>noise</system-reminder>"}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        json.dumps({"type": "user", "message": {"content": "more steering"}}),
    ]) + "\n")
    msgs = human_messages(str(p))
    assert msgs == ["real intent here", "more steering"]


@pytest.mark.skipif(os.environ.get("CI") == "true",
                    reason="walks the real /Users/* of THIS machine (asserts the "
                           "acedimagi account exists) — meaningless on a CI runner")
def test_user_session_roots_real_machine():
    # smoke: on the real machine this should at least find acedimagi
    roots = user_session_roots()
    assert any(r["user"] == "acedimagi" for r in roots)


def test_strip_session_final_vs_full(tmp_path):
    import json as _j
    from orchestrator.harvest import strip_session
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join([
        _j.dumps({"type": "user", "message": {"content": "do the thing"}}),
        _j.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "step one narration"}]}}),
        _j.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "FINAL answer A"}]}}),
        _j.dumps({"type": "user", "message": {"content": "next"}}),
        _j.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "FINAL answer B"}]}}),
    ]) + "\n")
    final = strip_session(str(p), mode="final")
    assert "FINAL answer A" in final and "FINAL answer B" in final
    assert "step one narration" not in final          # intermediate prose dropped in final mode
    full = strip_session(str(p), mode="full")
    assert "step one narration" in full               # kept in full mode


def test_session_digest_shape(tmp_path):
    import json as _j
    from orchestrator.harvest import session_digest
    p = tmp_path / "s.jsonl"
    msgs = [_j.dumps({"type": "user", "message": {"content": f"input {i}"}}) for i in range(10)]
    msgs.append(_j.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ended here"}]}}))
    p.write_text("\n".join(msgs) + "\n")
    d = session_digest(str(p), user="ace", mtime=0.0, inputs_k=4)
    assert d["turns"] == 10
    assert d["first_input"] == "input 0"
    assert len(d["inputs"]) <= 4
    assert d["final_output"] == "ended here"


def test_corpus_map_cross_user(tmp_path):
    from orchestrator.harvest import corpus_map
    users = _fake_machine(tmp_path)
    m = corpus_map("ddd", ["ddd", "demo", "walkthrough", "demo video"], roots=_roots(users))
    assert m["confidence"] == "whole-corpus"
    assert m["total_sessions"] >= 2
    assert all("path" in d and "first_input" in d for d in m["digests"])


def test_intent_prompt_has_rubric_material_and_schema():
    from orchestrator.harvest import build_intent_prompt
    p = build_intent_prompt("USER: do X\n\nASSISTANT: I did Y", ["always run the tests first"])
    # embeds the human's own words (the close-read evidence)
    assert "do X" in p and "always run the tests first" in p
    # names the intent-miss classes it must flag
    for term in ["approved", "shipped", "approval", "eroded"]:
        assert term.lower() in p.lower()
    # REQUIRES the SP1 evidence record with a verbatim source_ref quote
    assert "source_ref" in p and "already_fixed_check" in p and "confidence_basis" in p
    assert "verbatim" in p.lower()


def test_session_digest_full_keeps_all_inputs_untruncated(tmp_path):
    import json as _j
    from orchestrator.harvest import session_digest
    p = tmp_path / "s.jsonl"
    long_in = "x" * 500
    msgs = [_j.dumps({"type": "user", "message": {"content": long_in}}) for _ in range(8)]
    p.write_text("\n".join(msgs) + "\n")
    full = session_digest(str(p), full=True)
    assert len(full["inputs"]) == 8                      # all kept, not sampled
    assert full["inputs"][0] == long_in                  # untruncated
    tiny = session_digest(str(p), full=False)
    assert len(tiny["inputs"]) <= 6 and len(tiny["inputs"][0]) <= 160   # sampled + truncated


def test_intent_audit_no_llm_returns_material_no_findings(tmp_path, monkeypatch):
    from orchestrator import harvest
    # a minimal real jsonl with one human msg + one assistant reply
    j = tmp_path / "s.jsonl"
    j.write_text(
        '{"type":"user","message":{"content":"approve the broad option"}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"shipped the narrow one"}]}}\n')
    out = harvest.intent_audit(str(j), use_llm=False)
    assert out["error"] is None and out["qualified"] == [] and out["dropped"] == []


def test_intent_audit_validates_emitted_findings(tmp_path, monkeypatch):
    from orchestrator import harvest
    j = tmp_path / "s.jsonl"
    j.write_text('{"type":"user","message":{"content":"approve the broad option"}}\n')
    good = {"title": "approved broad, shipped narrow", "friction_type": "intent_miss", "fix_kind": "skill_edit",
            "target": "skills/x", "recommendation": "honor the approved scope",
            "evidence": {"source_ref": "you: 'approve the broad option'", "was_read": True,
                         "already_fixed_check": {"ran": True, "result": "live on main"},
                         "confidence": "high", "confidence_basis": "verbatim quote diverges from the shipped narrow filter"}}
    bad = {"title": "vibes", "friction_type": "intent_miss", "evidence": "I feel you wanted more"}
    monkeypatch.setattr(harvest, "_run_intent_llm", lambda *a, **k: ([good, bad], None))
    out = harvest.intent_audit(str(j), use_llm=True)
    assert [f["title"] for f in out["qualified"]] == ["approved broad, shipped narrow"]
    assert len(out["dropped"]) == 1  # the vibes finding has no evidence record


def _evidence(source_ref):
    return {"source_ref": source_ref, "was_read": True,
            "already_fixed_check": {"ran": True, "result": "live on main"},
            "confidence": "high", "confidence_basis": "verbatim quote diverges from what shipped"}


def test_intent_audit_drops_fabricated_quote(tmp_path, monkeypatch):
    """SP3 gap: qualify_findings validates evidence SHAPE, not that source_ref is real —
    an LLM can fabricate a Jonathan quote and it would pass. The grounding pass added in
    intent_audit must catch it."""
    from orchestrator import harvest
    j = tmp_path / "s.jsonl"
    j.write_text('{"type":"user","message":{"content":"approve the broad option"}}\n')
    fabricated = {
        "title": "approved broad, shipped narrow", "friction_type": "intent_miss",
        "fix_kind": "skill_edit", "target": "skills/x", "recommendation": "honor the approved scope",
        "evidence": _evidence("you: 'a thing never said in this session xyz'"),
    }
    monkeypatch.setattr(harvest, "_run_intent_llm", lambda *a, **k: ([fabricated], None))
    out = harvest.intent_audit(str(j), use_llm=True)
    assert out["qualified"] == []
    assert len(out["dropped"]) == 1
    assert "fabricated" in out["dropped"][0]["_drop_reason"].lower()


def test_intent_audit_keeps_grounded_quote(tmp_path, monkeypatch):
    from orchestrator import harvest
    j = tmp_path / "s.jsonl"
    j.write_text(
        '{"type":"user","message":{"content":"only ship the broad approved version, not a narrower cut"}}\n')
    grounded = {
        "title": "approved broad, shipped narrow", "friction_type": "intent_miss",
        "fix_kind": "skill_edit", "target": "skills/x", "recommendation": "honor the approved scope",
        "evidence": _evidence("you: 'only ship the broad approved version, not a narrower cut'"),
    }
    monkeypatch.setattr(harvest, "_run_intent_llm", lambda *a, **k: ([grounded], None))
    out = harvest.intent_audit(str(j), use_llm=True)
    assert [f["title"] for f in out["qualified"]] == ["approved broad, shipped narrow"]
    assert out["dropped"] == []


def test_intent_audit_keeps_class1_with_positive_phrasing(tmp_path, monkeypatch):
    """SP3 gap: the inherited invariant rail (never/always/must not/do not) false-drops
    legit class-1..3 intent findings. A class-1 finding phrased POSITIVELY and NOT shipped
    as hook_rule/schema_validator must survive."""
    from orchestrator import harvest
    j = tmp_path / "s.jsonl"
    j.write_text(
        '{"type":"user","message":{"content":"approve the broad rollout, not just the pilot"}}\n')
    class1 = {
        "title": "approved broad rollout, shipped pilot-only", "friction_type": "intent_miss",
        "fix_kind": "skill_edit", "target": "skills/x",
        "recommendation": "honor the approved broad scope",  # positively phrased, no invariant words
        "evidence": _evidence("you: 'approve the broad rollout, not just the pilot'"),
    }
    monkeypatch.setattr(harvest, "_run_intent_llm", lambda *a, **k: ([class1], None))
    out = harvest.intent_audit(str(j), use_llm=True)
    assert [f["title"] for f in out["qualified"]] == ["approved broad rollout, shipped pilot-only"]
    assert out["dropped"] == []


def test_run_intent_llm_garbage_is_error(tmp_path, monkeypatch):
    """SP3 gap: rc=0 with non-YAML prose stdout parses to [] and used to masquerade as a
    clean (no-findings) audit. It must fail loud instead."""
    from orchestrator import harvest

    class _Proc:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    monkeypatch.setattr(
        harvest.subprocess, "run",
        lambda *a, **k: _Proc("I could not find anything: prose not yaml"))
    findings, err = harvest._run_intent_llm("prompt", "sonnet", 2.0)
    assert findings is None
    assert err is not None and "did not parse to a YAML list" in err

    monkeypatch.setattr(harvest.subprocess, "run", lambda *a, **k: _Proc("[]"))
    findings, err = harvest._run_intent_llm("prompt", "sonnet", 2.0)
    assert findings == [] and err is None
