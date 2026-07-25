"""Single-command structural self-audit for the canopy plugin.

canopy documents a handful of structural invariants in `CLAUDE.md`
(Pattern B for command/skill collisions, the reserved built-in slash-command
namespace, the VERSION ↔ plugin.json ↔ marketplace.json version triple, and
the per-skill description char budget). Historically these were enforced only
by scattered pytest guards — there was no way to run the self-audit on demand
or gate CI on it from one place.

This module wires every documented invariant into one runnable check, mirroring
the spirit of ace's `detect-structure-drift`. Each invariant function returns a
list of structured findings:

    {"invariant": str, "severity": "error"|"warning", "detail": str}

`run_structure_drift()` composes them into a single report with an overall
`ok` flag. The Click command (`canopy structure-drift`) prints the findings and
exits 0 by default; with `--strict` it exits non-zero when any finding exists,
making it a CI gate.

The collision-detection logic here is the single source of truth — the existing
collision tests (tests/test_command_skill_collisions.py and
tests/test_builtin_command_collisions.py) keep their own assertions but the
core scanning is shared via the helpers below.
"""
from __future__ import annotations

from pathlib import Path

from orchestrator.skill_catalog import _parse_frontmatter_description
from orchestrator.skill_budget import DEFAULT_PER_SKILL_LIMIT
from orchestrator.version_bump import (
    find_version_files,
    find_marketplace_json,
    _read_version_file,
    _read_plugin_json_version,
    _read_marketplace_json_versions,
)

# Claude Code built-in slash command names. Naming a plugin skill / command /
# agent the same as a built-in causes a silent routing collision and can drop
# 100+ skill descriptions from the system prompt. Kept in sync with
# tests/test_builtin_command_collisions.py::RESERVED_BUILTINS.
RESERVED_BUILTINS: frozenset[str] = frozenset({
    "help",
    "clear",
    "doctor",
    "config",
    "compact",
    "model",
    "fast",
    "bug",
    "exit",
    "quit",
    "logout",
    "login",
    "ide",
    "vim",
    "release-notes",
    "cost",
    "memory",
    "agents",
    "mcp",
    "permissions",
})


# The plugin clone every agent's canopy install is served from. Same precedent
# as `agent_email.MARKETPLACE_CLONE` — it is a full canopy checkout on disk.
MARKETPLACE_CLONE = Path.home() / ".claude/plugins/marketplaces/canopy"


def is_canopy_checkout(path: Path) -> bool:
    """True when `path` is the root of a canopy source checkout.

    The two files every invariant check needs to mean anything: VERSION (the
    version triple) and the plugin tree (skills / commands / agents).
    """
    return (
        (path / "VERSION").is_file()
        and (path / "plugins" / "canopy" / ".claude-plugin" / "plugin.json").is_file()
    )


def default_repo_root() -> Path:
    """Best-effort canopy checkout to audit when `--repo` isn't given.

    Under `uv tool install` — how every agent actually runs canopy — this module
    lives in site-packages, so the shipped-from path is `.../lib/python3.x` and
    holds no plugin tree at all. Auditing it is worse than useless: three of the
    four invariant checks take their "directory missing -> return empty" path and
    report zero findings, so the audit LOOKS clean while checking nothing. Prefer,
    in order, any path that is genuinely a canopy checkout.
    """
    # 1. The checkout this module ships from (source / editable installs).
    #    src/orchestrator/structure_drift.py -> repo root is two parents up.
    shipped_from = Path(__file__).resolve().parent.parent.parent
    if is_canopy_checkout(shipped_from):
        return shipped_from

    # 2. CWD or an ancestor — running inside a canopy checkout or worktree.
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if is_canopy_checkout(candidate):
            return candidate

    # 3. The marketplace clone the installed plugin is served from.
    if is_canopy_checkout(MARKETPLACE_CLONE):
        return MARKETPLACE_CLONE

    # Nothing resolved. Return the shipped-from path unchanged; the caller
    # reports it as a `repo_root` finding rather than auditing it silently.
    return shipped_from


def _plugin_root(repo_root: Path) -> Path:
    return repo_root / "plugins" / "canopy"


# --------------------------------------------------------------------------
# Shared scanning helpers (single source of truth for the collision tests)
# --------------------------------------------------------------------------

def command_names(repo_root: Path) -> set[str]:
    commands_dir = _plugin_root(repo_root) / "commands"
    if not commands_dir.exists():
        return set()
    return {p.stem for p in commands_dir.glob("*.md")}


def skill_names(repo_root: Path) -> set[str]:
    skills_dir = _plugin_root(repo_root) / "skills"
    if not skills_dir.exists():
        return set()
    return {p.name for p in skills_dir.iterdir()
            if p.is_dir() and (p / "SKILL.md").is_file()}


def agent_names(repo_root: Path) -> set[str]:
    agents_dir = _plugin_root(repo_root) / "agents"
    if not agents_dir.exists():
        return set()
    return {p.stem for p in agents_dir.glob("*.md")}


def find_command_skill_collisions(repo_root: Path) -> list[str]:
    """Names that exist as both a command and a skill (collision candidates)."""
    return sorted(command_names(repo_root) & skill_names(repo_root))


def command_follows_pattern_b(repo_root: Path, name: str) -> bool:
    """A colliding command must reference its sibling `skills/<name>/SKILL.md`
    so the Read tool pulls in the real skill body (Pattern B)."""
    command_path = _plugin_root(repo_root) / "commands" / f"{name}.md"
    try:
        body = command_path.read_text()
    except OSError:
        return False
    return "SKILL.md" in body and f"skills/{name}/SKILL.md" in body


def all_plugin_names(repo_root: Path) -> dict[str, list[str]]:
    """Return {name: [kinds...]} across skills, commands, and agents."""
    names: dict[str, list[str]] = {}
    for n in skill_names(repo_root):
        names.setdefault(n, []).append("skill")
    for n in command_names(repo_root):
        names.setdefault(n, []).append("command")
    for n in agent_names(repo_root):
        names.setdefault(n, []).append("agent")
    return names


# --------------------------------------------------------------------------
# Invariant checks — each returns a list of findings
# --------------------------------------------------------------------------

INVARIANT_PATTERN_B = "command_skill_pattern_b"
INVARIANT_RESERVED_NAME = "reserved_builtin_name"
INVARIANT_VERSION_SYNC = "version_sync"
INVARIANT_SKILL_DESCRIPTION_BUDGET = "skill_description_budget"
INVARIANT_REPO_ROOT = "repo_root"


def check_repo_root(repo_root: Path) -> list[dict]:
    """The audit target must actually be a canopy checkout.

    Every other check degrades to "no findings" against a directory with no
    plugin tree, so a bad root reports a clean bill of health for invariants
    that were never scanned. Fail loudly instead.
    """
    if is_canopy_checkout(repo_root):
        return []
    return [{
        "invariant": INVARIANT_REPO_ROOT,
        "severity": "error",
        "detail": (
            f"{repo_root} is not a canopy checkout (no VERSION and/or "
            f"plugins/canopy/.claude-plugin/plugin.json), so NO invariant was "
            f"actually checked — this is not a clean result. Re-run from inside "
            f"a canopy checkout, or pass `--repo <path-to-canopy>`."
        ),
    }]


def check_command_skill_pattern_b(repo_root: Path) -> list[dict]:
    """Every command colliding with a skill must follow Pattern B."""
    findings: list[dict] = []
    for name in find_command_skill_collisions(repo_root):
        if not command_follows_pattern_b(repo_root, name):
            findings.append({
                "invariant": INVARIANT_PATTERN_B,
                "severity": "error",
                "detail": (
                    f"Command `{name}` collides with skill `{name}` but does not "
                    f"reference `skills/{name}/SKILL.md`. The Skill tool will "
                    f"silently re-serve the command body instead of the skill. "
                    f"Convert plugins/canopy/commands/{name}.md to Pattern B "
                    f"(read SKILL.md from disk, then Read it)."
                ),
            })
    return findings


def check_reserved_builtin_names(repo_root: Path) -> list[dict]:
    """No command/skill/agent may share a name with a built-in slash command."""
    findings: list[dict] = []
    for name, kinds in sorted(all_plugin_names(repo_root).items()):
        if name in RESERVED_BUILTINS:
            findings.append({
                "invariant": INVARIANT_RESERVED_NAME,
                "severity": "error",
                "detail": (
                    f"Plugin {'/'.join(kinds)} `{name}` collides with Claude Code "
                    f"built-in slash command `/{name}`. This causes silent "
                    f"system-prompt truncation (100+ skill descriptions can vanish). "
                    f"Rename it — e.g. `doctor` -> `canopy-doctor`."
                ),
            })
    return findings


def check_version_sync(repo_root: Path) -> list[dict]:
    """VERSION == plugin.json version == every marketplace.json version field."""
    findings: list[dict] = []
    try:
        v_path, p_path = find_version_files(repo_root)
    except FileNotFoundError as e:
        return [{
            "invariant": INVARIANT_VERSION_SYNC,
            "severity": "error",
            "detail": f"Could not locate version files: {e}",
        }]

    version = _read_version_file(v_path)
    try:
        plugin_version = _read_plugin_json_version(p_path)
    except ValueError as e:
        return [{
            "invariant": INVARIANT_VERSION_SYNC,
            "severity": "error",
            "detail": f"Could not read plugin.json version: {e}",
        }]

    if version != plugin_version:
        findings.append({
            "invariant": INVARIANT_VERSION_SYNC,
            "severity": "error",
            "detail": (
                f"VERSION ({version}) != plugin.json version ({plugin_version}). "
                f"Run `canopy version bump` to resolve."
            ),
        })

    mp_path = find_marketplace_json(repo_root)
    if mp_path is not None:
        mp_versions = _read_marketplace_json_versions(mp_path)
        for mp_v in mp_versions:
            if mp_v != version:
                findings.append({
                    "invariant": INVARIANT_VERSION_SYNC,
                    "severity": "error",
                    "detail": (
                        f"marketplace.json carries version {mp_v} but VERSION is "
                        f"{version}. Every marketplace.json version field must "
                        f"track VERSION. Run `canopy version bump`."
                    ),
                })

    return findings


def check_skill_description_budget(
    repo_root: Path,
    per_skill_limit: int = DEFAULT_PER_SKILL_LIMIT,
) -> list[dict]:
    """Any skill whose frontmatter description exceeds the per-skill char cap."""
    findings: list[dict] = []
    skills_dir = _plugin_root(repo_root) / "skills"
    if not skills_dir.exists():
        return findings
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        desc = _parse_frontmatter_description(skill_md).strip()
        size = len(desc)
        if size > per_skill_limit:
            findings.append({
                "invariant": INVARIANT_SKILL_DESCRIPTION_BUDGET,
                "severity": "warning",
                "detail": (
                    f"Skill `{skill_dir.name}` description is {size} chars, over the "
                    f"{per_skill_limit}-char per-skill cap. Claude Code truncates "
                    f"over-cap descriptions. Tighten plugins/canopy/skills/"
                    f"{skill_dir.name}/SKILL.md frontmatter."
                ),
            })
    return findings


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

def run_structure_drift(
    repo_root: Path | None = None,
    per_skill_limit: int = DEFAULT_PER_SKILL_LIMIT,
) -> dict:
    """Run every structural invariant check and return a combined report.

    Returns:
        {
            "ok": bool,                 # True when no findings
            "findings": [ {invariant, severity, detail}, ... ],
            "by_invariant": { invariant: [findings...] },
            "counts": {
                "total": N,
                "error": E,
                "warning": W,
            },
            "repo_root": str,
        }
    """
    root = repo_root or default_repo_root()

    findings: list[dict] = []
    # Gate: against a non-checkout every check below silently reports nothing,
    # so report the bad root alone rather than three fabricated passes.
    root_findings = check_repo_root(root)
    if root_findings:
        findings += root_findings
    else:
        findings += check_command_skill_pattern_b(root)
        findings += check_reserved_builtin_names(root)
        findings += check_version_sync(root)
        findings += check_skill_description_budget(root, per_skill_limit=per_skill_limit)

    by_invariant: dict[str, list[dict]] = {}
    for f in findings:
        by_invariant.setdefault(f["invariant"], []).append(f)

    error_count = sum(1 for f in findings if f["severity"] == "error")
    warning_count = sum(1 for f in findings if f["severity"] == "warning")

    return {
        "ok": not findings,
        "findings": findings,
        "by_invariant": by_invariant,
        "counts": {
            "total": len(findings),
            "error": error_count,
            "warning": warning_count,
        },
        "repo_root": str(root),
    }
