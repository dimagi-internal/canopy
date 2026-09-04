"""Load, save, and query project-directory-to-GitHub-repo mappings.

Supports both JSON (new, used by hook) and YAML (legacy) formats.
Reads from JSON first, falls back to YAML for migration.
"""
import json
import re
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def load_repo_map(path: Path) -> dict:
    """Load repo mappings. Tries JSON first, falls back to YAML."""
    # Try JSON version first
    json_path = path.with_suffix(".json") if path.suffix != ".json" else path
    if json_path.exists():
        try:
            with open(json_path, encoding="utf-8") as f:
                return json.load(f) or {}
        except (json.JSONDecodeError, OSError):
            pass

    # Fall back to YAML
    yaml_path = path.with_suffix(".yaml") if path.suffix != ".yaml" else path
    if yaml_path.exists() and HAS_YAML:
        try:
            with open(yaml_path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass

    # Try the exact path as-is
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            if HAS_YAML:
                try:
                    with open(path, encoding="utf-8") as f:
                        return yaml.safe_load(f) or {}
                except Exception:
                    pass

    return {}


def save_repo_mapping(path: Path, project_key: str, repo: str) -> None:
    """Save a single project-to-repo mapping (as JSON)."""
    repo_map = load_repo_map(path)
    repo_map[project_key] = repo
    json_path = path.with_suffix(".json") if path.suffix != ".json" else path
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(repo_map, f, indent=2)


def get_repo_for_project(repo_map: dict, project_key: str) -> str | None:
    """Look up the GitHub repo for a project directory key."""
    return repo_map.get(project_key)


def infer_repo_from_project_key(project_key: str, repo_map: dict) -> str | None:
    """Infer the GitHub repo for a project_key via emdash path conventions.

    Used as a fallback when the post_tool_use hook never captured an entry
    for this project_key (common for worktrees deleted before any tool call
    fired, or sessions that pre-date the hook). The emdash layout is:

      ~/emdash/worktrees/<repo-short>/emdash/<branch>   →
        project_key = "-Users-<user>-emdash-worktrees-<repo-short>-emdash-<branch>"

      ~/emdash/repositories/<repo-short>                →
        project_key = "-Users-<user>-emdash-repositories-<repo-short>"

    The repo *short* name comes from the path; the full ``owner/<repo-short>``
    is resolved by cross-referencing existing repo_map values (the hook
    will have captured at least one current worktree of every active repo).
    When multiple owners map to the same short name (rare), we return None
    rather than guessing.

    Returns the inferred ``owner/repo`` or None if no confident match.
    """
    # Worktree pattern: capture between "-emdash-worktrees-" and the next "-emdash-".
    m = re.search(r"-emdash-worktrees-(.+?)-emdash-", project_key)
    if m:
        short = m.group(1)
    else:
        # Repositories pattern: capture after "-emdash-repositories-" to end.
        m = re.search(r"-emdash-repositories-(.+)$", project_key)
        if not m:
            return None
        short = m.group(1)

    if not short:
        return None

    matches = {v for v in repo_map.values() if isinstance(v, str) and v.endswith(f"/{short}")}
    if len(matches) == 1:
        return next(iter(matches))
    return None


def resolve_repo(repo_map: dict, project_key: str) -> str | None:
    """Resolve project_key → GitHub repo, with inference fallback.

    Order:
      1. Direct lookup in ``repo_map`` (the hook-captured truth).
      2. Inference via emdash path conventions, cross-referenced against
         existing repo_map values.

    Returns None when neither succeeds.
    """
    direct = get_repo_for_project(repo_map, project_key)
    if direct:
        return direct
    return infer_repo_from_project_key(project_key, repo_map)


def extract_repo_from_git_url(url: str) -> str | None:
    """Extract owner/repo from a GitHub git URL."""
    if not url:
        return None
    match = re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?$", url)
    return match.group(1) if match else None
