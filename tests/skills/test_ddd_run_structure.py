"""Structural invariants for the ddd-run skill (SP4.3).

Pins the load-bearing strings and shape of SKILL.md and the command file.
Does NOT exercise behavior — drift here is a bug.

Checked invariants:
    1. SKILL.md exists with correct name in frontmatter.
    2. Preamble (update check) present.
    3. ## Inputs section names run_id, unified_spec, and why_brief.
    4. ## Procedure section present.
    5. QA-gate step: references python -m scripts.ddd.spec_qa.
    6. Render step: references the canopy walkthrough engine
       (canopy:walkthrough or walkthrough skill).
    7. Concept judge step: references ddd-concept-eval.
    8. User-artifact judge step: references visual-judge (canopy:visual-judge
       or visual-judge) with audience="feature user" signal.
    9. Both judges are dispatched ("parallel" or "both" or two separate calls).
   10. Assemble+convergence step: references assemble_run_state and
       compute_convergence.
   11. Command file exists with description, allowed-tools (incl. Skill or Agent).
"""
from __future__ import annotations

from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent.parent / "plugins" / "canopy"
SKILL_DIR = PLUGIN_ROOT / "skills" / "ddd-run"
COMMANDS = PLUGIN_ROOT / "commands"


# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------


def test_skill_md_exists() -> None:
    assert (SKILL_DIR / "SKILL.md").exists(), "SKILL.md missing for ddd-run"


def test_command_file_exists() -> None:
    assert (COMMANDS / "ddd-run.md").exists(), "command file missing for ddd-run"


# ---------------------------------------------------------------------------
# Frontmatter + preamble
# ---------------------------------------------------------------------------


def test_skill_name_in_frontmatter() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    fm = content.split("---", 2)[1]
    assert "name: ddd-run" in fm, "SKILL.md frontmatter must declare 'name: ddd-run'"


def test_skill_has_preamble_update_check() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "## Preamble (run first)" in content
    assert "canopy-update-check.sh" in content


# ---------------------------------------------------------------------------
# Inputs section
# ---------------------------------------------------------------------------


def test_skill_has_inputs_section() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "## Inputs" in content


def test_skill_inputs_names_run_id() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "run_id" in content, "Inputs must name the run_id"


def test_skill_inputs_names_unified_spec() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "unified_spec" in content or "unified_spec.yaml" in content, (
        "Inputs must name unified_spec"
    )


def test_skill_inputs_names_why_brief() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "why_brief" in content or "why_brief.yaml" in content, (
        "Inputs must name why_brief"
    )


# ---------------------------------------------------------------------------
# Procedure section
# ---------------------------------------------------------------------------


def test_skill_has_procedure_section() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "## Procedure" in content


def test_skill_has_qa_gate_step(  ) -> None:
    """Step 1: must reference scripts.ddd.spec_qa to gate on malformed specs."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "scripts.ddd.spec_qa" in content, (
        "Procedure must run python -m scripts.ddd.spec_qa as a gate step"
    )


def test_skill_has_render_step_naming_walkthrough_engine() -> None:
    """Step 2: must reference the canopy walkthrough engine."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    # Accept either form: canopy:walkthrough or just 'walkthrough'
    assert "canopy:walkthrough" in content or "walkthrough" in content.lower(), (
        "Procedure must name the canopy walkthrough engine for the render step"
    )


def test_skill_has_concept_judge_step() -> None:
    """Step 3a: must reference ddd-concept-eval."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "ddd-concept-eval" in content, (
        "Procedure must dispatch ddd-concept-eval as the concept judge"
    )


def test_skill_has_user_artifact_judge_step() -> None:
    """Step 3b: must reference visual-judge for the user-artifact judge."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "visual-judge" in content or "canopy:visual-judge" in content, (
        "Procedure must reference canopy:visual-judge for the user-artifact judge"
    )


def test_skill_user_artifact_judge_mentions_audience() -> None:
    """User-artifact judge must be qualified with audience='feature user'."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    # The audience qualifier must be present — accept various quoting styles
    assert "feature user" in content, (
        "User-artifact judge must specify audience='feature user'"
    )


def test_skill_both_judges_present() -> None:
    """Both concept judge and user-artifact judge must appear together (parallel step)."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    # Both must appear; parallel dispatch signal: 'parallel' or 'both' or just both refs
    has_concept = "ddd-concept-eval" in content
    has_user_artifact = "visual-judge" in content or "canopy:visual-judge" in content
    assert has_concept and has_user_artifact, (
        "Both ddd-concept-eval and canopy:visual-judge must be present as judges"
    )


def test_skill_has_assemble_step() -> None:
    """Step 4: must reference assemble_run_state."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "assemble_run_state" in content, (
        "Procedure must call run_pipeline.assemble_run_state to write verdict paths"
    )


def test_skill_has_convergence_step() -> None:
    """Step 4: must reference compute_convergence."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "compute_convergence" in content, (
        "Procedure must call run_pipeline.compute_convergence to report convergence"
    )


def test_skill_wires_extra_verdicts_into_aggregator() -> None:
    """Step 4 must plug the generic aggregator in (canopy#273 item 1): discover
    any out-of-chain verdict artifacts in the run dir via load_verdict /
    discover_extra_verdicts and pass them through extra_verdict_paths +
    compute_convergence(extra=...).

    Since 0.2.525 Step 4 is ONE command, ``python -m scripts.ddd.assemble`` (agents
    kept hand-writing their own assemble scripts), so the wiring is pinned in that
    module's source and the SKILL must route Step 4 through it."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "scripts.ddd.assemble" in content, "Step 4 must run python -m scripts.ddd.assemble"
    src = (PLUGIN_ROOT.parent.parent / "scripts" / "ddd" / "assemble.py").read_text()
    assert "discover_extra_verdicts" in src, (
        "assemble must discover extra verdicts via scripts.ddd.verdicts.discover_extra_verdicts"
    )
    assert "load_verdict" in src, (
        "assemble must load the gating pair via scripts.ddd.verdicts.load_verdict"
    )
    assert "extra_verdict_paths=" in src, (
        "assemble must record extras via assemble_run_state(extra_verdict_paths=...)"
    )
    assert "compute_convergence(concept, user, extra=extra)" in src, (
        "assemble must pass extras via compute_convergence(extra=...)"
    )
    for artifact in (
        "verdict-timing.json",
        "verdict-video.json",
        "verdict-why.yaml",
        "verdict-actionability.yaml",
    ):
        assert artifact in content, (
            f"Step 4 must name {artifact} as a discoverable extra verdict"
        )


def test_skill_stamps_user_verdict_metadata() -> None:
    """verdict-user.yaml must carry the unified verdict metadata stamp
    (canopy#273 item 2) — kind: user_artifact / gate: gating /
    live_state_verified: true. This is the one gating stamp written by THIS
    skill (the concept judge stamps its own); silent drift here would turn the
    user-artifact verdict advisory and break convergence."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "kind: user_artifact" in content, (
        "SKILL.md must stamp kind: user_artifact on verdict-user.yaml"
    )
    assert "gate: gating" in content, (
        "SKILL.md must stamp gate: gating on verdict-user.yaml"
    )
    assert "live_state_verified: true" in content, (
        "SKILL.md must stamp live_state_verified: true on verdict-user.yaml"
    )


def test_skill_report_renders_cap_visible_verdict_lines() -> None:
    """Step 5's summary must render verdict lines via
    run_pipeline.format_verdict_line (canopy#273 item 3), so a capped verdict
    ("capped from X — not live-state verified") is never indistinguishable
    from an honest score."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "format_verdict_line" in content, (
        "Step 5 must render verdict lines via run_pipeline.format_verdict_line"
    )
    assert "capped from" in content, (
        "Step 5 must document the capped-score rendering (capped from X)"
    )


# ---------------------------------------------------------------------------
# Command file
# ---------------------------------------------------------------------------


def test_command_has_description() -> None:
    content = (COMMANDS / "ddd-run.md").read_text()
    fm = content.split("---", 2)[1]
    assert "description:" in fm, "Command file must have a description in frontmatter"


def test_command_has_allowed_tools() -> None:
    content = (COMMANDS / "ddd-run.md").read_text()
    assert "allowed-tools" in content, "Command file must declare allowed-tools"


def test_command_allowed_tools_includes_skill_or_agent() -> None:
    content = (COMMANDS / "ddd-run.md").read_text()
    assert "Skill" in content or "Agent" in content, (
        "Command allowed-tools must include Skill or Agent to dispatch sub-skills"
    )
