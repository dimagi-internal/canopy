"""Structural invariants for the ddd-narrative-review skill.

Pins the load-bearing strings in SKILL.md and the command file.
Does NOT exercise runtime behaviour — drift here is a bug.

Checked invariants:
    1.  SKILL.md exists with correct name in frontmatter.
    2.  Preamble (update check) present.
    3.  ## Inputs section names spec_path and run_id.
    4.  ## Procedure section present.
    5.  Resolved-invocation pattern: grep for DDD_REPO resolution.
    6.  Posts via scripts.ddd.narrative.
    7.  Presents the review URL to the user.
    8.  Presents the inline storyboard (scene arc: title → story beat).
    9.  Awaits the user's response via apply.
    10. Rethink → loop back to /ddd-spec (re-draft loop).
    11. Agree before render/build statement.
    12. concept_change gate mentioned.
    13. Command file exists with description and allowed-tools.
"""
from __future__ import annotations

from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent.parent / "plugins" / "canopy"
SKILL_DIR = PLUGIN_ROOT / "skills" / "ddd-narrative-review"
COMMANDS = PLUGIN_ROOT / "commands"

SKILL_FILE = SKILL_DIR / "SKILL.md"
COMMAND_FILE = COMMANDS / "ddd-narrative-review.md"


# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------


def test_skill_md_exists() -> None:
    assert SKILL_FILE.exists(), f"SKILL.md missing for ddd-narrative-review at {SKILL_FILE}"


def test_command_file_exists() -> None:
    assert COMMAND_FILE.exists(), f"command file missing for ddd-narrative-review at {COMMAND_FILE}"


# ---------------------------------------------------------------------------
# Frontmatter + preamble
# ---------------------------------------------------------------------------


def _frontmatter(path: Path) -> str:
    parts = path.read_text().split("---", 2)
    assert len(parts) >= 3, f"No frontmatter found in {path}"
    return parts[1]


def test_skill_name_in_frontmatter() -> None:
    fm = _frontmatter(SKILL_FILE)
    assert "name: ddd-narrative-review" in fm, (
        "SKILL.md frontmatter must declare 'name: ddd-narrative-review'"
    )


def test_skill_has_preamble_update_check() -> None:
    content = SKILL_FILE.read_text()
    assert "## Preamble (run first)" in content, "SKILL.md must have '## Preamble (run first)'"
    assert "canopy-update-check.sh" in content, "Preamble must reference canopy-update-check.sh"


# ---------------------------------------------------------------------------
# Inputs section
# ---------------------------------------------------------------------------


def test_skill_has_inputs_section() -> None:
    content = SKILL_FILE.read_text()
    assert "## Inputs" in content, "SKILL.md must have an ## Inputs section"


def test_skill_inputs_names_spec_path() -> None:
    content = SKILL_FILE.read_text()
    assert "spec_path" in content, "Inputs section must name 'spec_path'"


def test_skill_inputs_names_run_id() -> None:
    content = SKILL_FILE.read_text()
    assert "run_id" in content, "Inputs section must name 'run_id'"


# ---------------------------------------------------------------------------
# Procedure section
# ---------------------------------------------------------------------------


def test_skill_has_procedure_section() -> None:
    content = SKILL_FILE.read_text()
    assert "## Procedure" in content, "SKILL.md must have a ## Procedure section"


# ---------------------------------------------------------------------------
# Resolved-invocation pattern (DDD_REPO resolution — same as PR #63)
# ---------------------------------------------------------------------------


def test_skill_uses_ddd_repo_resolution() -> None:
    """Must use the resolved DDD_REPO pattern from PR #63."""
    content = SKILL_FILE.read_text()
    assert "DDD_REPO" in content, (
        "SKILL.md must resolve DDD_REPO (the PR #63 resolution pattern) "
        "so scripts/ddd is found whether running from the plugin cache or the checkout"
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
# Posts via scripts.ddd.narrative
# ---------------------------------------------------------------------------


def test_skill_invokes_narrative_post() -> None:
    """Procedure must invoke scripts.ddd.narrative (or python -m scripts.ddd.narrative)."""
    content = SKILL_FILE.read_text()
    assert "scripts.ddd.narrative" in content, (
        "SKILL.md must invoke 'python -m scripts.ddd.narrative' (or scripts.ddd.narrative)"
    )


def test_skill_mentions_post_subcommand() -> None:
    content = SKILL_FILE.read_text()
    assert " post " in content or " post\n" in content or '"post"' in content or "'post'" in content, (
        "SKILL.md procedure must invoke the 'post' subcommand of scripts.ddd.narrative"
    )


def test_skill_mentions_apply_subcommand() -> None:
    content = SKILL_FILE.read_text()
    assert " apply " in content or " apply\n" in content or '"apply"' in content or "'apply'" in content, (
        "SKILL.md procedure must invoke the 'apply' subcommand of scripts.ddd.narrative"
    )


# ---------------------------------------------------------------------------
# Presents URL + storyboard
# ---------------------------------------------------------------------------


def test_skill_presents_review_url() -> None:
    content = SKILL_FILE.read_text()
    assert "url" in content.lower() or "review url" in content.lower() or "URL" in content, (
        "SKILL.md must present the review URL to the user"
    )


def test_skill_presents_storyboard() -> None:
    """Must show an inline storyboard of the scene arc."""
    content = SKILL_FILE.read_text()
    storyboard_signals = [
        "storyboard",
        "scene arc",
        "story beat",
        "scene-by-scene",
    ]
    assert any(s in content.lower() for s in storyboard_signals), (
        "SKILL.md must present an inline storyboard (scene arc: title → story beat)"
    )


# ---------------------------------------------------------------------------
# Rethink → re-draft loop
# ---------------------------------------------------------------------------


def test_skill_routes_rethink_to_ddd_spec() -> None:
    """On rethink decision, skill must loop back to /ddd-spec."""
    content = SKILL_FILE.read_text()
    assert "rethink" in content.lower(), "SKILL.md must describe the 'rethink' decision"
    assert "ddd-spec" in content, (
        "On 'rethink', SKILL.md must direct the agent back to /ddd-spec to re-draft"
    )


def test_skill_rethink_prevents_render() -> None:
    """On rethink, agent must NOT proceed to render/build."""
    content = SKILL_FILE.read_text()
    no_render_signals = [
        "do not proceed",
        "don't proceed",
        "not proceed",
        "loop back",
    ]
    assert any(s.lower() in content.lower() for s in no_render_signals), (
        "SKILL.md must state that rethink prevents proceeding to render/build"
    )


# ---------------------------------------------------------------------------
# Agree-before-render/build statement
# ---------------------------------------------------------------------------


def test_skill_states_agree_before_render() -> None:
    """SKILL.md must explicitly state that narrative must be agreed before rendering."""
    content = SKILL_FILE.read_text()
    before_render_signals = [
        "before any rendering",
        "before rendering",
        "before any render",
        "before any build",
        "before building",
        "before judging",
    ]
    assert any(s.lower() in content.lower() for s in before_render_signals), (
        "SKILL.md must state that agreement is required BEFORE rendering, building, or judging"
    )


def test_skill_states_this_is_blocking() -> None:
    """Gate must be described as blocking (concept_change pause)."""
    content = SKILL_FILE.read_text()
    blocking_signals = [
        "blocking",
        "concept_change pause",
        "must not advance",
        "do not advance",
        "must not proceed",
    ]
    assert any(s.lower() in content.lower() for s in blocking_signals), (
        "SKILL.md must state that this is a blocking concept_change pause"
    )


# ---------------------------------------------------------------------------
# concept_change gate
# ---------------------------------------------------------------------------


def test_skill_mentions_concept_change_gate() -> None:
    content = SKILL_FILE.read_text()
    assert "concept_change" in content, (
        "SKILL.md must mention the 'concept_change' gate class"
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


def test_command_allowed_tools_includes_bash() -> None:
    content = COMMAND_FILE.read_text()
    assert "Bash" in content, "Command allowed-tools must include Bash"
