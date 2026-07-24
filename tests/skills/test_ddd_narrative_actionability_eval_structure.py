"""Structural invariants for the ddd-narrative-actionability-eval skill (DDD v3).

These tests pin the load-bearing strings and shape of the rubric, SKILL.md,
and command file. They do NOT exercise runtime behavior. Drift here is a bug.

Checked invariants:
    1.  SKILL.md exists with correct name in frontmatter.
    2.  rubric.yaml exists with 4 weighted dims summing to 1.0.
    3.  Command file exists with description and allowed-tools.
    4.  SKILL.md describes the cold-derive-then-score method.
    5.  SKILL.md states the actionability gate (low score → must revise).
    6.  SKILL.md references the QA gate (ddd-spec-qa).
    7.  SKILL.md emits the visual-judge verdict shape.
    8.  rubric.yaml has default_score: 3 on every dimension.
    9.  rubric.yaml overall_rule: lowest.
    10. rubric.yaml dimensions: coverage, specificity, correctness, consistency.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).parent.parent.parent / "plugins" / "canopy"
SKILL_DIR = PLUGIN_ROOT / "skills" / "ddd-narrative-actionability-eval"
COMMANDS = PLUGIN_ROOT / "commands"

SKILL_FILE = SKILL_DIR / "SKILL.md"
RUBRIC_FILE = SKILL_DIR / "rubric.yaml"
COMMAND_FILE = COMMANDS / "ddd-narrative-actionability-eval.md"

EXPECTED_DIM_NAMES = {"coverage", "specificity", "correctness", "consistency"}


# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------


def test_skill_md_exists() -> None:
    assert SKILL_FILE.exists(), f"SKILL.md missing for ddd-narrative-actionability-eval at {SKILL_FILE}"


def test_rubric_yaml_exists() -> None:
    assert RUBRIC_FILE.exists(), f"rubric.yaml missing for ddd-narrative-actionability-eval at {RUBRIC_FILE}"


def test_command_file_exists() -> None:
    assert COMMAND_FILE.exists(), f"command file missing at {COMMAND_FILE}"


# ---------------------------------------------------------------------------
# Rubric shape: 4 dims, correct names, weights sum to 1.0
# ---------------------------------------------------------------------------


def test_rubric_has_four_dimensions() -> None:
    rubric = yaml.safe_load(RUBRIC_FILE.read_text())
    dims = {d["id"] for d in rubric["dimensions"]}
    assert dims == EXPECTED_DIM_NAMES, (
        f"Expected dimensions {EXPECTED_DIM_NAMES}, got {dims}"
    )


def test_rubric_weights_sum_to_one() -> None:
    rubric = yaml.safe_load(RUBRIC_FILE.read_text())
    total = sum(d["weight"] for d in rubric["dimensions"])
    assert total == pytest.approx(1.0), f"Weights sum to {total}, expected 1.0"


def test_rubric_coverage_weight_is_35() -> None:
    rubric = yaml.safe_load(RUBRIC_FILE.read_text())
    coverage = next(d for d in rubric["dimensions"] if d["id"] == "coverage")
    assert coverage["weight"] == pytest.approx(0.35), (
        f"coverage weight should be 0.35, got {coverage['weight']}"
    )


def test_rubric_specificity_weight_is_25() -> None:
    rubric = yaml.safe_load(RUBRIC_FILE.read_text())
    dim = next(d for d in rubric["dimensions"] if d["id"] == "specificity")
    assert dim["weight"] == pytest.approx(0.25), (
        f"specificity weight should be 0.25, got {dim['weight']}"
    )


def test_rubric_correctness_weight_is_20() -> None:
    rubric = yaml.safe_load(RUBRIC_FILE.read_text())
    dim = next(d for d in rubric["dimensions"] if d["id"] == "correctness")
    assert dim["weight"] == pytest.approx(0.20), (
        f"correctness weight should be 0.20, got {dim['weight']}"
    )


def test_rubric_consistency_weight_is_20() -> None:
    rubric = yaml.safe_load(RUBRIC_FILE.read_text())
    dim = next(d for d in rubric["dimensions"] if d["id"] == "consistency")
    assert dim["weight"] == pytest.approx(0.20), (
        f"consistency weight should be 0.20, got {dim['weight']}"
    )


def test_rubric_each_dim_has_default_score_3() -> None:
    rubric = yaml.safe_load(RUBRIC_FILE.read_text())
    for dim in rubric["dimensions"]:
        assert "default_score" in dim, f"Dimension '{dim['id']}' missing 'default_score'"
        assert dim["default_score"] == 3, (
            f"Dimension '{dim['id']}' default_score should be 3, got {dim['default_score']}"
        )


def test_rubric_has_overall_rule_lowest() -> None:
    rubric = yaml.safe_load(RUBRIC_FILE.read_text())
    assert rubric.get("overall_rule") == "lowest", (
        f"overall_rule should be 'lowest', got {rubric.get('overall_rule')}"
    )


def test_rubric_each_dim_has_anchor() -> None:
    rubric = yaml.safe_load(RUBRIC_FILE.read_text())
    for dim in rubric["dimensions"]:
        assert "anchor" in dim, f"Dimension '{dim['id']}' missing 'anchor'"
        assert dim["anchor"], f"Dimension '{dim['id']}' has empty anchor"


def test_rubric_each_dim_has_deduction_rules() -> None:
    rubric = yaml.safe_load(RUBRIC_FILE.read_text())
    for dim in rubric["dimensions"]:
        rules = dim.get("deduction_rules")
        assert rules is not None, f"Dimension '{dim['id']}' missing 'deduction_rules'"
        assert len(rules) > 0, f"Dimension '{dim['id']}' has empty 'deduction_rules'"


# ---------------------------------------------------------------------------
# SKILL.md: frontmatter + preamble
# ---------------------------------------------------------------------------


def _frontmatter(path: Path) -> str:
    parts = path.read_text().split("---", 2)
    assert len(parts) >= 3, f"No frontmatter found in {path}"
    return parts[1]


def test_skill_name_in_frontmatter() -> None:
    fm = _frontmatter(SKILL_FILE)
    assert "name: ddd-narrative-actionability-eval" in fm, (
        "SKILL.md frontmatter must declare 'name: ddd-narrative-actionability-eval'"
    )


def test_skill_has_preamble_update_check() -> None:
    content = SKILL_FILE.read_text()
    assert "## Preamble (run first)" in content, "SKILL.md must have '## Preamble (run first)'"
    assert "canopy-update-check.sh" in content, "Preamble must reference canopy-update-check.sh"


def test_skill_has_inputs_section() -> None:
    content = SKILL_FILE.read_text()
    assert "## Inputs" in content, "SKILL.md must have an ## Inputs section"


def test_skill_inputs_names_unified_spec() -> None:
    content = SKILL_FILE.read_text()
    assert "unified_spec" in content, "Inputs must reference 'unified_spec'"


# ---------------------------------------------------------------------------
# SKILL.md: QA gate
# ---------------------------------------------------------------------------


def test_skill_has_qa_gate() -> None:
    """Skill must be gated by ddd-spec-qa."""
    content = SKILL_FILE.read_text()
    assert "ddd-spec-qa" in content, (
        "SKILL.md must state it is gated by ddd-spec-qa"
    )
    # Must skip if QA failed
    gate_signals = [
        "skip if QA failed",
        "skip if qa failed",
        "Gated by ddd-spec-qa",
        "gated by ddd-spec-qa",
        "QA gate",
        "spec-qa",
    ]
    assert any(s.lower() in content.lower() for s in gate_signals), (
        "SKILL.md must state it skips or is gated when spec-qa fails"
    )


# ---------------------------------------------------------------------------
# SKILL.md: cold-derive method
# ---------------------------------------------------------------------------


def test_skill_describes_cold_derivation() -> None:
    """SKILL.md must describe the cold-derive step (read narration/show, write build plan)."""
    content = SKILL_FILE.read_text()
    cold_signals = [
        "cold deriv",
        "cold-deriv",
        "cold derive",
        "independently",
    ]
    assert any(s.lower() in content.lower() for s in cold_signals), (
        "SKILL.md must describe the cold-derivation step "
        "(read only narration/concept_claim/show, write build plan independently)"
    )


def test_skill_cold_derive_uses_narration_not_features() -> None:
    """Cold derivation must read narration/concept_claim/show only (NOT declared features)."""
    content = SKILL_FILE.read_text()
    not_features_signals = [
        "NOT its features",
        "not its features",
        "NOT the declared features",
        "not the features",
        "without looking at features",
        "ignoring the features",
        "only the narration",
        "only narration",
    ]
    assert any(s.lower() in content.lower() for s in not_features_signals), (
        "SKILL.md must state that cold derivation reads narration/concept_claim/show "
        "but NOT the declared features[]"
    )


def test_skill_describes_self_consistency() -> None:
    """SKILL.md must describe the self-consistency check (~3 independent derivations)."""
    content = SKILL_FILE.read_text()
    consistency_signals = [
        "self-consistency",
        "self consistency",
        "3 times",
        "three times",
        "~3",
        "independent derivation",
        "independently",
    ]
    assert any(s.lower() in content.lower() for s in consistency_signals), (
        "SKILL.md must describe running cold derivation ~3 times for self-consistency"
    )


# ---------------------------------------------------------------------------
# SKILL.md: scoring — compare cold-derived vs declared features
# ---------------------------------------------------------------------------


def test_skill_scores_coverage() -> None:
    content = SKILL_FILE.read_text()
    assert "coverage" in content.lower(), (
        "SKILL.md must describe the coverage dimension "
        "(did cold derivation infer all declared features?)"
    )


def test_skill_scores_specificity() -> None:
    content = SKILL_FILE.read_text()
    assert "specificity" in content.lower(), (
        "SKILL.md must describe the specificity dimension "
        "(are inferred items concrete/buildable, or hand-wavy?)"
    )


def test_skill_scores_correctness() -> None:
    content = SKILL_FILE.read_text()
    assert "correctness" in content.lower(), (
        "SKILL.md must describe the correctness dimension "
        "(do inferred items match declared intent?)"
    )


def test_skill_scores_consistency() -> None:
    content = SKILL_FILE.read_text()
    assert "consistency" in content.lower(), (
        "SKILL.md must describe the consistency dimension "
        "(did the ~3 derivations agree?)"
    )


# ---------------------------------------------------------------------------
# SKILL.md: verdict shape
# ---------------------------------------------------------------------------


def test_skill_emits_verdict_shape() -> None:
    content = SKILL_FILE.read_text()
    assert "verdict" in content
    assert "overall_score" in content
    assert "fix_recommendation" in content or "fix_recommendation" in content


def test_skill_emits_actionability_findings() -> None:
    """SKILL.md must describe the per-scene actionability_findings[] output."""
    content = SKILL_FILE.read_text()
    assert "actionability_findings" in content, (
        "SKILL.md must emit 'actionability_findings[]' listing "
        "declared features the cold derivation missed or got wrong"
    )


def test_skill_verdict_uses_pass_warn_fail() -> None:
    """Verdict values must be pass/warn/fail (not blocked — no QA precheck emits blocked here)."""
    content = SKILL_FILE.read_text()
    assert "pass" in content
    assert "warn" in content
    assert "fail" in content


# ---------------------------------------------------------------------------
# SKILL.md: actionability gate statement
# ---------------------------------------------------------------------------


def test_skill_states_actionability_gate() -> None:
    """SKILL.md must state that a low score means the narrative is too vague to act on."""
    content = SKILL_FILE.read_text()
    gate_signals = [
        "too vague to act on",
        "too vague to build",
        "must be revised",
        "narrative must be revised",
        "gates the narrative",
        "gate",
    ]
    assert any(s.lower() in content.lower() for s in gate_signals), (
        "SKILL.md must state that a low actionability score means the narrative "
        "is too vague to act on and must be revised"
    )


# ---------------------------------------------------------------------------
# SKILL.md: DDD_REPO resolution pattern
# ---------------------------------------------------------------------------


def test_skill_uses_ddd_repo_resolution() -> None:
    """Must use the resolved DDD_REPO pattern."""
    content = SKILL_FILE.read_text()
    assert "DDD_REPO" in content, (
        "SKILL.md must resolve DDD_REPO so scripts/ddd is found correctly"
    )
    assert "canopy-runtime.sh" in content, (
        "DDD_REPO must resolve via the shared resolver scripts/canopy-runtime.sh "
        "(the bundled-runtime idiom; direct checkout paths are banned by "
        "tests/test_plugin_runtime_resolution.py)"
    )


def test_skill_ddd_repo_has_fallback() -> None:
    """DDD_REPO resolution must have the marketplace fallback."""
    content = SKILL_FILE.read_text()
    assert "installed_plugins.json" in content, (
        "DDD_REPO resolution must locate the resolver via installPath from "
        "installed_plugins.json (the ladder inside canopy-runtime.sh handles "
        "marketplace/dev fallbacks)"
    )


# ---------------------------------------------------------------------------
# Command file shape
# ---------------------------------------------------------------------------


def test_command_has_description_in_frontmatter() -> None:
    fm = _frontmatter(COMMAND_FILE)
    assert "description:" in fm, "Command file must have a 'description:' in frontmatter"


def test_command_has_allowed_tools() -> None:
    content = COMMAND_FILE.read_text()
    assert "allowed-tools" in content, "Command file must declare 'allowed-tools'"


def test_command_allowed_tools_includes_skill_or_agent() -> None:
    content = COMMAND_FILE.read_text()
    assert "Skill" in content or "Agent" in content, (
        "Command allowed-tools must include Skill or Agent"
    )


def test_skill_verdict_carries_out_of_chain_metadata() -> None:
    """canopy#265 item 3: actionability grades narration text against declared
    features[] — no live state — so the verdict must self-describe as such."""
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "kind: actionability" in content
    assert "gate: advisory" in content
    assert "live_state_verified: false" in content
    assert "calibration: provisional" in content
