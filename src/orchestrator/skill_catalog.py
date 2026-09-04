"""Enumerate available Claude Code skills across user, project, and plugin scopes.

Used by the proposer to avoid suggesting skills that already exist, and by the
`canopy skills list` CLI for inspection.

Catalog entry shape:
    {
        "name": "doctor",                  # bare skill name
        "qualified": "canopy:doctor",      # plugin:skill, or just name for user-scope
        "scope": "plugin" | "user",        # where it lives
        "source": "canopy" | "user",       # plugin name, or "user" for ~/.claude/skills/
        "description": "...",              # one-line description from frontmatter
        "path": "/abs/path/to/SKILL.md",
        "installed_version": "0.2.79",     # plugin version from installed_plugins.json (None for user)
        "cache_path": "/abs/path/to/<plugin>/<plugin>/<version>/",  # plugin install dir (None for user)
    }
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

PLUGIN_CACHE = Path.home() / ".claude" / "plugins" / "cache"
USER_SKILLS = Path.home() / ".claude" / "skills"
INSTALLED_PLUGINS_JSON = Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def _read_installed_plugin_versions(
    installed_json: Path = INSTALLED_PLUGINS_JSON,
) -> dict[str, str]:
    """Map plugin name → installed version from installed_plugins.json.

    Each plugins[name] is a list of installations; we take the first entry.
    Names in installed_plugins.json are typically `<plugin>@<marketplace>` —
    we key by the bare plugin name (stem before `@`) so the catalog scan
    (which reads cache dir names like `canopy`) can join cleanly.

    Returns an empty dict if the file is missing or malformed.
    """
    try:
        data = json.loads(installed_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for raw_name, infos in (data.get("plugins") or {}).items():
        if not infos:
            continue
        bare = raw_name.split("@", 1)[0]
        version = infos[0].get("version")
        if version and version != "unknown":
            out.setdefault(bare, version)
    return out


def _parse_frontmatter_description(skill_md: Path) -> str:
    """Extract the `description` field from a SKILL.md frontmatter block.

    Returns "" if the file is missing or has no description.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return ""
    frontmatter = parts[1]
    desc_lines: list[str] = []
    in_desc = False
    for line in frontmatter.splitlines():
        if line.startswith("description:"):
            in_desc = True
            value = line.split(":", 1)[1].strip()
            if value and value not in (">", "|"):
                return value.strip("\"'")
            continue
        if in_desc:
            if line and not line.startswith(" ") and not line.startswith("\t"):
                break
            stripped = line.strip()
            if stripped:
                desc_lines.append(stripped)
    return " ".join(desc_lines).strip("\"'")


def _scan_plugin_caches(cache_root: Path = PLUGIN_CACHE) -> list[dict]:
    """Scan ~/.claude/plugins/cache/<plugin>/<plugin>/<version>/{skills,commands,agents}.

    Plugins expose three kinds of named entities to the assistant:
    - `skills/<name>/SKILL.md` — full skill bodies
    - `commands/<name>.md` — slash commands (also surfaced as `plugin:name`)
    - `agents/<name>.md` — sub-agents (also surfaced as `plugin:name`)

    All three appear in the assistant's available-skills list under the same
    `plugin:name` namespace. For overlap detection we treat them uniformly —
    a `new_skill` proposal that names an existing command or agent is just as
    duplicative as one that names an existing skill.
    """
    entries: list[dict] = []
    if not cache_root.exists():
        return entries
    versions_by_plugin = _read_installed_plugin_versions()
    for plugin_root in sorted(cache_root.iterdir()):
        if not plugin_root.is_dir():
            continue
        plugin_name = plugin_root.name
        versioned = plugin_root / plugin_name
        if not versioned.is_dir():
            continue
        version_dirs = [d for d in versioned.iterdir() if d.is_dir()]
        if not version_dirs:
            continue
        current = max(version_dirs, key=lambda d: d.name)
        installed_version = versions_by_plugin.get(plugin_name) or current.name
        cache_path = str(current)

        # skills/: each is a directory with SKILL.md
        skills_dir = current / "skills"
        if skills_dir.is_dir():
            for skill_dir in sorted(skills_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.is_file():
                    continue
                entries.append({
                    "name": skill_dir.name,
                    "qualified": f"{plugin_name}:{skill_dir.name}",
                    "scope": "plugin",
                    "source": plugin_name,
                    "kind": "skill",
                    "description": _parse_frontmatter_description(skill_md),
                    "path": str(skill_md),
                    "installed_version": installed_version,
                    "cache_path": cache_path,
                })

        # commands/ and agents/: each is a single .md file
        for kind, subdir in (("command", "commands"), ("agent", "agents")):
            kind_dir = current / subdir
            if not kind_dir.is_dir():
                continue
            for md in sorted(kind_dir.glob("*.md")):
                # Skip readme/changelog/etc. only the names that look like commands.
                if md.stem.lower() in {"readme", "changelog", "license"}:
                    continue
                entries.append({
                    "name": md.stem,
                    "qualified": f"{plugin_name}:{md.stem}",
                    "scope": "plugin",
                    "source": plugin_name,
                    "kind": kind,
                    "description": _parse_frontmatter_description(md),
                    "path": str(md),
                    "installed_version": installed_version,
                    "cache_path": cache_path,
                })
    return entries


def _scan_user_skills(user_dir: Path = USER_SKILLS) -> list[dict]:
    """Scan ~/.claude/skills/<name>/SKILL.md."""
    entries: list[dict] = []
    if not user_dir.exists():
        return entries
    for skill_dir in sorted(user_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        entries.append({
            "name": skill_dir.name,
            "qualified": skill_dir.name,
            "scope": "user",
            "source": "user",
            "kind": "skill",
            "description": _parse_frontmatter_description(skill_md),
            "path": str(skill_md),
            "installed_version": None,
            "cache_path": None,
        })
    return entries


def build_catalog(
    plugin_cache: Path = PLUGIN_CACHE,
    user_skills: Path = USER_SKILLS,
) -> list[dict]:
    """Return all known skills across plugin and user scopes."""
    return _scan_plugin_caches(plugin_cache) + _scan_user_skills(user_skills)


# --- Overlap detection ---


_NAME_LIKE = re.compile(r"[a-z][a-z0-9-]{2,}(?::[a-z][a-z0-9-]{2,})?")


def extract_candidate_names(text: str) -> list[str]:
    """Pull skill-name-shaped tokens out of a proposal action string.

    Matches bare names (`doctor`) and plugin-qualified names (`canopy:doctor`).
    Used to guess which skill name a `new_skill` proposal is actually proposing.
    """
    if not text:
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    for match in _NAME_LIKE.findall(text.lower()):
        if match in seen:
            continue
        seen.add(match)
        candidates.append(match)
    return candidates


def find_overlap(
    proposal_action: str,
    catalog: Iterable[dict],
) -> dict | None:
    """If the proposal action mentions an existing skill, return the matching entry.

    Matching rules (conservative, to avoid false positives on common English words
    that happen to be skill names like `health`, `ship`, `learn`):

    - A `plugin:skill` qualified token always matches if it equals an existing
      qualified name.
    - A bare token only matches if it is hyphenated (e.g. `project-status`),
      since most short single-word matches are spurious. Skills with single-word
      names (`doctor`, `ship`) can only be flagged via their qualified form.
    """
    candidates = extract_candidate_names(proposal_action)
    if not candidates:
        return None
    plugin_qualified: dict[str, dict] = {}
    hyphenated_bare: dict[str, dict] = {}
    for entry in catalog:
        qualified = entry["qualified"].lower()
        name = entry["name"].lower()
        if ":" in qualified:
            plugin_qualified.setdefault(qualified, entry)
        if "-" in name:
            hyphenated_bare.setdefault(name, entry)
    for cand in candidates:
        if cand in plugin_qualified:
            return plugin_qualified[cand]
        if "-" in cand and cand in hyphenated_bare:
            return hyphenated_bare[cand]
    return None


def _score_entry(query_tokens: list[str], entry: dict) -> float:
    """Score a catalog entry against query tokens.

    Combines:
    - Token overlap with the skill `name` (weighted heavily — 3x per hit)
    - Substring hits in the qualified name (2x per hit)
    - Token overlap in the description (1x per hit)
    - Bonus for exact substring of the full query string in name/qualified

    Higher is better. Zero means no match at all.
    """
    if not query_tokens:
        return 0.0
    name = entry.get("name", "").lower()
    qualified = entry.get("qualified", "").lower()
    description = (entry.get("description") or "").lower()
    score = 0.0
    for tok in query_tokens:
        if not tok:
            continue
        if tok in name:
            score += 3.0
        if tok in qualified and tok not in name:
            score += 2.0
        if tok in description:
            score += 1.0
    # Whole-query exact-substring bonus
    full = " ".join(query_tokens)
    if full and full in name:
        score += 5.0
    elif full and full in qualified:
        score += 3.0
    elif full and full in description:
        score += 2.0
    return score


def find_skills(
    query: str,
    catalog: Iterable[dict],
    limit: int = 5,
) -> list[dict]:
    """Fuzzy-match a free-text query against catalog entries.

    Matches against `name` and `description`, returning the top `limit` entries
    sorted by score (descending). Entries with zero score are omitted.
    """
    if not query or not query.strip():
        return []
    tokens = [t for t in re.split(r"[\s,;:/]+", query.lower()) if t]
    scored: list[tuple[float, dict]] = []
    for entry in catalog:
        s = _score_entry(tokens, entry)
        if s > 0:
            scored.append((s, entry))
    # Stable sort: highest score first, ties broken by qualified name
    scored.sort(key=lambda pair: (-pair[0], pair[1].get("qualified", "")))
    return [entry for _, entry in scored[:limit]]


def format_for_prompt(catalog: list[dict], max_entries: int = 200) -> str:
    """Render the catalog as a compact list suitable for an LLM prompt."""
    if not catalog:
        return "(no existing skills detected)"
    lines: list[str] = []
    truncated = catalog[:max_entries]
    for entry in truncated:
        desc = entry.get("description", "") or ""
        if len(desc) > 120:
            desc = desc[:117] + "..."
        lines.append(f"- {entry['qualified']} — {desc}" if desc else f"- {entry['qualified']}")
    if len(catalog) > max_entries:
        lines.append(f"... ({len(catalog) - max_entries} more not shown)")
    return "\n".join(lines)
