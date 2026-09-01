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
    # A turn (it loaded skills/turn) that does none of the expected UNCONDITIONAL steps.
    _write_transcript(t, str(tmp_path), [
        ("Read", {"file_path": "/repo/skills/turn/SKILL.md"}, "Hal's turn loop..."),
        ("Read", {"file_path": "/x"}, "ok"),
    ])
    gaps = set(friction_signals(t)["checklist_gaps"])
    assert {"preflight", "self-review", "skill-self-check"} <= gaps
    # `workspace-refresh` is CONDITIONAL: nothing here asked for a publish and the turn is
    # not in auto mode, so turn.md's "publishing is MANUAL" makes not-publishing correct.
    assert "workspace-refresh" not in gaps


def test_manual_turn_not_penalized_for_skipping_publish(tmp_path):
    """The regression that dispatched work: a compliant manual turn scored as failing.

    turn.md Step 4 — "Publishing to canopy-web is MANUAL ... ONLY when the human explicitly
    asks". 8 of eva's 10 graded turns tripped this in the 26h to 2026-08-19, and the false
    signal was dispatched at eva as a fix order.
    """
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Read", {"file_path": "/repo/skills/turn/SKILL.md"}, "eva's turn loop"),
        ("Bash", {"command": "bin/eva-preflight"}, "ready"),
        ("Bash", {"command": "echo self-review + skill-self-check done"}, "ok"),
    ])
    assert "workspace-refresh" not in friction_signals(t)["checklist_gaps"]


def test_auto_mode_turn_owes_a_turn_record(tmp_path):
    """Auto mode is the real signal the conditional preserves.

    turn.md Turn mode §5 makes `canopy agent turn` an AUTOMATIC close step in auto mode —
    it is the after-the-fact audit trail for sends that went out unattended. Skipping it
    there is a genuine gap.
    """
    t = tmp_path / "turn.jsonl"
    lines = []
    t.write_text("")
    _write_transcript(t, str(tmp_path), [
        ("Read", {"file_path": "/repo/skills/turn/SKILL.md"}, "turn loop"),
    ])
    # The agent states the mode in its opening, as turn.md requires.
    with open(t, "a") as fh:
        fh.write(json.dumps({
            "type": "assistant", "cwd": str(tmp_path),
            "message": {"content": [
                {"type": "text", "text": "Running in auto mode for this turn."},
            ]},
        }) + "\n")
    assert "workspace-refresh" in friction_signals(t)["checklist_gaps"]


def test_human_asking_to_publish_makes_it_expected(tmp_path):
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Read", {"file_path": "/repo/skills/turn/SKILL.md"}, "turn loop"),
    ])
    with open(t, "a") as fh:
        # a genuine human message: message.content is a plain str, not a block list
        fh.write(json.dumps({
            "type": "user", "cwd": str(tmp_path),
            "message": {"content": "do a turn, then publish it to the board please"},
        }) + "\n")
    assert "workspace-refresh" in friction_signals(t)["checklist_gaps"]


def test_agent_saying_it_did_not_publish_is_not_a_trigger(tmp_path):
    """The false positive the first cut of this check shipped with.

    eva's 2026-08-19 closeout said "nothing published to canopy-web (not asked)" — a correct
    report of correctly SKIPPING the step — and a negation-blind grep over the agent's own
    prose read it as the step being owed. "The human asked" is settled by the human's words.
    """
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Read", {"file_path": "/repo/skills/turn/SKILL.md"}, "turn loop"),
    ])
    with open(t, "a") as fh:
        fh.write(json.dumps({
            "type": "assistant", "cwd": str(tmp_path),
            "message": {"content": [{"type": "text", "text": (
                "Fix shipped and surfaced to Beth with links. Nothing published to "
                "canopy-web (not asked). Not running in auto mode.")}]},
        }) + "\n")
    assert "workspace-refresh" not in friction_signals(t)["checklist_gaps"]


def test_investigating_the_publish_step_does_not_trigger_it(tmp_path):
    """Triggers read intent, not tool inputs — otherwise a session that merely greps for
    `agent-publish` triggers the step and then satisfies it with the same string."""
    t = tmp_path / "turn.jsonl"
    _write_transcript(t, str(tmp_path), [
        ("Read", {"file_path": "/repo/skills/turn/SKILL.md"}, "turn loop"),
        ("Bash", {"command": "grep -rn 'agent-publish|turn-close' src/"}, "3 hits"),
    ])
    assert "workspace-refresh" not in friction_signals(t)["checklist_gaps"]


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
    with open(t, "a") as fh:
        # a genuine human message: message.content is a plain str, not a block list
        fh.write(json.dumps({
            "type": "user", "cwd": str(tmp_path),
            "message": {"content": "do a turn and publish to the board"},
        }) + "\n")
    gaps = set(friction_signals(t)["checklist_gaps"])
    assert "preflight" not in gaps          # preflight marker present
    # asked for -> graded; `canopy agent-publish` ran -> credited, not a gap
    assert "workspace-refresh" not in gaps


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


def _worktree_case(tmp_path, slug, siblings, worktree_root, session_dir="emdash-x-0901-abc"):
    """Lay out repositories/<siblings> + worktrees/<worktree_root>/<session_dir>, write one
    transcript whose cwd is that session dir, and return (repo, projects)."""
    root = tmp_path / "emdash"
    for name in siblings:
        (root / "repositories" / name).mkdir(parents=True, exist_ok=True)
    cwd = root / "worktrees" / worktree_root / session_dir
    cwd.mkdir(parents=True)
    projects = tmp_path / "projects"
    d = projects / ("-" + str(cwd).strip("/").replace("/", "-"))
    d.mkdir(parents=True)
    _write_transcript(d / "a.jsonl", str(cwd), [("Read", {"file_path": "/x"}, "ok")])
    return root / "repositories" / slug, projects


def test_find_turn_transcripts_matches_hash_suffixed_worktree_root(tmp_path):
    """emdash creates `<slug>-<hash>/` worktree containers, so requiring a bare
    `/worktrees/<slug>/` dropped ~93% of the fleet's turns while still reporting
    full coverage. See the `ace-web` sibling test for the other half of this."""
    repo, projects = _worktree_case(tmp_path, "ace", ["ace", "ace-web"], "ace-c89535f9")
    found = find_turn_transcripts(repo, hours=99999, projects_dir=projects)
    assert [f.name for f in found] == ["a.jsonl"]


def test_find_turn_transcripts_matches_named_worktree_root(tmp_path):
    """Humans create named containers too (`ace-1813`, `canopy-ddd-identity`). Those are
    the agent's worktrees, not another repo's."""
    repo, projects = _worktree_case(tmp_path, "ace", ["ace", "ace-web"], "ace-1813")
    found = find_turn_transcripts(repo, hours=99999, projects_dir=projects)
    assert [f.name for f in found] == ["a.jsonl"]


def test_find_turn_transcripts_does_not_swallow_sibling_repo_worktrees(tmp_path):
    """GUARDRAIL: `ace-web` is a real sibling repo. Widening the matcher to "any dir
    containing the slug" pulls its sessions into `ace`'s corpus — trading a silent
    under-count for a silent over-count that corrupts a different agent's review."""
    repo, projects = _worktree_case(tmp_path, "ace", ["ace", "ace-web"], "ace-web")
    assert find_turn_transcripts(repo, hours=99999, projects_dir=projects) == []

    repo2, projects2 = _worktree_case(
        tmp_path / "b", "ace", ["ace", "ace-web"], "ace-web-canopy-cutover")
    assert find_turn_transcripts(repo2, hours=99999, projects_dir=projects2) == []


def test_worktree_root_falls_back_to_hash_shape_without_sibling_list(tmp_path):
    """With no readable repositories/ dir to disambiguate against, accept only the shape
    emdash actually emits — never any suffix, which would swallow sibling repos."""
    from orchestrator.agent_review import _worktree_root_is_agents
    none = frozenset()
    assert _worktree_root_is_agents("ace-c89535f9", "ace", none)
    assert not _worktree_root_is_agents("ace-web", "ace", none)
    assert not _worktree_root_is_agents("ace-1813", "ace", none)  # 4 hex chars: too short
    assert _worktree_root_is_agents("ace", "ace", none)


def test_scan_reports_considered_so_a_blind_run_cannot_read_as_clean(tmp_path):
    """A run that examines transcripts and attributes none must be able to SAY so —
    `whole-corpus` with `unreadable: []` over an empty collection is a false all-clear."""
    from orchestrator.agent_review import scan_turn_transcripts
    repo, projects = _worktree_case(tmp_path, "ace", ["ace", "ace-web"], "ace-web")
    found, considered = scan_turn_transcripts(repo, hours=99999, projects_dir=projects)
    assert found == [] and considered == 1


def test_run_review_marks_an_empty_scan_blind(tmp_path):
    """turns == 0 with candidates examined is `blind`, not `whole-corpus`."""
    from orchestrator import agent_review as ar
    repo, projects = _worktree_case(tmp_path, "ace", ["ace", "ace-web"], "ace-web")
    result = ar.run_review(str(repo), projects_dir=projects, use_llm=False)
    assert result["turns"] == 0
    assert result["corpus"]["confidence"] == "blind"
    assert result["corpus"]["considered"] == 1
    assert result["corpus"]["matched"] == 0


def test_run_review_keeps_whole_corpus_when_there_was_nothing_to_find(tmp_path):
    """The other side of the same coin: a genuinely quiet window is NOT blind."""
    from orchestrator import agent_review as ar
    (tmp_path / "emdash" / "repositories" / "ace").mkdir(parents=True)
    projects = tmp_path / "projects"
    projects.mkdir()
    result = ar.run_review(str(tmp_path / "emdash" / "repositories" / "ace"),
                           projects_dir=projects, use_llm=False)
    assert result["turns"] == 0
    assert result["corpus"]["confidence"] == "whole-corpus"
    assert result["corpus"]["considered"] == 0


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


def test_bad_confidence_value_no_longer_fails_the_evidence_gate():
    """`confidence` is a LABEL, not evidence — _valid_evidence stops gating it so
    qualify_findings can repair it. The basis (the justification) is still gated."""
    ev = dict(_GOOD_EV); ev["confidence"] = "very-high"
    ok, _ = _valid_evidence(ev)
    assert ok is True


def test_missing_confidence_basis_is_still_invalid():
    ev = dict(_GOOD_EV); del ev["confidence_basis"]
    ok, reason = _valid_evidence(ev)
    assert ok is False
    assert "confidence_basis" in reason


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


def test_invariant_with_skill_edit_is_coerced_not_dropped():
    """The rail used to DISCARD an evidence-valid finding over a label the engine's
    own prompt had already asked for. Repair the routing; keep the evidence."""
    f = {"title": "NEVER post without a yes", "fix_kind": "skill_edit", "evidence": _GOOD_EV}
    qualified, dropped = qualify_findings([f])
    assert dropped == []
    assert len(qualified) == 1
    assert qualified[0]["fix_kind"] == "hook_rule"
    assert qualified[0]["_fix_kind_coerced"]["from"] == "skill_edit"
    assert qualified[0]["_fix_kind_coerced"]["to"] == "hook_rule"


def test_coercion_preserves_the_models_own_recommendation():
    """The triager needs the model's intent next to the correction — coercing the
    label must not quietly rewrite what the finding actually says to do."""
    f = {"title": "ALWAYS verify the link before sending", "fix_kind": "claude_update",
         "target": "CLAUDE.md", "recommendation": "add a link-verification step",
         "evidence": _GOOD_EV}
    qualified, _ = qualify_findings([f])
    assert qualified[0]["recommendation"] == "add a link-verification step"
    assert qualified[0]["target"] == "CLAUDE.md"


def test_schema_target_coerces_to_schema_validator():
    f = {"title": "NEVER accept a finding without evidence", "fix_kind": "skill_edit",
         "target": "config/findings.schema.json", "evidence": _GOOD_EV}
    qualified, _ = qualify_findings([f])
    assert qualified[0]["fix_kind"] == "schema_validator"


def test_gating_json_target_coerces_to_hook_rule_not_schema_validator():
    """`config/gating.json` is a hook-rule config, not a schema — matching on the
    .json extension instead of the word 'schema' would misroute the fleet's most
    common invariant target."""
    f = {"title": "NEVER send raw email", "fix_kind": "skill_edit",
         "target": "config/gating.json", "evidence": _GOOD_EV}
    qualified, _ = qualify_findings([f])
    assert qualified[0]["fix_kind"] == "hook_rule"


def test_ordinary_finding_keeps_its_fix_kind_untouched():
    """Only INVARIANT findings are steered. A normal improvement is left alone."""
    f = {"title": "tidy the digest", "fix_kind": "skill_edit",
         "recommendation": "reorder items", "evidence": _GOOD_EV}
    qualified, _ = qualify_findings([f])
    assert qualified[0]["fix_kind"] == "skill_edit"
    assert "_fix_kind_coerced" not in qualified[0]


def test_evidence_gate_is_still_a_hard_drop_even_for_an_invariant():
    """Load-bearing: unevidenced is UNFIXABLE, wrongly-routed is not. An invariant
    with bad evidence must not be rescued by the coercion path."""
    f = {"title": "NEVER post without a yes", "fix_kind": "skill_edit",
         "evidence": "just a string"}
    qualified, dropped = qualify_findings([f])
    assert qualified == []
    assert len(dropped) == 1
    assert "_fix_kind_coerced" not in dropped[0]


def test_qualify_and_log_reports_the_coercion(capsys):
    """A silent repair is just a different kind of quiet."""
    f = {"title": "NEVER post without a yes", "fix_kind": "skill_edit", "evidence": _GOOD_EV}
    kept = _qualify_and_log([f], label="test-agent")
    assert len(kept) == 1
    err = capsys.readouterr().err
    assert "coerced" in err.lower() and "skill_edit" in err and "hook_rule" in err


def test_invariant_with_hook_rule_is_kept():
    f = {"title": "NEVER post without a yes", "fix_kind": "hook_rule", "evidence": _GOOD_EV}
    qualified, _ = qualify_findings([f])
    assert qualified == [f]
    assert "_fix_kind_coerced" not in f  # already structural — nothing to correct


# --- M3: unhashable LLM output must fail-loud (drop), never crash ------------

def test_unhashable_confidence_does_not_crash_and_is_dropped():
    """M3 invariant preserved: a list where a level belongs must fail-loud, never
    raise. It is unmappable, so with no finding-level sibling it drops."""
    f = {"title": "t", "evidence": dict(_GOOD_EV, confidence=["high"])}
    qualified, dropped = qualify_findings([f])
    assert qualified == []
    assert len(dropped) == 1 and dropped[0]["_drop_reason"]


def test_invariant_with_unhashable_fix_kind_is_coerced_not_crash():
    """M3 still holds: unhashable LLM output must never crash the qualifier. It now
    lands in the coercion path (a non-str fix_kind is by definition non-structural)
    rather than the drop path, and the original is preserved in the annotation."""
    f = {"title": "NEVER post without a yes", "fix_kind": ["hook_rule"], "evidence": _GOOD_EV}
    qualified, dropped = qualify_findings([f])
    assert dropped == []
    assert len(qualified) == 1
    assert qualified[0]["fix_kind"] == "hook_rule"
    assert qualified[0]["_fix_kind_coerced"]["from"] == ["hook_rule"]


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


# --- Task: agent-review scans EVERY readable session source, not one home ----
#
# `run_review` scanned `CLAUDE_PROJECTS = Path.home()/".claude"/"projects"` --
# the CURRENT user's home, one place. JJ alternates two macOS accounts
# (jjackson + acedimagi) on rate-limit, so an agent's real corpus is routinely
# split across both. Measured 2026-07-28: `canopy agent-review ace --hours 48`
# reported 2 turns from jjackson while acedimagi held the sessions the review
# was actually about. Findings drawn from half a corpus are worse than no
# findings -- they read as complete.
#
# `agent_coverage.coverage_report` already fixed exactly this via the
# `session_sources` seam (see session_sources.py's docstring for the
# hal/architect regression). These tests hold `run_review` to the same
# contract, reusing that seam rather than growing a second discovery path.

def test_run_review_merges_transcripts_across_sources(tmp_path, monkeypatch):
    """A turn that happened only on the SECOND account must appear in the corpus."""
    from orchestrator import agent_review as ar
    from orchestrator.session_sources import SessionSource

    repo = tmp_path / "repositories" / "ace"
    (repo / "skills").mkdir(parents=True)

    jj_root = tmp_path / "jjackson_home" / ".claude" / "projects"
    ace_root = tmp_path / "acedimagi_home" / ".claude" / "projects"

    d1 = jj_root / "-Users-jjackson-emdash-repositories-ace"
    d1.mkdir(parents=True)
    _write_transcript(d1 / "a.jsonl", str(repo), [("Read", {"file_path": "/x"}, "ok")])

    # The other account, in a worktree checkout — the shape that actually occurs.
    ace_cwd = f"{tmp_path}/acedimagi_home/emdash/worktrees/ace/emdash/run-spark-abc"
    d2 = ace_root / "-Users-acedimagi-emdash-worktrees-ace-emdash-run-spark-abc"
    d2.mkdir(parents=True)
    _write_transcript(d2 / "b.jsonl", ace_cwd, [("Read", {"file_path": "/y"}, "ok")])

    monkeypatch.setattr(
        "orchestrator.agent_review.session_sources",
        lambda *a, **k: [
            SessionSource(name="local:jjackson", kind="local", location=str(jj_root), readable=True),
            SessionSource(name="local:acedimagi", kind="local", location=str(ace_root), readable=True),
        ],
    )

    res = ar.run_review(str(repo), use_llm=False)

    assert res["turns"] == 2, "the second account's turn was dropped"
    assert res["corpus"]["confidence"] == "whole-corpus"
    assert sorted(res["corpus"]["sources"]) == ["local:acedimagi", "local:jjackson"]


def test_run_review_flags_half_blind_when_a_source_is_unreadable(tmp_path, monkeypatch):
    """Degrade LOUD. An unreadable account means findings may be incomplete, and the
    caller has to be able to see that rather than infer it."""
    from orchestrator import agent_review as ar
    from orchestrator.session_sources import SessionSource

    repo = tmp_path / "repositories" / "ace"
    (repo / "skills").mkdir(parents=True)
    jj_root = tmp_path / "jjackson_home" / ".claude" / "projects"
    (jj_root / "-Users-jjackson-emdash-repositories-ace").mkdir(parents=True)

    monkeypatch.setattr(
        "orchestrator.agent_review.session_sources",
        lambda *a, **k: [
            SessionSource(name="local:jjackson", kind="local", location=str(jj_root), readable=True),
            SessionSource(name="local:acedimagi", kind="local",
                          location="/Users/acedimagi/.claude/projects",
                          readable=False, reason="not readable"),
        ],
    )

    res = ar.run_review(str(repo), use_llm=False)
    assert res["corpus"]["confidence"] == "half-blind"
    assert res["corpus"]["unreadable"] == ["local:acedimagi"]


def test_run_review_explicit_projects_dir_still_scans_only_that_dir(tmp_path, monkeypatch):
    """The injectable override stays injectable — tests and callers that name one
    dir must not silently fan out across every account on the machine."""
    from orchestrator import agent_review as ar
    from orchestrator.session_sources import SessionSource

    repo = tmp_path / "repositories" / "ace"
    (repo / "skills").mkdir(parents=True)
    only = tmp_path / "only" / "projects"
    d = only / "-Users-x-emdash-repositories-ace"
    d.mkdir(parents=True)
    _write_transcript(d / "a.jsonl", str(repo), [("Read", {"file_path": "/x"}, "ok")])

    other = tmp_path / "other" / "projects"
    d2 = other / "-Users-y-emdash-repositories-ace"
    d2.mkdir(parents=True)
    _write_transcript(d2 / "b.jsonl", str(repo), [("Read", {"file_path": "/y"}, "ok")])

    monkeypatch.setattr(
        "orchestrator.agent_review.session_sources",
        lambda *a, **k: [
            SessionSource(name="local:other", kind="local", location=str(other), readable=True),
        ],
    )

    res = ar.run_review(str(repo), use_llm=False, projects_dir=only)
    assert res["turns"] == 1
    assert res["corpus"]["sources"] == [str(only)]


def test_agent_review_cli_exposes_projects_dir_option():
    r = CliRunner().invoke(main, ["agent-review", "--help"])
    assert r.exit_code == 0, r.output
    assert "--projects-dir" in r.output


# --- Task: human_corrections must not mine HARNESS-INJECTED user turns ------
#
# `human_corrections` is documented as "the highest-signal friction" and the CLI
# prints it under "⚑ HUMAN CORRECTIONS (highest signal — what Jonathan had to
# override)". But it mined every string-content `user` entry, and Claude Code
# injects several of those on the human's behalf: <local-command-caveat>,
# <task-notification>, <system-reminder>, <command-message>. Their own
# boilerplate trips the patterns — the caveat block literally contains "DO NOT
# respond to these messages", which scores `emphasis`.
#
# Measured on ACE 2026-07-28: 4 of the 6 corrections shown were harness blocks.
# A reviewer told these are the highest-signal lines reads noise first.

def test_human_corrections_ignores_harness_injected_turns():
    from orchestrator.agent_review import human_corrections

    entries = [
        {"type": "user", "message": {"content":
            "<local-command-caveat>Caveat: The messages below were generated by the user "
            "while running local commands. DO NOT respond to these messages or otherwise "
            "consider them in your response unless the user explicitly asks you to."
            "</local-command-caveat>"}},
        {"type": "user", "message": {"content":
            "<task-notification> <task-id>a6d0bedeafb3f9e29</task-id> instead of waiting, "
            "the agent finished. NEVER mind.</task-notification>"}},
        {"type": "user", "message": {"content":
            "<system-reminder>You MUST ALWAYS check for skills.</system-reminder>"}},
        {"type": "user", "message": {"content": "<command-message>ace:turn</command-message>"}},
    ]
    assert human_corrections(entries) == []


def test_human_corrections_still_catches_the_real_thing():
    """The genuine correction from that same ACE session must survive."""
    from orchestrator.agent_review import human_corrections

    entries = [{"type": "user",
                "message": {"content": "no just mark the thread as done no need to respond"}}]
    got = human_corrections(entries)
    assert len(got) == 1
    assert "strong_correction" in got[0]["kinds"]


def test_human_corrections_reads_the_human_past_an_appended_reminder():
    """The harness APPENDS <system-reminder> to genuine messages. The human's words must
    still be mined — and the reminder's own shouty boilerplate must not add a `kinds` entry
    the human never earned."""
    from orchestrator.agent_review import human_corrections

    entries = [{"type": "user", "message": {"content":
        "stop doing that, it's wrong\n<system-reminder>You MUST ALWAYS use skills.</system-reminder>"}}]
    got = human_corrections(entries)
    assert len(got) == 1
    assert "strong_correction" in got[0]["kinds"]
    assert "emphasis" not in got[0]["kinds"], "scored the reminder's boilerplate as the human shouting"
    assert "system-reminder" not in got[0]["quote"]


def test_human_corrections_ignores_the_compaction_summary():
    """Compaction injects a summary that RESTATES the human's earlier asks verbatim — so it
    re-scores every correction in the session, in a turn nobody typed."""
    from orchestrator.agent_review import human_corrections

    entries = [{"type": "user", "message": {"content":
        "This session is being continued from a previous conversation that ran out of context. "
        "The summary below covers the earlier portion.\n\nSummary:\n1. Primary Request:\n"
        "   - stop doing that, it's wrong, do it instead of the other way"}}]
    assert human_corrections(entries) == []


# --- #416: a call's OUTCOME is recorded, not inferred -------------------------
#
# Every tool_result block carries `is_error`. The extractor used to drop it and
# re-derive failure by grepping the result prose, which made the SUBJECT of a
# result indistinguishable from its OUTCOME. These lock in the real 2026-07-28
# false positives, measured over 36 ACE turns.

def _write_transcript_with_status(path, cwd, calls):
    """calls: (tool, input_dict, result_str, is_error) — is_error None omits the flag,
    reproducing a transcript written before the harness recorded it."""
    lines = []
    for i, (tool, inp, result, is_error) in enumerate(calls):
        tid = f"t{i}"
        lines.append({
            "type": "assistant", "cwd": cwd,
            "message": {"content": [
                {"type": "tool_use", "id": tid, "name": tool, "input": inp},
            ]},
        })
        block = {"type": "tool_result", "tool_use_id": tid, "content": result}
        if is_error is not None:
            block["is_error"] = is_error
        lines.append({"type": "user", "message": {"content": [block]}})
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def test_succeeding_calls_are_not_friction_however_their_output_reads(tmp_path):
    """The four shapes actually miscounted on ACE: a PASS line naming OAuth, a zero-exit
    build whose URL held `-404-`, a 200 response, and reading a file about auth."""
    t = tmp_path / "turn.jsonl"
    _write_transcript_with_status(t, str(tmp_path), [
        ("Bash", {"command": "ace doctor"},
         "PASS cchq_connect_features: COMMCARE_CONNECT flag enabled on 'connect-ace-prod', "
         "OAuth connection to connect.dimagi.com configured", False),
        ("Bash", {"command": "npm run build"},
         "--- tsc exit: 0 --- commcare_test-both-404-1785178018219-qsyzi4/app.apk", False),
        ("Bash", {"command": "curl -s $URL"}, '{"status": 200, "size_bytes": 7978}', False),
        ("Read", {"file_path": "apps/api/auth.py"},
         '"""Session-cookie + Bearer-token auth. Matches DRF credentials handling."""', False),
    ])
    s = friction_signals(t)
    assert s["failures"] == [], f"a succeeding call was counted as a failure: {s['failures']}"
    assert s["auth_friction"] == [], f"a succeeding call was counted as auth friction: {s['auth_friction']}"


def test_harness_error_flag_is_believed_even_when_the_text_reads_clean(tmp_path):
    """The complement: no marker fires, but the harness said it failed."""
    t = tmp_path / "turn.jsonl"
    _write_transcript_with_status(t, str(tmp_path), [
        ("Bash", {"command": "some-tool"}, "nothing matched the given pattern", True),
    ])
    assert len(friction_signals(t)["failures"]) == 1


def test_a_zero_exit_traceback_is_still_a_failure(tmp_path):
    """`cmd || true` exits 0 while holding a real stack trace — trusting is_error
    blindly would drop it, so strong markers survive a success verdict."""
    t = tmp_path / "turn.jsonl"
    _write_transcript_with_status(t, str(tmp_path), [
        ("Bash", {"command": "pytest -q || true"},
         'Traceback (most recent call last):\n  File "x.py", line 3\nValueError', False),
    ])
    assert len(friction_signals(t)["failures"]) == 1


def test_transcripts_without_the_flag_review_exactly_as_before(tmp_path):
    """Older sessions predate is_error; they must fall through to the markers unchanged."""
    t = tmp_path / "turn.jsonl"
    _write_transcript_with_status(t, str(tmp_path), [
        ("Bash", {"command": "gog gmail search"}, "People API has not been used... 403", None),
    ])
    s = friction_signals(t)
    assert len(s["failures"]) == 1
    assert len(s["auth_friction"]) == 1


# --- `--json-output` must not report a failed run as a clean bill of health ---

def _invoke_agent_review_json(monkeypatch, result):
    """Run `canopy agent-review --json-output` with run_review stubbed to `result`."""
    from click.testing import CliRunner

    from orchestrator import cli as cli_mod

    monkeypatch.setattr(
        "orchestrator.agent_review.run_review", lambda *a, **k: result, raising=False
    )
    return CliRunner().invoke(cli_mod.main, ["agent-review", "hal", "--json-output"])


def _result(**over):
    base = {
        "agent": "hal", "repo": "/tmp/hal", "turns": 32, "signals": [],
        "corpus": {}, "findings": [], "dropped_findings": [],
    }
    base.update(over)
    return base


def test_json_output_exits_nonzero_when_synthesis_did_not_complete(monkeypatch):
    """The human path already shouts about this; the machine path stayed quiet.

    `--json-output` is the path cron and the skills consume, so a timed-out synthesis
    returning `findings: []` with exit 0 is the same wrong answer that caused the
    original incident — just machine-readable.
    """
    res = _invoke_agent_review_json(
        monkeypatch, _result(error="agent-review synthesis pass timed out after 180s")
    )
    assert res.exit_code != 0, "an errored run must not exit 0"
    assert "did not complete" in res.output


def test_json_output_still_emits_the_json_when_it_fails(monkeypatch):
    """Callers paid for the deterministic signals — failing must not withhold them."""
    res = _invoke_agent_review_json(
        monkeypatch, _result(turns=32, error="synthesis pass timed out after 180s")
    )
    payload = json.loads(res.output[res.output.index("{"):res.output.rindex("}") + 1])
    assert payload["turns"] == 32
    assert payload["error"]


def test_json_output_exits_zero_on_a_clean_run(monkeypatch):
    """A genuinely finding-free review is still a success — don't cry wolf."""
    res = _invoke_agent_review_json(monkeypatch, _result())
    assert res.exit_code == 0
    assert json.loads(res.output)["findings"] == []


# --- The human path must not contradict itself: "No findings synthesized." on a run
# --- that just printed `Findings (N):`. The terminal line was guarded on `not no_llm`
# --- alone, so every ordinary SUCCESS fell through to it. ----------------------

def _invoke_agent_review_human(monkeypatch, result, *extra):
    """Run `canopy agent-review hal` (human output) with run_review stubbed to `result`."""
    from click.testing import CliRunner

    from orchestrator import cli as cli_mod

    monkeypatch.setattr(
        "orchestrator.agent_review.run_review", lambda *a, **k: result, raising=False
    )
    return CliRunner().invoke(cli_mod.main, ["agent-review", "hal", *extra])


_A_FINDING = {
    "title": "namespace the turn skill",
    "friction_type": "skill_collisions",
    "fix_kind": "skill_edit",
    "target": "skills/turn/SKILL.md",
    "recommendation": "force reading the agent's own turn skill from disk",
    "confidence": "high",
}


def test_successful_run_does_not_claim_no_findings(monkeypatch):
    """N>0 findings printed, nothing dropped, no error — the tail must not say
    'No findings synthesized.' It did, on every ordinary successful run."""
    res = _invoke_agent_review_human(monkeypatch, _result(findings=[_A_FINDING]))
    assert res.exit_code == 0, res.output
    assert "Findings (1):" in res.output
    assert "No findings synthesized." not in res.output


def test_findings_alongside_drops_also_does_not_claim_no_findings(monkeypatch):
    """`elif not findings and dropped` only covered the all-already-shipped case;
    findings AND drops together fell through to the same wrong line."""
    res = _invoke_agent_review_human(
        monkeypatch,
        _result(findings=[_A_FINDING], dropped_findings=[{"title": "already shipped"}]),
    )
    assert "Findings (1):" in res.output
    assert "No findings synthesized." not in res.output


def test_genuinely_empty_run_still_says_no_findings(monkeypatch):
    """The line is load-bearing for a real empty run — don't silence it."""
    res = _invoke_agent_review_human(monkeypatch, _result())
    assert res.exit_code == 0, res.output
    assert "No findings synthesized." in res.output


def test_all_already_shipped_stays_quiet(monkeypatch):
    """Unchanged: the drop list above already explains the empty findings list."""
    res = _invoke_agent_review_human(
        monkeypatch, _result(dropped_findings=[{"title": "already shipped"}])
    )
    assert "No findings synthesized." not in res.output
    assert "Dropped by source gate" in res.output


def test_errored_run_still_shouts(monkeypatch):
    """Unchanged: a costed pass that didn't complete stays LOUD, not a parenthetical."""
    res = _invoke_agent_review_human(
        monkeypatch, _result(error="synthesis pass timed out after 180s")
    )
    assert "SYNTHESIS PASS DID NOT COMPLETE" in res.output
    assert "No findings synthesized." not in res.output


def test_no_llm_run_prints_neither_line(monkeypatch):
    """--no-llm never synthesizes, so it must not report on synthesis either way."""
    res = _invoke_agent_review_human(monkeypatch, _result(), "--no-llm")
    assert "No findings synthesized." not in res.output


# --- The coercion must be VISIBLE, not a silent rewrite of the model's routing ----

def test_cli_shows_the_fix_kind_coercion(monkeypatch):
    """A triager reading the findings table has to see that canopy overrode the
    model's fix_kind — and what it originally said — to sanity-check the target."""
    finding = {
        "title": "NEVER send raw email",
        "friction_type": "safety_override",
        "fix_kind": "hook_rule",
        "target": "config/gating.json",
        "recommendation": "route sends through bin/hal-email",
        "confidence": "high",
        "_fix_kind_coerced": {"from": "skill_edit", "to": "hook_rule", "reason": "…"},
    }
    res = _invoke_agent_review_human(monkeypatch, _result(findings=[finding]))
    assert res.exit_code == 0, res.output
    assert "coerced" in res.output.lower()
    assert "skill_edit" in res.output           # what the model proposed
    assert "hook_rule → config/gating.json" in res.output   # what it ships as


# --- No wall-clock budget by default: a timed-out pass returns ZERO findings, so a
# --- budget shorter than the work silently manufactures a clean bill of health.
# --- (180s repeatably timed out on a 7-turn hal corpus.) -----------------------

def test_synthesis_and_verify_default_to_no_timeout():
    from orchestrator import agent_review as ar
    assert ar.SYNTHESIS_TIMEOUT is None
    assert ar.VERIFY_TIMEOUT is None


def test_run_review_passes_no_timeout_to_subprocess_by_default(tmp_path, monkeypatch):
    """The default must reach subprocess.run as timeout=None — not merely be absent
    from the CLI — or the pass is still bounded somewhere down the stack."""
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
        seen["timeout"] = kwargs.get("timeout", "ABSENT")
        return sp.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(ar.subprocess, "run", fake_run)
    ar.run_review(str(repo), projects_dir=projects)
    assert seen["timeout"] is None


def test_an_explicit_timeout_still_works_and_still_fails_loud(tmp_path, monkeypatch):
    """The plumbing stays: a caller that WANTS a bound still gets one, and still gets
    the well-formed string error a live cron incident paid for."""
    import subprocess as sp
    from orchestrator import agent_review as ar

    repo = tmp_path / "repositories" / "echo"
    (repo / "skills").mkdir(parents=True)
    projects = tmp_path / "projects"
    d = projects / "-Users-x-emdash-repositories-echo"
    d.mkdir(parents=True)
    _write_transcript(d / "a.jsonl", str(repo), [("Read", {"file_path": "/x"}, "ok")])

    def fake_run(cmd, **kwargs):
        raise sp.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(ar.subprocess, "run", fake_run)
    result = ar.run_review(str(repo), projects_dir=projects, timeout=45)
    assert isinstance(result["error"], str)
    assert "timed out" in result["error"].lower() and "45" in result["error"]
    assert result["findings"] == []


# --- #488: human_corrections must not mine MACHINE-authored user turns --------
#
# Sibling of the harness-injected-turns task above, one layer out. Those blocks are
# tag-delimited, so a regex finds them. These are plain strings that read exactly like a
# human turn, because the harness genuinely received them as typed input:
#
#   * a Stop hook's `reason`, replayed into the conversation ("Stop hook feedback: …")
#   * a dispatch brief another agent wrote to open a dispatched session
#
# Measured on hal 2026-08-14 (`canopy agent-review hal`, last 24h): 6 reported "human
# corrections", of which 5 were machine-authored — two hook replays of hal's own
# turn_close_guard, two Ada dispatch briefs, one generated resume brief. One was Jonathan.
# The same ratio the harness-block filter was built for, via a class it doesn't cover.
#
# Not cosmetic: the generated resume brief became a fleet-scope finding, escalated because
# the same "correction" appeared in two agents' transcripts in one window. It appeared in
# both because ONE dispatch batch opened six sessions in five minutes with the same
# generated text — co-occurrence is the bug's signature, not corroboration.

def test_human_corrections_ignores_replayed_hook_feedback():
    """A Stop hook's `reason` comes back as a `user` turn. A close-out rail is written to
    be forceful, so it scores `strong_correction` every time it fires — in every turn that
    ended without its close-out. Claude Code marks these `isMeta: true`."""
    from orchestrator.agent_review import human_corrections

    entries = [{"type": "user", "isMeta": True, "message": {"content":
        "Stop hook feedback:\nThis turn is ending without its close-out. Run it now:\n\n"
        "    bin/hal-turn-close\n\nThat refreshes the canopy-web workspace and prints the "
        "close checklist (hal:agent-turn-review, skill self-check, task packaging)."}}]
    assert human_corrections(entries) == []


def test_human_corrections_ignores_a_canopy_dispatched_brief():
    """A dispatched turn opens with another agent's brief. Its scaffolding — ALL-CAPS
    section headers, "RE-VALIDATE FIRST", "STOP and report" — is exactly what `emphasis` /
    `strong_correction` / `safety_override` match. The dispatcher stamps it; we strip it."""
    from orchestrator.agent_dispatch import stamp_dispatched
    from orchestrator.agent_review import human_corrections

    brief = ("Two fixes in canopy's own agent-review path. Target repo: canopy.\n\n"
             "**RE-VALIDATE FIRST.** If either is already fixed, STOP on that one and "
             "report back rather than shipping. Do not send anything outbound.")
    entries = [{"type": "user", "message": {"content": stamp_dispatched(brief)}}]
    assert human_corrections(entries) == []


def test_dispatch_marker_is_the_same_string_on_both_sides():
    """The stamper and the stripper live in different modules; a drift between them fails
    open — every dispatched brief silently becomes a `human_correction` again."""
    from orchestrator import agent_dispatch, agent_review

    assert agent_dispatch.DISPATCH_MARKER == agent_review.DISPATCH_MARKER


def test_human_corrections_survives_a_transcript_with_no_isMeta_key():
    """Older transcripts and other harness versions don't carry `isMeta`. Absent must mean
    "assume human" — a stricter default would start dropping Jonathan."""
    from orchestrator.agent_review import human_corrections

    entries = [{"type": "user", "message": {"content": "stop, that's wrong"}}]
    got = human_corrections(entries)
    assert len(got) == 1
    assert "strong_correction" in got[0]["kinds"]


def test_human_corrections_keeps_the_one_real_correction_beside_the_five_fakes():
    """The hal 2026-08-14 corpus, reduced: five machine turns and Jonathan's one line.
    Mining it must yield exactly his."""
    from orchestrator.agent_dispatch import stamp_dispatched
    from orchestrator.agent_review import human_corrections

    entries = [
        {"type": "user", "isMeta": True, "message": {"content":
            "Stop hook feedback:\nThis turn is ending without its close-out. Run it now:\n"
            "    bin/hal-turn-close"}},
        {"type": "user", "message": {"content": stamp_dispatched(
            "Three self-improvement fixes inside your own repo. Nothing outbound.\n\n"
            "**RE-VALIDATE FIRST.** If already fixed, STOP and report.")}},
        {"type": "user", "message": {"content": stamp_dispatched(
            "Two fixes in canopy's own agent-review path. Target repo: canopy.\n\n"
            "**RE-VALIDATE FIRST.** STOP rather than shipping a fix that already landed.")}},
        {"type": "user", "message": {"content":
            "Why are you asking me?  just do the right thing if its clear?"}},
        {"type": "user", "isMeta": True, "message": {"content":
            "Stop hook feedback:\nThis turn is ending without its close-out. Run it now:\n"
            "    bin/hal-turn-close"}},
        {"type": "user", "message": {"content": stamp_dispatched(
            "We got rate limited on the acedimagi account and are moving this work to this "
            "account to continue.\n\nREPO:   hal\nBRANCH: hal/self-improve-ship\n\n"
            "RE-VALIDATE BEFORE CONTINUING. If the work looks already done, STOP AND "
            "REPORT rather than redoing it.")}},
    ]
    got = human_corrections(entries)
    assert len(got) == 1, [g["quote"][:60] for g in got]
    assert got[0]["quote"].startswith("Why are you asking me?")
    assert got[0]["kinds"] == ["confusion"]


# --- Confidence rail: repair the label, keep the evidence ---------------------
# Regression guard for the 2026-08-18 cycle, where `agent-review ace --hours 24`
# synthesized 5 findings and dropped all 5 on a malformed `evidence.confidence`,
# reporting "No findings synthesized" for the noisiest agent in the fleet.
from orchestrator.agent_review import normalize_confidence
import pytest


@pytest.mark.parametrize("level", ["high", "medium", "low"])
def test_valid_confidence_passes_through_uncoerced(level):
    f = {"title": "t", "evidence": dict(_GOOD_EV, confidence=level)}
    qualified, dropped = qualify_findings([f])
    assert dropped == []
    assert qualified[0]["evidence"]["confidence"] == level
    assert "_confidence_coerced" not in qualified[0]


@pytest.mark.parametrize("raw,expected", [
    ("HIGH", "high"), ("  Medium  ", "medium"), ("Low.", "low"),
    ("very high", "high"), ("very-high", "high"), ("Very_High", "high"),
    ("certain", "high"), ("strong", "high"),
    ("med", "medium"), ("moderate", "medium"), ("mid", "medium"),
    ("weak", "low"), ("tentative", "low"), ("speculative", "low"),
])
def test_alias_and_case_are_coerced_not_dropped(raw, expected):
    f = {"title": "t", "evidence": dict(_GOOD_EV, confidence=raw)}
    qualified, dropped = qualify_findings([f])
    assert dropped == []
    assert qualified[0]["evidence"]["confidence"] == expected
    c = qualified[0]["_confidence_coerced"]
    assert c["from"] == raw and c["to"] == expected


@pytest.mark.parametrize("raw,expected", [
    (0.95, "high"), (0.8, "high"), (0.6, "medium"), (0.5, "medium"), (0.2, "low"),
    (95, "high"), (85, "high"), (60, "medium"), (10, "low"),
    ("0.9", "high"), ("85%", "high"), ("40%", "low"),
])
def test_numeric_confidence_is_banded(raw, expected):
    f = {"title": "t", "evidence": dict(_GOOD_EV, confidence=raw)}
    qualified, dropped = qualify_findings([f])
    assert dropped == [], f"{raw!r} was dropped"
    assert qualified[0]["evidence"]["confidence"] == expected


def test_unmappable_string_bands_down_to_low_never_up():
    """A repair must never INFLATE trust: an unrecognized-but-present label is
    conservative-defaulted to 'low', not to 'high'."""
    f = {"title": "t", "evidence": dict(_GOOD_EV, confidence="banana")}
    qualified, dropped = qualify_findings([f])
    assert dropped == []
    assert qualified[0]["evidence"]["confidence"] == "low"
    assert "banana" in qualified[0]["_confidence_coerced"]["reason"]


def test_missing_nested_confidence_falls_back_to_finding_level():
    """The synthesis prompt asks for `confidence` at BOTH levels; the model routinely
    fills exactly one. Reading the sibling is a repair, not an invention."""
    ev = dict(_GOOD_EV); del ev["confidence"]
    f = {"title": "t", "confidence": "high", "evidence": ev}
    qualified, dropped = qualify_findings([f])
    assert dropped == []
    assert qualified[0]["evidence"]["confidence"] == "high"
    assert qualified[0]["_confidence_coerced"]["source"] == "finding.confidence"


def test_confidence_absent_at_both_levels_is_still_dropped():
    """Genuinely absent is absent — the hard drop survives, and the drop reason now
    names BOTH observed values so a mislabel is distinguishable from an omission."""
    ev = dict(_GOOD_EV); del ev["confidence"]
    f = {"title": "t", "evidence": ev}
    qualified, dropped = qualify_findings([f])
    assert qualified == []
    reason = dropped[0]["_drop_reason"]
    assert "evidence.confidence" in reason and "finding.confidence" in reason


def test_empty_confidence_at_both_levels_is_dropped():
    f = {"title": "t", "confidence": "   ", "evidence": dict(_GOOD_EV, confidence="")}
    qualified, dropped = qualify_findings([f])
    assert qualified == [] and len(dropped) == 1


def test_bool_confidence_is_not_treated_as_a_score():
    """bool is an int in Python — True must not band to 'low' via the numeric path."""
    level, _ = normalize_confidence(True)
    assert level is None


def test_out_of_range_number_is_not_banded():
    level, _ = normalize_confidence(400)
    assert level is None


def test_confidence_coercion_is_logged_to_stderr(capsys):
    """A silent repair is just a different kind of quiet — the fix_kind rail logs, so
    this one must too."""
    f = {"title": "noisy finding", "evidence": dict(_GOOD_EV, confidence="very high")}
    kept = _qualify_and_log([f], label="ace")
    assert len(kept) == 1
    err = capsys.readouterr().err
    assert "coerced confidence" in err and "noisy finding" in err and "'very high'" in err


def test_five_findings_with_bad_confidence_all_survive():
    """The exact 2026-08-18 shape: five evidence-valid findings whose only defect was
    the confidence word. The run reported 'No findings synthesized'; it must not again."""
    raws = ["very high", "VeryHigh", 0.9, "moderate", "Certain"]
    findings = [
        {"title": f"finding {i}", "fix_kind": "skill_edit",
         "evidence": dict(_GOOD_EV, confidence=r)}
        for i, r in enumerate(raws)
    ]
    qualified, dropped = qualify_findings(findings)
    assert dropped == [], [d["_drop_reason"] for d in dropped]
    assert len(qualified) == 5
    assert all(q["evidence"]["confidence"] in _CONF_LEVELS_T for q in qualified)


_CONF_LEVELS_T = {"high", "medium", "low"}


def test_both_confidence_fields_are_synced_to_the_resolved_level():
    """The gate reads evidence.confidence; the findings TABLE reads finding.confidence.
    Nothing normalized the latter, so a repaired finding could still print the raw label
    (or 'banana'). One resolved level, written to both."""
    f = {"title": "t", "confidence": "banana",
         "evidence": dict(_GOOD_EV, confidence="very high")}
    qualified, dropped = qualify_findings([f])
    assert dropped == []
    assert qualified[0]["evidence"]["confidence"] == "high"
    assert qualified[0]["confidence"] == "high", "the displayed label must match the gate's"


def test_sibling_fallback_also_normalizes_the_displayed_label():
    ev = dict(_GOOD_EV); del ev["confidence"]
    f = {"title": "t", "confidence": "VERY HIGH", "evidence": ev}
    qualified, _ = qualify_findings([f])
    assert qualified[0]["confidence"] == "high"
    assert qualified[0]["evidence"]["confidence"] == "high"


def test_prompt_says_the_two_confidence_fields_are_one_judgment(tmp_path):
    """Root cause of the 2026-08-18 drops: the prompt asked for `confidence` twice with
    no hint they were the same value, so the model filled exactly one."""
    prompt = build_review_prompt(tmp_path, corpus=[])
    assert "SAME value as evidence.confidence" in prompt


# --- Turn-step markers must track how the step is performed TODAY ---------------
# Regression guard for 2026-08-19. The haystack is assistant text + user text + tool
# NAMES/INPUTS — never tool RESULTS. So when a step's mechanics move inside a script,
# only the script's name reaches the haystack and the old marker goes blind. Measured on
# hal over 12 turn-sessions: the close-out ran in 10, `workspace-refresh` detected 1.
import re

from orchestrator.agent_review import DEFAULT_TURN_STEPS

# label -> markers. A step may carry a 3rd element (conditional triggers); these
# marker tests are about detection only, so drop everything past the marker tuple.
_STEPS = {step[0]: step[1] for step in DEFAULT_TURN_STEPS}


def _detects(step: str, text: str) -> bool:
    return any(re.search(m, text) for m in _STEPS[step])


@pytest.mark.parametrize("invocation", [
    "bash bin/hal-turn-close",
    "bin/ace-turn-close --dry-run",
    "python3 bin/eva-turn-close",
    "canopy agent turn --slug hal --title x",
])
def test_workspace_refresh_detects_the_close_out_script(invocation):
    """The close-out script IS the workspace refresh — it shells `canopy agent-publish`.
    Detecting only the inner command penalized agents for adopting the rail."""
    assert _detects("workspace-refresh", invocation.lower())


def test_workspace_refresh_still_detects_the_bare_command():
    assert _detects("workspace-refresh", "canopy agent-publish skills --repo .")
    assert _detects("workspace-refresh", "posted to /agents/hal")


def test_self_review_detects_the_renamed_step():
    """hal and ace renamed `self-review` to `agent-turn-review`; eva and echo did not.
    Both names are the same step and both must count."""
    assert _detects("self-review", "running hal:agent-turn-review before sending")
    assert _detects("self-review", "ran eva:self-review")


def test_preflight_detects_the_canonical_check_command():
    assert _detects("preflight", 'bash "$canopy/scripts/canopy-update-check.sh"')
    assert _detects("preflight", "[preflight] canopy 0.2.413 is current")
    assert _detects("preflight", "up_to_date 0.2.413".lower())


def test_markers_do_not_fire_on_an_unrelated_turn():
    """Widening must not turn every session into a pass — that would hide real gaps."""
    noise = "read the readme, ran the tests, opened a pr, merged it"
    for step in _STEPS:
        assert not _detects(step, noise), f"{step} false-positived on unrelated text"


# --- A bare-filename target must reach the verification gate as evidence ----------
# 2 of the 8 findings in the real 2026-08-18 `agent-review ace` run named a bare file
# (`persona.md`, `CLAUDE.md`) as their target. `_finding_symbols` only accepted tokens
# containing "/", so those findings reached the source-verification gate with NO symbols
# — no grep, no file content — which resolves to `unverifiable` and is KEPT. Same shape
# as the miss #504 fixed: the gate cannot see the target, so an already-shipped finding
# survives to waste a turn.
from orchestrator.agent_review import _finding_symbols


@pytest.mark.parametrize("target", ["persona.md", "CLAUDE.md", "run-state-validator.ts",
                                    "config.json", "setup.sh", "pyproject.toml"])
def test_bare_filename_target_is_extracted(target):
    assert target in _finding_symbols([{"target": target}])


def test_path_target_still_extracted():
    assert "skills/turn/SKILL.md" in _finding_symbols([{"target": "skills/turn/SKILL.md"}])


@pytest.mark.parametrize("prose", ["e.g.", "vs.", "etc.", "i.e.", "the"])
def test_prose_tokens_are_not_mistaken_for_files(prose):
    """A permissive `\\.\\w+$` rule would turn ordinary prose into phantom file targets,
    and a phantom target produces confidently-empty evidence."""
    assert prose not in _finding_symbols([{"target": f"somewhere {prose} something"}])


def test_multi_target_string_yields_each_file():
    syms = _finding_symbols([{"target": "persona.md or CLAUDE.md"}])
    assert "persona.md" in syms and "CLAUDE.md" in syms


def test_backticked_symbols_still_win():
    syms = _finding_symbols([{"title": "fix `normalize_confidence` in `agent_review.py`"}])
    assert "normalize_confidence" in syms and "agent_review.py" in syms
