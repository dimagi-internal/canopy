"""Structural invariants for the ddd orchestrator agent (SP5).

Pins the load-bearing strings in the agent file and command file.
Does NOT exercise runtime behavior — drift here is a bug.

Checked invariants:
  1.  Agent file exists at plugins/canopy/agents/ddd.md.
  2.  Command file exists at plugins/canopy/commands/ddd.md.
  3.  Frontmatter: name=ddd, model=inherit, memory=user.
  4.  All 8 DDD skills named in chain order.
  5.  TWO-gate pause policy stated: concept_change + external_release.
  6.  Nothing-else-blocks rule stated explicitly.
  7.  Routing section distinguishes two finding sources:
      A. Design-findings routes (from ddd-concept-eval's design_findings.json):
         PRODUCT → /design-review, /review, or /qa
         CONCEPT → re-run ddd-spec (edit narration/design_intent)
         RESEARCH → autonomous investigation + re-run Phase 0
         DEFER → log only (non-blocking)
      B. Why-brief gap types (from Phase 0 ddd-why-brief):
         RESEARCH → autonomous investigation
         CAPABILITY → product-build task  ← originates HERE, not in design_findings
         DECISION → concept_change pause
  8.  compute_convergence referenced.
  9.  MAX_ITERATIONS referenced.
 10.  Reads .canopy/ddd/context and earlier runs' summaries (no learnings.md).
 11.  Suggest-then-confirm self-tuning mentioned (never auto-apply).
 12.  Digest email referenced (PM-loop autonomous digest style).
 13.  Command file has description + allowed-tools including Skill or Agent.
"""
from __future__ import annotations

from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent.parent / "plugins" / "canopy"
AGENTS_DIR = PLUGIN_ROOT / "agents"
COMMANDS_DIR = PLUGIN_ROOT / "commands"

AGENT_FILE = AGENTS_DIR / "ddd.md"
COMMAND_FILE = COMMANDS_DIR / "ddd.md"

# Chain order must match the pipeline definition in the task description.
ORDERED_SKILLS = [
    "ddd-evidence-audit",
    "ddd-why-brief",
    "ddd-why-qa",
    "ddd-why-eval",
    "ddd-spec",
    "ddd-spec-qa",
    "ddd-concept-eval",
    "ddd-run",
]


# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------


def test_agent_file_exists() -> None:
    assert AGENT_FILE.exists(), f"Agent file missing: {AGENT_FILE}"


def test_command_file_exists() -> None:
    assert COMMAND_FILE.exists(), f"Command file missing: {COMMAND_FILE}"


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def _frontmatter(path: Path) -> str:
    """Return the YAML frontmatter block (between the first two --- delimiters)."""
    parts = path.read_text().split("---", 2)
    assert len(parts) >= 3, f"No frontmatter found in {path}"
    return parts[1]


def test_agent_frontmatter_name() -> None:
    fm = _frontmatter(AGENT_FILE)
    assert "name: ddd" in fm, "Agent frontmatter must declare 'name: ddd'"


def test_agent_frontmatter_model_inherit() -> None:
    fm = _frontmatter(AGENT_FILE)
    assert "model: inherit" in fm, "Agent frontmatter must declare 'model: inherit'"


def test_agent_frontmatter_memory_user() -> None:
    fm = _frontmatter(AGENT_FILE)
    assert "memory: user" in fm, "Agent frontmatter must declare 'memory: user'"


# ---------------------------------------------------------------------------
# All 8 skills named
# ---------------------------------------------------------------------------


def test_agent_names_all_eight_skills() -> None:
    content = AGENT_FILE.read_text()
    missing = [s for s in ORDERED_SKILLS if s not in content]
    assert not missing, f"Agent body missing skill references: {missing}"


def test_agent_names_skills_in_chain_order() -> None:
    """Skills must appear in the canonical chain order (Phase 0 → spec → run).

    ddd-run orchestrates ddd-concept-eval internally, so the agent correctly
    references ddd-run (Step 7 dispatch) before ddd-concept-eval (routing
    explanation section).  We check ordered position for all consecutive pairs
    EXCEPT the ddd-run → ddd-concept-eval pair, which intentionally inverts
    because ddd-run wraps the concept eval.
    """
    content = AGENT_FILE.read_text()
    positions = {}
    for skill in ORDERED_SKILLS:
        idx = content.find(skill)
        assert idx != -1, f"Skill '{skill}' not found in agent body"
        positions[skill] = idx

    # Pairs where strict position order is NOT required:
    # ddd-run dispatches ddd-concept-eval internally, so ddd-run appears
    # in the main steps section while ddd-concept-eval appears later in the
    # routing/description section.
    inversion_allowed = {("ddd-concept-eval", "ddd-run")}

    for i in range(len(ORDERED_SKILLS) - 1):
        a, b = ORDERED_SKILLS[i], ORDERED_SKILLS[i + 1]
        if (a, b) in inversion_allowed or (b, a) in inversion_allowed:
            continue
        assert positions[a] < positions[b], (
            f"Skill '{a}' must appear before '{b}' in the agent body "
            f"(positions: {positions[a]} vs {positions[b]})"
        )


# ---------------------------------------------------------------------------
# Two-gate pause policy
# ---------------------------------------------------------------------------


def test_agent_states_concept_change_gate() -> None:
    content = AGENT_FILE.read_text()
    assert "concept_change" in content, (
        "Agent must state the 'concept_change' gate as one of two blocking gates"
    )


def test_agent_states_external_release_gate() -> None:
    content = AGENT_FILE.read_text()
    assert "external_release" in content, (
        "Agent must state the 'external_release' gate as one of two blocking gates"
    )


def test_agent_states_two_gate_policy() -> None:
    """Both gates must appear together in a policy statement, not just mentioned in passing."""
    content = AGENT_FILE.read_text()
    # Accept any phrasing that puts both gates next to each other as a policy
    has_both_near = False
    idx_cc = content.find("concept_change")
    idx_er = content.find("external_release")
    if idx_cc != -1 and idx_er != -1:
        # Within 500 chars of each other in either order = policy statement
        has_both_near = abs(idx_cc - idx_er) <= 500
    assert has_both_near, (
        "concept_change and external_release must appear together as a pause policy"
    )


def test_agent_states_nothing_else_blocks() -> None:
    """Agent must explicitly say nothing else pauses or blocks."""
    content = AGENT_FILE.read_text()
    nothing_else = (
        "nothing else" in content.lower()
        or "everything else" in content.lower()
        or "only two" in content.lower()
        or "only 2" in content.lower()
        or "all other" in content.lower()
    )
    assert nothing_else, (
        "Agent must explicitly state that nothing else blocks (only these two gates pause)"
    )


# ---------------------------------------------------------------------------
# Routing: two-source structure
# ---------------------------------------------------------------------------


def test_agent_routing_distinguishes_two_finding_sources() -> None:
    """Agent must explicitly separate design_findings.json routes from why-brief gap types.

    The routing section must reference both source artifacts so a maintainer
    understands that CAPABILITY is NOT a design_findings route.
    """
    content = AGENT_FILE.read_text()
    has_design_findings_source = "design_findings" in content or "ddd-concept-eval" in content
    has_why_brief_source = "why_brief" in content or "why-brief" in content or "ddd-why-brief" in content
    assert has_design_findings_source, (
        "Routing section must reference design_findings.json (or ddd-concept-eval) as a source"
    )
    assert has_why_brief_source, (
        "Routing section must reference why_brief (or ddd-why-brief) as a source"
    )


def test_agent_capability_route_tied_to_why_brief_not_design_findings() -> None:
    """CAPABILITY must be documented as coming from why-brief gaps (Phase 0), NOT design_findings.

    The agent must make clear that CAPABILITY originates from ddd-why-brief gap types so
    a maintainer doesn't expect it to appear in design_findings.json.
    """
    content = AGENT_FILE.read_text()
    assert "CAPABILITY" in content, "Agent must name CAPABILITY"
    # CAPABILITY must appear in the same section as why-brief / Phase 0 gap types,
    # not as one of the four design_findings route values.
    # We verify by checking that the doc explicitly states CAPABILITY is NOT in design_findings,
    # OR that CAPABILITY appears alongside the why-brief gap type vocabulary
    # (RESEARCH / CAPABILITY / DECISION triplet from ddd-why-brief).
    why_brief_gap_vocabulary = (
        "CAPABILITY" in content and "DECISION" in content
        and ("gap" in content.lower() or "why-brief" in content.lower() or "why_brief" in content.lower())
    )
    assert why_brief_gap_vocabulary, (
        "CAPABILITY must appear alongside DECISION and gap/why-brief vocabulary "
        "(these are why-brief gap types, not design_findings routes)"
    )
    # Additionally, CAPABILITY must appear as a row in the why-brief gap types table
    # (§B), not in the design-findings route table (§A).  We locate the §B header by
    # looking for the "why-brief gap types" section marker and confirm CAPABILITY
    # appears after it.  Using rfind for CAPABILITY picks its last occurrence, which
    # is always in the gap-types table (not in the earlier pause-policy preamble).
    why_brief_section_marker = "Why-brief gap types" in content or "why-brief gap types" in content.lower()
    assert why_brief_section_marker, (
        "Agent must have a clearly labeled 'Why-brief gap types' section that covers CAPABILITY"
    )
    # Find the start of the §B section and verify CAPABILITY appears within it
    # (i.e. after the section header, not only in the earlier preamble).
    section_b_idx = content.lower().find("why-brief gap types")
    capability_last_idx = content.rfind("CAPABILITY")
    assert capability_last_idx > section_b_idx, (
        "CAPABILITY's defining routing entry must appear in the why-brief gap types section (§B), "
        "not only in the design-findings table (§A)"
    )


# ---------------------------------------------------------------------------
# Design-findings routes: all four must be present
# ---------------------------------------------------------------------------


def test_agent_routes_product_findings() -> None:
    """PRODUCT findings must route to specialist skills (design-review / review / qa)."""
    content = AGENT_FILE.read_text()
    has_product_route = (
        "design-review" in content or "/design-review" in content
        or ("PRODUCT" in content and ("review" in content or "qa" in content))
    )
    assert has_product_route, (
        "Agent must route PRODUCT findings to /design-review, /review, or /qa"
    )


def test_agent_routes_concept_findings() -> None:
    """CONCEPT findings must trigger spec edit (narration / design_intent) and ddd-spec re-run."""
    content = AGENT_FILE.read_text()
    assert "CONCEPT" in content, "Agent must name the CONCEPT route"
    concept_fix = (
        "narration" in content or "design_intent" in content or "ddd-spec" in content
    )
    assert concept_fix, (
        "CONCEPT route must edit spec narration/design_intent or re-run ddd-spec"
    )


def test_agent_routes_research_findings() -> None:
    """RESEARCH findings must trigger autonomous investigation (in design-findings AND why-brief)."""
    content = AGENT_FILE.read_text()
    assert "RESEARCH" in content, "Agent must name the RESEARCH route"
    # RESEARCH must appear in both the design-findings and why-brief sections
    # (it is a valid route in both). Verify at least two occurrences.
    assert content.count("RESEARCH") >= 2, (
        "RESEARCH must appear at least twice — once as a design-findings route "
        "and once as a why-brief gap type"
    )


def test_agent_routes_defer_findings() -> None:
    """DEFER findings must be logged non-blocking (advisory findings land here)."""
    content = AGENT_FILE.read_text()
    assert "DEFER" in content, "Agent must name the DEFER route in the design-findings table"
    # DEFER must be associated with non-blocking / log-only behavior
    defer_log = "log" in content.lower() or "non-blocking" in content.lower() or "digest" in content.lower()
    assert defer_log, (
        "DEFER route must describe log-only or non-blocking handling"
    )


def test_agent_all_four_design_findings_routes_present() -> None:
    """All four valid design_findings.json route values must appear in the routing section."""
    content = AGENT_FILE.read_text()
    for route in ("PRODUCT", "CONCEPT", "RESEARCH", "DEFER"):
        assert route in content, (
            f"Design-findings route '{route}' missing from agent routing section"
        )


# ---------------------------------------------------------------------------
# Why-brief gap types: all three must be present
# ---------------------------------------------------------------------------


def test_agent_routes_capability_findings() -> None:
    """CAPABILITY gap type (from why-brief, Phase 0) must create a product-build task.

    CAPABILITY is a why-brief gap type emitted by ddd-why-brief, NOT a route value
    from design_findings.json. The agent must handle it but must not list it as a
    design-findings route.
    """
    content = AGENT_FILE.read_text()
    assert "CAPABILITY" in content, "Agent must name the CAPABILITY gap type"
    build_task = "task" in content.lower() or "build" in content.lower()
    assert build_task, (
        "CAPABILITY gap type must create a product-build task"
    )


def test_agent_all_three_why_brief_gap_types_present() -> None:
    """All three why-brief gap types (RESEARCH, CAPABILITY, DECISION) must be handled."""
    content = AGENT_FILE.read_text()
    for gap_type in ("RESEARCH", "CAPABILITY", "DECISION"):
        assert gap_type in content, (
            f"Why-brief gap type '{gap_type}' missing from agent routing section"
        )


# ---------------------------------------------------------------------------
# Convergence + iteration cap
# ---------------------------------------------------------------------------


def test_agent_references_compute_convergence() -> None:
    content = AGENT_FILE.read_text()
    assert "compute_convergence" in content, (
        "Agent must reference compute_convergence from run_pipeline"
    )


def test_agent_references_loop_cap() -> None:
    # The v3 loop is progress-aware (commit c299bc2): the raw MAX_ITERATIONS=3
    # count was replaced by a stall/regression stop (`stop_max_iter`) plus a
    # `HARD_CAP` runaway backstop. The agent must still document how the
    # refinement loop is bounded — just via that mechanism, not the old literal.
    content = AGENT_FILE.read_text()
    assert "stop_max_iter" in content, (
        "Agent must reference stop_max_iter — the v3 progress-aware loop stop"
    )
    assert "HARD_CAP" in content or "hard cap" in content.lower() or "backstop" in content.lower(), (
        "Agent must reference the HARD_CAP runaway backstop that bounds the loop"
    )


# ---------------------------------------------------------------------------
# Context + learnings bootstrap
# ---------------------------------------------------------------------------


def test_agent_reads_ddd_context() -> None:
    content = AGENT_FILE.read_text()
    assert ".canopy/ddd" in content, (
        "Agent must read the .canopy/ddd/ directory for context and learnings"
    )
    assert "context" in content.lower(), (
        "Agent must read context.md from .canopy/ddd/"
    )


def test_agent_reads_prior_run_summaries_not_learnings() -> None:
    """learnings.md is retired (2026-10-16): the bootstrap reads earlier runs'
    summaries from canopy-web, and nothing tells the agent to read, append to or
    commit a learnings file."""
    content = AGENT_FILE.read_text()
    assert "scripts.ddd.prior_runs" in content
    assert "append_learning" not in content
    assert "Read `$DDD_DIR/learnings.md`" not in content
    assert "Commit that file with the run" not in content


def test_agent_bootstraps_context_if_missing() -> None:
    """Mirror of PM supervisor: bootstrap context.md if it doesn't exist."""
    content = AGENT_FILE.read_text()
    bootstrap = (
        "bootstrap" in content.lower()
        or "if missing" in content.lower()
        or "if it doesn't exist" in content.lower()
        or "does not exist" in content.lower()
        or "doesn't exist" in content.lower()
    )
    assert bootstrap, (
        "Agent must bootstrap context.md if it is missing (mirror PM supervisor pattern)"
    )


# ---------------------------------------------------------------------------
# Self-tuning (suggest-then-confirm, never auto-apply)
# ---------------------------------------------------------------------------


def test_agent_mentions_suggest_then_confirm_self_tuning() -> None:
    content = AGENT_FILE.read_text()
    suggest = (
        "suggest" in content.lower()
        or "propose" in content.lower()
    )
    confirm = (
        "confirm" in content.lower()
        or "approval" in content.lower()
        or "approved" in content.lower()
    )
    assert suggest and confirm, (
        "Agent must describe suggest-then-confirm self-tuning (never auto-apply)"
    )


def test_agent_self_tuning_does_not_auto_apply() -> None:
    content = AGENT_FILE.read_text()
    assert "never auto" in content.lower() or "do not auto" in content.lower() or "not auto-apply" in content.lower(), (
        "Agent must explicitly say it never auto-applies self-tuning changes"
    )


# ---------------------------------------------------------------------------
# Digest email
# ---------------------------------------------------------------------------


def test_agent_mentions_digest_email() -> None:
    content = AGENT_FILE.read_text()
    assert "digest" in content.lower(), (
        "Agent must mention the autonomous digest email (PM-loop style)"
    )


def test_agent_digest_links_to_review_page() -> None:
    """Digest must include a link to the canopy-web review page (SP6 destination)."""
    content = AGENT_FILE.read_text()
    has_review_link = (
        "review page" in content.lower()
        or "canopy-web" in content.lower()
        or "ReviewRequest" in content
    )
    assert has_review_link, (
        "Agent digest must link to the canopy-web review page (SP6 destination)"
    )


# ---------------------------------------------------------------------------
# Command file shape
# ---------------------------------------------------------------------------


def test_command_has_description_in_frontmatter() -> None:
    fm = _frontmatter(COMMAND_FILE)
    assert "description:" in fm, "Command file must have a description in frontmatter"


def test_command_has_allowed_tools() -> None:
    content = COMMAND_FILE.read_text()
    assert "allowed-tools" in content, "Command file must declare allowed-tools"


def test_command_allowed_tools_includes_skill_or_agent() -> None:
    content = COMMAND_FILE.read_text()
    assert "Skill" in content or "Agent" in content, (
        "Command allowed-tools must include Skill or Agent to dispatch sub-skills"
    )


# ---------------------------------------------------------------------------
# v3 — actionability gate + approve/redraft vocabulary
# ---------------------------------------------------------------------------


def test_agent_includes_actionability_eval_gate() -> None:
    """v3: Agent must invoke ddd-narrative-actionability-eval between spec-qa and narrative-review."""
    content = AGENT_FILE.read_text()
    assert "ddd-narrative-actionability-eval" in content, (
        "Agent must invoke ddd-narrative-actionability-eval as a gate (Step 6a)"
    )


def test_agent_actionability_gate_appears_before_narrative_review() -> None:
    """v3: actionability eval must appear before narrative-review in the agent flow."""
    content = AGENT_FILE.read_text()
    idx_eval = content.find("ddd-narrative-actionability-eval")
    idx_review = content.find("ddd-narrative-review")
    assert idx_eval != -1, "ddd-narrative-actionability-eval not found in agent"
    assert idx_review != -1, "ddd-narrative-review not found in agent"
    assert idx_eval < idx_review, (
        "ddd-narrative-actionability-eval must appear before ddd-narrative-review in the agent flow"
    )


def test_agent_actionability_fail_loops_to_spec() -> None:
    """v3: if actionability eval fails, agent must loop back to ddd-spec (not advance to review)."""
    content = AGENT_FILE.read_text()
    # The actionability section must mention looping back or returning to ddd-spec on fail
    fail_loop_signals = [
        "loop back to step 5",
        "loop back to ddd-spec",
        "back to step 5",
        "back to ddd-spec",
        "do not advance",
        "do not proceed",
    ]
    assert any(s.lower() in content.lower() for s in fail_loop_signals), (
        "Agent must state that an actionability fail loops back to ddd-spec (not advance to review)"
    )


def test_agent_narrative_review_uses_approve_redraft() -> None:
    """v3: narrative-agreement gate must use approve/redraft (not agree/edit/rethink)."""
    content = AGENT_FILE.read_text()
    assert "approve" in content.lower(), (
        "Agent must use 'approve' as the go-forward narrative decision"
    )
    assert "redraft" in content.lower(), (
        "Agent must use 'redraft' as the loop-back narrative decision"
    )
