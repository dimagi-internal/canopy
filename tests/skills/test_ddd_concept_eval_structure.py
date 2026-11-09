"""Structural invariants for the ddd-concept-eval skill (SP3).

These tests pin the load-bearing strings and shape of the rubric, SKILL.md,
and command file.  They do NOT exercise behavior.  Drift here is a bug.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).parent.parent.parent / "plugins" / "canopy"
SKILL_DIR = PLUGIN_ROOT / "skills" / "ddd-concept-eval"
COMMANDS = PLUGIN_ROOT / "commands"
RUBRIC_PATH = SKILL_DIR / "rubric.yaml"

EXPECTED_DIMS = {
    "concept_clarity",
    "use_case_soundness",     # added in 11729cd — catch trivial use cases, even post-acceptance
    "design_soundness",
    "visual_polish",          # carved out of design_soundness in v0.2.153
    "why_groundedness",
    "claim_reality_coherence",
    "motion_friction",
}

EXPECTED_ROUTES = {"PRODUCT", "CONCEPT", "RESEARCH", "DEFER"}


# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------


def test_skill_md_exists() -> None:
    assert (SKILL_DIR / "SKILL.md").exists(), "SKILL.md missing for ddd-concept-eval"


def test_rubric_yaml_exists() -> None:
    assert RUBRIC_PATH.exists(), "rubric.yaml missing for ddd-concept-eval"


def test_command_file_exists() -> None:
    assert (COMMANDS / "ddd-concept-eval.md").exists(), "command file missing for ddd-concept-eval"


# ---------------------------------------------------------------------------
# Rubric shape
# ---------------------------------------------------------------------------


def test_rubric_has_expected_dimensions() -> None:
    rubric = yaml.safe_load(RUBRIC_PATH.read_text())
    dims = {d["id"] for d in rubric["dimensions"]}
    assert dims == EXPECTED_DIMS, f"Expected dims {EXPECTED_DIMS}, got {dims}"


def test_rubric_weights_sum_to_one() -> None:
    rubric = yaml.safe_load(RUBRIC_PATH.read_text())
    total = sum(d["weight"] for d in rubric["dimensions"])
    assert total == pytest.approx(1.0), f"Weights sum to {total}, expected 1.0"


def test_rubric_claim_reality_coherence_is_non_blocking() -> None:
    rubric = yaml.safe_load(RUBRIC_PATH.read_text())
    crc = next(d for d in rubric["dimensions"] if d["id"] == "claim_reality_coherence")
    assert crc.get("blocking") is False, (
        "claim_reality_coherence must have blocking: false"
    )


def test_rubric_has_overall_rule_lowest() -> None:
    rubric = yaml.safe_load(RUBRIC_PATH.read_text())
    assert rubric.get("overall_rule") == "lowest", (
        f"overall_rule should be 'lowest', got {rubric.get('overall_rule')}"
    )


def test_rubric_each_dim_has_anchor() -> None:
    rubric = yaml.safe_load(RUBRIC_PATH.read_text())
    for dim in rubric["dimensions"]:
        assert "anchor" in dim, f"Dimension '{dim['id']}' missing 'anchor'"
        assert dim["anchor"], f"Dimension '{dim['id']}' has empty anchor"


def test_rubric_each_dim_has_default_score_3() -> None:
    rubric = yaml.safe_load(RUBRIC_PATH.read_text())
    for dim in rubric["dimensions"]:
        assert "default_score" in dim, f"Dimension '{dim['id']}' missing 'default_score'"
        assert dim["default_score"] == 3, (
            f"Dimension '{dim['id']}' default_score should be 3, got {dim['default_score']}"
        )


def test_rubric_declares_design_findings_output() -> None:
    """Rubric must declare the design_findings output schema with all 4 route values."""
    rubric = yaml.safe_load(RUBRIC_PATH.read_text())
    df = rubric.get("design_findings_output")
    assert df is not None, "rubric missing 'design_findings_output' top-level key"
    routes = set(df.get("route_values", []))
    assert routes == EXPECTED_ROUTES, (
        f"design_findings_output.route_values should be {EXPECTED_ROUTES}, got {routes}"
    )


# ---------------------------------------------------------------------------
# Calibration status (SP3.3)
# ---------------------------------------------------------------------------


def test_calibration_note_present() -> None:
    """rubric.yaml or SKILL.md must contain the calibration status note."""
    rubric_text = RUBRIC_PATH.read_text()
    skill_text = (SKILL_DIR / "SKILL.md").read_text()
    combined = rubric_text + skill_text
    assert "Provisional rubric" in combined, (
        "Calibration status note ('Provisional rubric') missing from rubric.yaml or SKILL.md"
    )
    assert "calibrate" in combined.lower(), (
        "Calibration status note must mention 'calibrate'"
    )
    assert "Not yet calibrated" in combined, (
        "Calibration status note must say 'Not yet calibrated'"
    )


# ---------------------------------------------------------------------------
# SKILL.md content
# ---------------------------------------------------------------------------


def test_skill_name_in_frontmatter() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    fm = content.split("---", 2)[1]
    assert "name: ddd-concept-eval" in fm


def test_skill_has_preamble_update_check() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "## Preamble (run first)" in content
    assert "canopy-update-check.sh" in content


def test_skill_has_inputs_section() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "## Inputs" in content


def test_skill_inputs_names_walkthrough_run_dir() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "run dir" in content.lower() or "run_dir" in content


def test_skill_inputs_names_unified_spec() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "unified_spec" in content or "unified-spec" in content or "unified_spec.yaml" in content


def test_skill_inputs_names_why_brief() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "why_brief" in content or "why-brief" in content


def test_skill_has_qa_gate_sentence() -> None:
    """Skill must gate on ddd-spec-qa result."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "ddd-spec-qa returned verdict: fail" in content, (
        "SKILL.md must say 'If ddd-spec-qa returned verdict: fail, skip this eval.'"
    )


def test_skill_reuses_visual_judge() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "visual-judge" in content or "canopy:visual-judge" in content, (
        "SKILL.md must reference canopy:visual-judge for per-scene scoring"
    )


def test_skill_emits_verdict_concept_yaml() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "verdict-concept.yaml" in content, (
        "SKILL.md must state it writes verdict-concept.yaml to the run dir"
    )


def test_skill_emits_design_findings_json() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "design_findings.json" in content, (
        "SKILL.md must state it writes design_findings.json to the run dir"
    )


def test_skill_has_non_blocking_claim_reality_sentence() -> None:
    """SKILL.md must explicitly say claim_reality_coherence is non-blocking."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "claim_reality_coherence" in content
    # The spec requires this exact phrasing or close equivalent
    assert "NEVER set verdict=blocked" in content or "never set verdict=blocked" in content.lower(), (
        "SKILL.md must say claim_reality_coherence findings NEVER set verdict=blocked"
    )


def test_skill_emits_verdict_shape() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "verdict" in content
    assert "fix_recommendation" in content
    assert "overall_score" in content


# ---------------------------------------------------------------------------
# Fix 4 — SP3 review additions
# ---------------------------------------------------------------------------


def test_rubric_each_dim_has_deduction_rules() -> None:
    """Every dimension must declare a non-empty deduction_rules list."""
    rubric = yaml.safe_load(RUBRIC_PATH.read_text())
    for dim in rubric["dimensions"]:
        rules = dim.get("deduction_rules")
        assert rules is not None, f"Dimension '{dim['id']}' missing 'deduction_rules'"
        assert len(rules) > 0, f"Dimension '{dim['id']}' has empty 'deduction_rules'"


def test_rubric_claim_reality_coherence_is_advisory() -> None:
    """claim_reality_coherence must carry advisory: true to signal exclusion from overall_score."""
    rubric = yaml.safe_load(RUBRIC_PATH.read_text())
    crc = next(d for d in rubric["dimensions"] if d["id"] == "claim_reality_coherence")
    assert crc.get("advisory") is True, (
        "claim_reality_coherence must have advisory: true in the rubric"
    )


def test_skill_claim_reality_excluded_from_overall_score() -> None:
    """SKILL.md must explicitly state claim_reality_coherence is excluded from overall_score."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    # Check for the load-bearing phrase written in Fix 1
    assert "EXCLUDED from the weakest-link overall_score" in content, (
        "SKILL.md must state claim_reality_coherence is 'EXCLUDED from the weakest-link overall_score'"
    )


# ---------------------------------------------------------------------------
# Command file
# ---------------------------------------------------------------------------


def test_command_has_description() -> None:
    content = (COMMANDS / "ddd-concept-eval.md").read_text()
    fm = content.split("---", 2)[1]
    assert "description:" in fm


def test_command_has_allowed_tools() -> None:
    content = (COMMANDS / "ddd-concept-eval.md").read_text()
    assert "allowed-tools" in content


def test_command_allowed_tools_includes_skill() -> None:
    content = (COMMANDS / "ddd-concept-eval.md").read_text()
    # Must allow Agent or Skill to dispatch visual-judge sub-skill
    assert "Skill" in content or "Agent" in content, (
        "Command allowed-tools must include Skill or Agent to dispatch visual-judge"
    )


def test_skill_verdict_carries_live_state_metadata() -> None:
    """The concept judge scores live per-scene screenshots, so its verdict
    declares live_state_verified: true and participates in gating."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "kind: concept" in content
    assert "gate: gating" in content
    assert "live_state_verified: true" in content
    assert "calibration: provisional" in content
