"""Tests for the agent self-improvement lens (deterministic friction extraction; no LLM)."""
import json

from orchestrator.agent_review import (
    FRICTION_TYPES,
    build_review_prompt,
    find_turn_transcripts,
    friction_signals,
    parse_findings,
    resolve_agent_repo,
)
from orchestrator.agent_factory import AgentSpec, create_agent


def _write_transcript(path, cwd, calls):
    """calls: list of (tool, input_dict, result_str). Writes a minimal Claude jsonl."""
    lines = []
    for i, (tool, inp, result) in enumerate(calls):
        tid = f"t{i}"
        lines.append({
            "type": "assistant", "cwd": cwd,
            "message": {"content": [
                {"type": "tool_use", "id": tid, "name": tool, "input": inp},
            ]},
        })
        lines.append({
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": result},
            ]},
        })
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def test_friction_signals_detects_failures_blocks_retries_auth(tmp_path):
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Bash", {"command": "gog gmail send --to a@b.c"}, "BLOCKED: use the wrapper. exit code 2"),
        ("Bash", {"command": "gog gmail search"}, "People API has not been used... 403"),
        ("Bash", {"command": "gog gmail search"}, "ok, 3 messages"),   # retry of a failed tool
        ("Read", {"file_path": "/x"}, "file contents fine"),
    ])
    s = friction_signals(t)
    assert len(s["gating_blocks"]) == 1
    assert any("403" in f["evidence"] or "API" in f["evidence"] for f in s["failures"])
    assert s["auth_friction"], "the 403/API-not-enabled line should flag auth friction"
    assert "Bash" in s["retry_loops"], "a failed tool re-tried should be a retry loop"


def test_friction_signals_flags_checklist_gaps(tmp_path):
    t = tmp_path / "turn.jsonl"
    # A turn (it loaded skills/turn) that does none of the expected steps -> all gaps.
    _write_transcript(t, str(tmp_path), [
        ("Read", {"file_path": "/repo/skills/turn/SKILL.md"}, "Hal's turn loop..."),
        ("Read", {"file_path": "/x"}, "ok"),
    ])
    gaps = set(friction_signals(t)["checklist_gaps"])
    assert {"preflight", "self-review", "skill-self-check", "workspace-refresh"} <= gaps


def test_non_turn_session_not_graded_against_turn_steps(tmp_path):
    # An `architect ddd` / harvest session is NOT a turn — grading it against the turn-step
    # checklist flagged every one as a 4-gap "failure storm" (hal's 2026-07 review). No turn
    # marker anywhere -> no checklist gaps.
    t = tmp_path / "architect.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Bash", {"command": "canopy harvest map ddd --full"}, "331 sessions, whole-corpus"),
        ("Write", {"file_path": "/repo/ledgers/ddd.md"}, "File created successfully"),
    ])
    assert friction_signals(t)["checklist_gaps"] == []


def test_friction_signals_credits_steps_that_ran(tmp_path):
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Bash", {"command": "python3 bin/echo_preflight.py"}, "ready"),
        ("Bash", {"command": "canopy agent-publish skills"}, "ok"),
    ])
    gaps = set(friction_signals(t)["checklist_gaps"])
    assert "preflight" not in gaps          # preflight marker present
    assert "workspace-refresh" not in gaps  # agent-publish marker present


def test_clean_turn_has_no_friction(tmp_path):
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [("Read", {"file_path": "/x"}, "all good here")])
    s = friction_signals(t)
    assert s["failures"] == [] and s["gating_blocks"] == [] and s["retry_loops"] == []


def test_find_turn_transcripts_matches_by_cwd(tmp_path):
    repo = tmp_path / "repositories" / "echo"
    repo.mkdir(parents=True)
    projects = tmp_path / "projects"
    # A matching project dir (name carries the slug) with a turn run inside the repo.
    d = projects / "-Users-x-emdash-repositories-echo"
    d.mkdir(parents=True)
    _write_transcript(d / "a.jsonl", str(repo), [("Read", {"file_path": "/x"}, "ok")])
    # A non-matching project dir (different repo) should be ignored.
    other = projects / "-Users-x-emdash-repositories-other"
    other.mkdir(parents=True)
    _write_transcript(other / "b.jsonl", str(tmp_path / "other"), [("Read", {}, "ok")])

    found = find_turn_transcripts(repo, hours=99999, projects_dir=projects)
    assert len(found) == 1
    assert found[0].name == "a.jsonl"


def test_resolve_agent_repo_by_path(tmp_path):
    repo = tmp_path / "echo"
    create_agent(AgentSpec(slug="echo", display_name="Echo", mandate="x."), repo)
    assert resolve_agent_repo(str(repo)) == repo


def test_build_review_prompt_and_parse_findings(tmp_path):
    repo = tmp_path / "echo"
    create_agent(AgentSpec(slug="echo", display_name="Echo", mandate="x."), repo)
    prompt = build_review_prompt(repo, [{"session_id": "s", "failures": []}])
    for ftype in FRICTION_TYPES:
        assert ftype in prompt
    # parse_findings tolerates fenced YAML and non-list junk
    yaml_out = "```yaml\n- title: Fix auth\n  friction_type: auth_friction\n  confidence: high\n```"
    parsed = parse_findings(yaml_out)
    assert parsed and parsed[0]["title"] == "Fix auth"
    assert parse_findings("not a list") == []


def test_pr_status_output_is_not_a_gating_block_or_failure(tmp_path):
    # Hal's 2026-07 review: `gh pr view` output ("mergeable: MERGEABLE/BLOCKED",
    # "blocked only on required review") was misread as gating friction / failures.
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Bash", {"command": "gh pr view 1253"},
         "title: fix(mobile)\nmergeable: MERGEABLE/BLOCKED  +47/-5\nreview=REVIEW_REQUIRED"),
        ("AskUserQuestion", {"questions": []},
         'Your questions have been answered: "#1253 is green, blocked only on required review"'),
    ])
    s = friction_signals(t)
    assert s["gating_blocks"] == []
    assert s["failures"] == []
    assert s["retry_loops"] == []


def test_read_of_hook_source_is_not_a_gating_block(tmp_path):
    # Reading the gating hook's own source contains "permissionDecision" — a Read can't be gated.
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Read", {"file_path": "/repo/hooks/gating_guard.py"},
         'print(json.dumps({"hookSpecificOutput": {"permissionDecision": "ask"}}))'),
    ])
    assert friction_signals(t)["gating_blocks"] == []


def test_cat_of_gating_config_is_not_a_gating_block(tmp_path):
    # `cat config/gating.json` output carries "BLOCKED:" deep in the deny message — a real hook
    # block IS the whole result, so the marker must appear at the head to count.
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Bash", {"command": "cat config/gating.json"},
         "=== config/gating.json ===\n" + '{"_doc": "' + "policy prose " * 30 + '",\n'
         '"deny": [{"message": "BLOCKED: raw gog gmail send bypasses the wrapper."}]}'),
    ])
    assert friction_signals(t)["gating_blocks"] == []


def test_hook_ask_modal_counts_as_gating_block(tmp_path):
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Bash", {"command": "gog gmail send --to a@b.c"},
         '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", '
         '"permissionDecisionReason": "APPROVE Hal -> gog gmail send"}}'),
    ])
    s = friction_signals(t)
    assert len(s["gating_blocks"]) == 1
    assert s["failures"] == []


def test_retry_loop_requires_similar_subject_not_just_tool_reuse(tmp_path):
    # A failed Bash call followed by UNRELATED Bash calls is not a retry loop...
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Bash", {"command": "cat /nope/missing.json"}, "Exit code 1\ncat: not found"),
        ("Bash", {"command": "git status --short"}, "clean"),
        ("Bash", {"command": "ls -la /tmp"}, "total 0"),
    ])
    assert friction_signals(t)["retry_loops"] == []
    # ...but the same command re-run right after failing IS.
    t2 = tmp_path / "turn2.jsonl"
    _write_transcript(t2, str(tmp_path), [
        ("Bash", {"command": "cat /nope/missing.json"}, "Exit code 1\ncat: not found"),
        ("Bash", {"command": "cat /nope/missing.json || true"}, "ok"),
    ])
    assert friction_signals(t2)["retry_loops"] == ["Bash"]


def test_auth_marker_does_not_fire_on_successful_write(tmp_path):
    # hal's 2026-07 review: a SUCCESSFUL Write of a memory file named
    # `email-oauth-not-minted.md` was flagged as auth_friction because "oauth" is in the path.
    # A completed file write is never runtime friction.
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Write", {"file_path": "/repo/memory/email-oauth-not-minted.md"},
         "File created successfully at: /repo/memory/email-oauth-not-minted.md"),
    ])
    s = friction_signals(t)
    assert s["auth_friction"] == [], "a successful write is not auth friction"
    assert s["failures"] == []


def test_skill_collision_flags_loading_another_plugins_same_named_skill(tmp_path):
    # hal's 2026-07 review: "do a turn" loaded `ace:turn` (ACE's turn loop) instead of hal's own
    # skills/turn — a silent wrong-skill load the mechanical signals were blind to.
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Skill", {"skill": "ace:turn"}, "ACE's turn skill loaded"),
        ("Skill", {"skill": "canopy:improve"}, "improve skill loaded"),   # not owned -> fine
        ("Skill", {"skill": "architect"}, "own bare skill -> fine"),
    ])
    s = friction_signals(t, own_skills=frozenset({"turn", "architect", "self-review"}))
    cols = s["skill_collisions"]
    assert len(cols) == 1
    assert cols[0]["invoked"] == "ace:turn" and cols[0]["own_skill"] == "turn"
    assert "skill_collision" in FRICTION_TYPES


def test_human_corrections_catches_safety_override_and_confusion():
    from orchestrator.agent_review import human_corrections
    entries = [
        {"type": "user", "message": {"content": "take a turn on what came in"}},
        {"type": "user", "message": {"content": "I'm lost, why are you asking me about 1) then showing 2?"}},
        {"type": "user", "message": {"content": "your submission skill should NEVER EVER submit directly without human review"}},
        {"type": "user", "message": {"content": "go ahead"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
    ]
    cor = human_corrections(entries)
    quotes = " ".join(c["quote"] for c in cor)
    assert "NEVER EVER submit" in quotes          # the safety override is caught
    assert "I'm lost" in quotes                    # the confusion is caught
    allkinds = {k for c in cor for k in c["kinds"]}
    assert "safety_override" in allkinds and "confusion" in allkinds and "emphasis" in allkinds
    assert "go ahead" not in quotes and "take a turn" not in quotes   # neutral msgs ignored

def test_run_review_surfaces_stdout_error_and_sane_budget(tmp_path, monkeypatch):
    # Echo's 2026-07 review: claude -p exited 1 with "Error: Exceeded USD budget (0.5)"
    # on STDOUT (stderr empty), so the report said 'claude -p failed: ' — undiagnosable.
    # Pin both fixes: (a) stdout errors surface, (b) the default budget clears $0.50,
    # which a real 7-turn corpus empirically exceeds.
    import subprocess as sp
    from orchestrator import agent_review as ar

    repo = tmp_path / "repositories" / "echo"
    (repo / "skills").mkdir(parents=True)
    projects = tmp_path / "projects"
    d = projects / "-Users-x-emdash-repositories-echo"
    d.mkdir(parents=True)
    _write_transcript(d / "a.jsonl", str(repo), [("Read", {"file_path": "/x"}, "ok")])

    seen_cmds = []

    def fake_run(cmd, **kwargs):
        seen_cmds.append(cmd)
        return sp.CompletedProcess(
            cmd, returncode=1, stdout="Error: Exceeded USD budget (0.5)", stderr="")

    monkeypatch.setattr(ar.subprocess, "run", fake_run)
    result = ar.run_review(str(repo), projects_dir=projects)

    assert "Exceeded USD budget" in result["error"]          # (a) stdout not swallowed
    budget = float(seen_cmds[0][seen_cmds[0].index("--max-budget-usd") + 1])
    assert budget > 0.5                                       # (b) default clears the observed cost


# --- Source-verification gate (the recurring "re-surfaced an already-fixed finding" bug) ---

def test_source_gate_drops_shipped_keeps_live_and_annotates(tmp_path):
    """The gate drops a finding the LLM verdicts `shipped` and keeps `live`/`unverifiable`
    ones, annotating each with its verdict. git calls no-op in a non-repo tmp dir."""
    from orchestrator.agent_review import verify_findings_against_source

    findings = [
        {"title": "add reply-all default", "target": "bin/echo_email.py",
         "recommendation": "flip the default"},
        {"title": "label denominators", "target": "skills/org-research/SKILL.md",
         "recommendation": "inline the denominator"},
        {"title": "mystery", "target": "skills/x/SKILL.md", "recommendation": "do x"},
    ]

    def fake_verdict(_prompt):
        return [
            {"index": 0, "verdict": "shipped", "evidence": "already reads as:eva@ in main"},
            {"index": 1, "verdict": "live", "evidence": "still bare percentages"},
            # index 2 omitted → defaults to unverifiable → KEPT
        ]

    kept, dropped, error = verify_findings_against_source(tmp_path, findings, verdict_fn=fake_verdict)
    assert error is None
    assert [f["title"] for f in dropped] == ["add reply-all default"]
    assert dropped[0]["verification"]["verdict"] == "shipped"
    assert [f["title"] for f in kept] == ["label denominators", "mystery"]
    assert kept[0]["verification"]["verdict"] == "live"
    assert kept[1]["verification"]["verdict"] == "unverifiable"   # missing verdict → kept


def test_source_gate_fails_open_when_verification_unavailable(tmp_path):
    """If the verdict pass returns nothing (LLM error/parse miss), NOTHING is dropped —
    the gate never silently eats a finding it couldn't check."""
    from orchestrator.agent_review import verify_findings_against_source

    findings = [{"title": "z", "target": "a/b.py", "recommendation": "do z"}]
    kept, dropped, error = verify_findings_against_source(tmp_path, findings, verdict_fn=lambda _p: None)
    assert dropped == []
    assert kept == findings   # unchanged, not annotated — a true no-op on failure
    assert error                # ...and the reason is surfaced, never a silent pass


def test_run_review_applies_source_gate(tmp_path, monkeypatch):
    """run_review wires the gate: a synthesized finding the gate marks shipped lands in
    dropped_findings, not findings — with the gate ON by default."""
    import subprocess as sp
    from orchestrator import agent_review as ar

    repo = tmp_path / "repositories" / "echo"
    (repo / "skills").mkdir(parents=True)
    projects = tmp_path / "projects"
    d = projects / "-Users-x-emdash-repositories-echo"
    d.mkdir(parents=True)
    _write_transcript(d / "a.jsonl", str(repo), [("Read", {"file_path": "/x"}, "ok")])

    def fake_run(cmd, **kwargs):   # the synthesis claude -p call
        return sp.CompletedProcess(
            cmd, returncode=0,
            stdout=(
                "- title: already fixed thing\n"
                "  friction_type: tool_failure\n"
                "  evidence:\n"
                "    source_ref: skills/x/SKILL.md:1\n"
                "    was_read: true\n"
                "    already_fixed_check: {ran: true, result: 'not-fixed on origin/main @abc'}\n"
                "    confidence: high\n"
                "    confidence_basis: opened the target and reproduced the friction\n"
            ), stderr="")

    monkeypatch.setattr(ar.subprocess, "run", fake_run)
    # Stub the gate's verdict so no real git/LLM runs; mark the only finding shipped.
    monkeypatch.setattr(
        ar, "verify_findings_against_source",
        lambda repo, findings, **kw: ([], [{**findings[0], "verification": {"verdict": "shipped"}}], None),
    )

    result = ar.run_review(str(repo), projects_dir=projects)
    assert result["findings"] == []
    assert len(result["dropped_findings"]) == 1
    assert result["dropped_findings"][0]["title"] == "already fixed thing"


def test_run_review_surfaces_verification_error(tmp_path, monkeypatch):
    """When the source gate can't run, run_review KEEPS the findings and records
    `verification_error` — a silent no-op gate (unverified findings looking verified)
    is the failure mode we're closing."""
    import subprocess as sp
    from orchestrator import agent_review as ar

    repo = tmp_path / "repositories" / "echo"
    (repo / "skills").mkdir(parents=True)
    projects = tmp_path / "projects"
    d = projects / "-Users-x-emdash-repositories-echo"
    d.mkdir(parents=True)
    _write_transcript(d / "a.jsonl", str(repo), [("Read", {"file_path": "/x"}, "ok")])

    def fake_run(cmd, **kwargs):
        return sp.CompletedProcess(
            cmd, returncode=0,
            stdout=(
                "- title: t\n"
                "  friction_type: tool_failure\n"
                "  evidence:\n"
                "    source_ref: skills/x/SKILL.md:1\n"
                "    was_read: true\n"
                "    already_fixed_check: {ran: true, result: 'not-fixed on origin/main @abc'}\n"
                "    confidence: high\n"
                "    confidence_basis: opened the target and reproduced the friction\n"
            ), stderr="")

    monkeypatch.setattr(ar.subprocess, "run", fake_run)
    monkeypatch.setattr(
        ar, "verify_findings_against_source",
        lambda repo, findings, **kw: (list(findings), [], "verify pass timed out after 300s"),
    )
    result = ar.run_review(str(repo), projects_dir=projects)
    assert len(result["findings"]) == 1          # kept (fail-open)
    assert result["dropped_findings"] == []
    assert "timed out" in result["verification_error"]


def test_run_review_no_verify_skips_gate(tmp_path, monkeypatch):
    """--no-verify (verify=False) returns synthesized findings untouched — the gate never runs."""
    import subprocess as sp
    from orchestrator import agent_review as ar

    repo = tmp_path / "repositories" / "echo"
    (repo / "skills").mkdir(parents=True)
    projects = tmp_path / "projects"
    d = projects / "-Users-x-emdash-repositories-echo"
    d.mkdir(parents=True)
    _write_transcript(d / "a.jsonl", str(repo), [("Read", {"file_path": "/x"}, "ok")])

    def fake_run(cmd, **kwargs):
        return sp.CompletedProcess(
            cmd, returncode=0,
            stdout=(
                "- title: t\n"
                "  friction_type: tool_failure\n"
                "  evidence:\n"
                "    source_ref: skills/x/SKILL.md:1\n"
                "    was_read: true\n"
                "    already_fixed_check: {ran: true, result: 'not-fixed on origin/main @abc'}\n"
                "    confidence: high\n"
                "    confidence_basis: opened the target and reproduced the friction\n"
            ), stderr="")

    monkeypatch.setattr(ar.subprocess, "run", fake_run)

    def boom(*a, **k):
        raise AssertionError("gate must not run when verify=False")

    monkeypatch.setattr(ar, "verify_findings_against_source", boom)
    result = ar.run_review(str(repo), projects_dir=projects, verify=False)
    assert len(result["findings"]) == 1
    assert result["dropped_findings"] == []


# --- Evidence-record validator (qualify_findings / _valid_evidence) ----------
from orchestrator.agent_review import qualify_findings, _valid_evidence

_GOOD_EV = {
    "source_ref": "skills/gsp-daily-briefing/SKILL.md:48",
    "was_read": True,
    "already_fixed_check": {"ran": True, "result": "not-fixed on origin/main @abc123"},
    "confidence": "high",
    "confidence_basis": "opened the target; friction reproduced at line 48",
}


def test_string_evidence_is_invalid():
    ok, reason = _valid_evidence("the corpus shows a dropped step")
    assert ok is False
    assert "record" in reason.lower() or "dict" in reason.lower()


def test_missing_already_fixed_check_is_invalid():
    ev = dict(_GOOD_EV); del ev["already_fixed_check"]
    ok, reason = _valid_evidence(ev)
    assert ok is False
    assert "already_fixed_check" in reason


def test_was_read_false_is_invalid():
    ev = dict(_GOOD_EV); ev["was_read"] = False
    ok, _ = _valid_evidence(ev)
    assert ok is False


def test_bad_confidence_value_is_invalid():
    ev = dict(_GOOD_EV); ev["confidence"] = "very-high"
    ok, _ = _valid_evidence(ev)
    assert ok is False


def test_full_record_is_valid():
    ok, reason = _valid_evidence(_GOOD_EV)
    assert ok is True and reason == ""


def test_qualify_splits_and_annotates():
    good = {"title": "t", "evidence": _GOOD_EV}
    bad = {"title": "u", "evidence": "just a string"}
    qualified, dropped = qualify_findings([good, bad])
    assert qualified == [good]
    assert len(dropped) == 1 and dropped[0]["title"] == "u"
    assert dropped[0]["_drop_reason"]  # non-empty


def test_non_dict_finding_is_dropped_with_reason():
    good = {"title": "t", "evidence": _GOOD_EV}
    findings = [good, "not a dict"]
    qualified, dropped = qualify_findings(findings)
    assert qualified == [good]
    assert len(dropped) == 1
    assert dropped[0].get("_drop_reason")  # non-empty
    assert len(qualified) + len(dropped) == len(findings)


def test_non_bool_ran_is_invalid():
    ev = dict(_GOOD_EV)
    ev["already_fixed_check"] = {"ran": "yes", "result": "x"}
    ok, reason = _valid_evidence(ev)
    assert ok is False
    assert "already_fixed_check" in reason


# --- Wire the validator into run_review + teach the prompt to emit the record ----------
from orchestrator.agent_review import build_review_prompt, _qualify_and_log
from pathlib import Path


def test_prompt_demands_structured_evidence(tmp_path: Path):
    prompt = build_review_prompt(tmp_path, corpus=[])
    assert "source_ref" in prompt
    assert "already_fixed_check" in prompt
    assert "was_read" in prompt
    assert "confidence_basis" in prompt


def test_qualify_and_log_drops_unqualified(capsys):
    good = {"title": "t", "evidence": _GOOD_EV}
    bad = {"title": "u", "evidence": "string"}
    kept = _qualify_and_log([good, bad], label="test-agent")
    assert kept == [good]
    err = capsys.readouterr().err
    assert "dropped" in err.lower() and "u" in err


# --- Structural-fix-only rail for invariant findings --------------------------
from orchestrator.agent_review import _is_invariant


def test_safety_override_is_invariant():
    assert _is_invariant({"friction_type": "safety_override", "title": "x"}) is True


def test_never_phrasing_is_invariant():
    assert _is_invariant({"title": "NEVER publish without approval", "recommendation": ""}) is True


def test_ordinary_finding_not_invariant():
    assert _is_invariant({"title": "tidy the digest", "recommendation": "reorder items"}) is False


def test_invariant_with_skill_edit_is_dropped():
    f = {"title": "NEVER post without a yes", "fix_kind": "skill_edit", "evidence": _GOOD_EV}
    qualified, dropped = qualify_findings([f])
    assert qualified == []
    assert "structural" in dropped[0]["_drop_reason"].lower()


def test_invariant_with_hook_rule_is_kept():
    f = {"title": "NEVER post without a yes", "fix_kind": "hook_rule", "evidence": _GOOD_EV}
    qualified, _ = qualify_findings([f])
    assert qualified == [f]


# --- M3: unhashable LLM output must fail-loud (drop), never crash ------------

def test_non_str_confidence_is_invalid_not_crash():
    ev = dict(_GOOD_EV)
    ev["confidence"] = ["high"]
    ok, reason = _valid_evidence(ev)
    assert ok is False
    assert reason


def test_invariant_with_unhashable_fix_kind_is_dropped_not_crash():
    f = {"title": "NEVER post without a yes", "fix_kind": ["hook_rule"], "evidence": _GOOD_EV}
    qualified, dropped = qualify_findings([f])
    assert qualified == []
    assert len(dropped) == 1


# --- over_claim / verify_late corpus detectors --------------------------------
# Entries here use the REAL transcript shape (type/message.content blocks) that
# read_transcript produces and human_corrections/extract_tool_calls consume —
# NOT a simplified {"role","text","tools"} shape. See _write_transcript above and
# test_human_corrections_catches_safety_override_and_confusion for the same convention.
from orchestrator.agent_review import overclaim_signals


def test_overclaim_types_registered():
    assert "over_claim" in FRICTION_TYPES
    assert "verify_late" in FRICTION_TYPES


def test_bare_completion_claim_flagged():
    # assistant asserts "Verified" with no tool_use block in the same message.
    entries = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Verified live — the filter is applied."},
        ]}},
    ]
    sigs = overclaim_signals(entries)
    assert any(s["type"] == "over_claim" for s in sigs)
    assert sigs[0]["turn"] == 0
    assert "Verified" in sigs[0]["evidence"]


def test_claim_backed_by_tool_not_flagged():
    # same assistant message also carries a tool_use block -> not an over_claim.
    entries = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Applied the filter."},
            {"type": "tool_use", "id": "t0", "name": "Bash", "input": {"command": "echo ok"}},
        ]}},
    ]
    sigs = overclaim_signals(entries)
    assert all(s["type"] != "over_claim" for s in sigs)


def test_claim_with_no_completion_verb_not_flagged():
    entries = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me look into this next."},
        ]}},
    ]
    assert overclaim_signals(entries) == []


def test_user_turns_are_not_scanned_for_overclaims():
    # human text containing a completion verb must never be mistaken for the agent's own claim.
    entries = [{"type": "user", "message": {"content": "is this shipped yet?"}}]
    assert overclaim_signals(entries) == []


def test_claim_substantiated_by_tool_use_earlier_in_same_turn_not_flagged():
    # Claude Code routinely splits work across entries: an assistant tool_use entry,
    # then a user tool_result entry, then a SEPARATE assistant entry with the wrap-up
    # text. The tool_use substantiates the claim across entries within the same turn
    # (no genuine human message resets the turn in between) -> must NOT be flagged.
    entries = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t0", "name": "Bash", "input": {"command": "pytest -q"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t0", "content": "43 passed"},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Done and verified."},
        ]}},
    ]
    sigs = overclaim_signals(entries)
    assert all(s["type"] != "over_claim" for s in sigs)


def test_claim_with_no_tool_use_anywhere_in_turn_still_flagged():
    # A genuine human message resets the turn boundary. The tool_use in the FIRST
    # turn must not substantiate a bare claim made in a later turn with no tool_use
    # of its own.
    entries = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t0", "name": "Bash", "input": {"command": "pytest -q"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t0", "content": "43 passed"},
        ]}},
        {"type": "user", "message": {"content": "thanks, now do the next one"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Done and verified."},
        ]}},
    ]
    sigs = overclaim_signals(entries)
    assert any(s["type"] == "over_claim" for s in sigs)
    assert sigs[0]["turn"] == 3


def test_friction_signals_wires_overclaims(tmp_path):
    t = tmp_path / "turn.jsonl"
    lines = [
        {"type": "assistant", "cwd": str(tmp_path), "message": {"content": [
            {"type": "text", "text": "Done — fixed the bug."},
        ]}},
    ]
    t.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    s = friction_signals(t)
    assert "overclaims" in s
    assert any(o["type"] == "over_claim" for o in s["overclaims"])


# --- CLI: `agent-review --qualify-file` routes external findings through qualify_findings ----
import json as _json
import yaml
from click.testing import CliRunner
from orchestrator.cli import main


def _write_qualify_fixture(tmp_path):
    good = {"title": "good finding", "evidence": _GOOD_EV}
    bad = {"title": "bad finding", "evidence": "just a string"}
    p = tmp_path / "findings.yaml"
    p.write_text(yaml.safe_dump([good, bad]))
    return p


def test_qualify_file_splits_good_and_bad(tmp_path):
    p = _write_qualify_fixture(tmp_path)
    r = CliRunner().invoke(main, ["agent-review", "--qualify-file", str(p)])
    assert r.exit_code == 0, r.output
    assert "Qualified (1)" in r.output
    assert "Dropped (1)" in r.output
    assert "record" in r.output.lower() or "dict" in r.output.lower()


def test_qualify_file_json_output(tmp_path):
    p = _write_qualify_fixture(tmp_path)
    r = CliRunner().invoke(main, ["agent-review", "--qualify-file", str(p), "--json-output"])
    assert r.exit_code == 0, r.output
    data = _json.loads(r.output)
    assert len(data["qualified"]) == 1
    assert len(data["dropped"]) == 1


def test_no_agent_no_qualify_errors():
    r = CliRunner().invoke(main, ["agent-review"])
    assert r.exit_code != 0
    assert "--qualify-file" in r.output


def test_qualify_file_malformed_yaml_is_clean_error(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(":\n  - [unclosed")
    r = CliRunner().invoke(main, ["agent-review", "--qualify-file", str(p)])
    assert r.exit_code != 0
    assert isinstance(r.exception, SystemExit)
    assert "could not read qualify-file" in r.output or "Error:" in r.output
    assert "Traceback" not in r.output


def test_agent_review_normal_path_still_reachable_without_qualify_file():
    # Regression guard: making AGENT optional (for --qualify-file) must not break the
    # normal `agent-review <slug>` path. A nonexistent slug should get PAST the
    # "provide an AGENT slug" guard and fail later on repo resolution instead.
    r = CliRunner().invoke(main, ["agent-review", "nonexistent-slug-xyz"])
    assert r.exit_code != 0
    assert "provide an AGENT slug" not in r.output
    assert "could not resolve agent repo" in r.output.lower()


# --- Timeout / error-shape contract (live cron: synthesis timed out, the error came back
# --- a bare int, and the consumer died with `object of type 'int' has no len()`) ---------

def test_run_review_timeout_returns_string_error_and_empty_findings(tmp_path, monkeypatch):
    """A timed-out synthesis pass must return a WELL-FORMED result: a descriptive STRING
    error naming the timeout, findings == [], and no exception out of run_review."""
    import subprocess as sp
    from orchestrator import agent_review as ar

    repo = tmp_path / "repositories" / "echo"
    (repo / "skills").mkdir(parents=True)
    projects = tmp_path / "projects"
    d = projects / "-Users-x-emdash-repositories-echo"
    d.mkdir(parents=True)
    _write_transcript(d / "a.jsonl", str(repo), [("Read", {"file_path": "/x"}, "ok")])

    def fake_run(cmd, **kwargs):
        raise sp.TimeoutExpired(cmd, kwargs.get("timeout", 180))

    monkeypatch.setattr(ar.subprocess, "run", fake_run)
    result = ar.run_review(str(repo), projects_dir=projects, timeout=45)

    assert isinstance(result["error"], str)
    assert "timed out" in result["error"].lower()
    assert "45" in result["error"]                 # the actual budget, not a vague message
    assert result["findings"] == []
    assert result["dropped_findings"] == []


def test_run_review_error_is_never_non_string(tmp_path, monkeypatch):
    """Every failure path yields a STRING error — even when the underlying failure value
    is a bare int (returncode) or a non-str exception payload."""
    import subprocess as sp
    from orchestrator import agent_review as ar

    repo = tmp_path / "repositories" / "echo"
    (repo / "skills").mkdir(parents=True)
    projects = tmp_path / "projects"
    d = projects / "-Users-x-emdash-repositories-echo"
    d.mkdir(parents=True)
    _write_transcript(d / "a.jsonl", str(repo), [("Read", {"file_path": "/x"}, "ok")])

    # (a) non-zero exit with EMPTY stdout+stderr — the shape that used to leave only a
    #     bare returncode to report.
    monkeypatch.setattr(ar.subprocess, "run",
                        lambda cmd, **kw: sp.CompletedProcess(cmd, 137, stdout="", stderr=""))
    result = ar.run_review(str(repo), projects_dir=projects)
    assert isinstance(result["error"], str)
    assert "137" in result["error"]
    assert len(result["error"]) > 0          # consumers do len() on this
    assert result["findings"] == []

    # (b) an OSError from the spawn itself (e.g. `claude` not on PATH under cron)
    def boom(cmd, **kw):
        raise OSError(2, "No such file or directory: 'claude'")

    monkeypatch.setattr(ar.subprocess, "run", boom)
    result = ar.run_review(str(repo), projects_dir=projects)
    assert isinstance(result["error"], str)
    assert result["findings"] == []

    # (c) unresolvable agent still returns the full result shape, not a bare {"error": ...}
    result = ar.run_review("no-such-agent-xyz", projects_dir=projects)
    assert isinstance(result["error"], str)
    assert result["findings"] == [] and result["dropped_findings"] == []
    assert result["turns"] == 0


def test_run_review_passes_timeout_to_subprocess(tmp_path, monkeypatch):
    """--timeout is real: the value reaches subprocess.run's timeout kwarg."""
    import subprocess as sp
    from orchestrator import agent_review as ar

    repo = tmp_path / "repositories" / "echo"
    (repo / "skills").mkdir(parents=True)
    projects = tmp_path / "projects"
    d = projects / "-Users-x-emdash-repositories-echo"
    d.mkdir(parents=True)
    _write_transcript(d / "a.jsonl", str(repo), [("Read", {"file_path": "/x"}, "ok")])

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return sp.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(ar.subprocess, "run", fake_run)
    ar.run_review(str(repo), projects_dir=projects, timeout=17)
    assert seen["timeout"] == 17
    # default is the module constant, not a magic number re-typed at the call site
    ar.run_review(str(repo), projects_dir=projects)
    assert seen["timeout"] == ar.SYNTHESIS_TIMEOUT


def test_agent_review_cli_accepts_timeout_option():
    r = CliRunner().invoke(main, ["agent-review", "--help"])
    assert r.exit_code == 0, r.output
    assert "--timeout" in r.output
    r2 = CliRunner().invoke(main, ["agent-review", "--timeout", "5", "--no-llm", "nope-not-an-agent"])
    assert "no such option" not in r2.output.lower()
