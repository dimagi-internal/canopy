"""Atomic-ish VERSION coordination across worktrees.

Two parallel canopy worktrees have repeatedly bumped VERSION to the same
number, requiring manual rebase + force-push recovery. The fix:

- `canopy version verify` — confirm VERSION and plugin.json agree (CI-safe)
- `canopy version bump` — pick `max(local, origin/main) + patch+1`, write both
  files atomically. Fetches origin first so a parallel worktree's bump is
  visible before deciding the next number.

Doesn't fully solve concurrent pushes — the second push will still fail and
need re-bump — but it solves the common case where the user forgot to fetch
before bumping.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _parse(version: str) -> tuple[int, int, int]:
    if not VERSION_RE.match(version.strip()):
        raise ValueError(f"Not a semver patch string: {version!r}")
    major, minor, patch = version.strip().split(".")
    return int(major), int(minor), int(patch)


def _format(parts: tuple[int, int, int]) -> str:
    return ".".join(str(p) for p in parts)


def _read_version_file(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


_PLUGIN_VERSION_LINE_RE = re.compile(r'("version"\s*:\s*")[\d.]+(")')


_PYPROJECT_VERSION_RE = re.compile(r'(?m)^(version\s*=\s*")[\d.]+(")')


def _write_pyproject_version(path: Path, new_version: str) -> int:
    """Surgically replace the first `version = "x.y.z"` (the [project] version) in pyproject.toml.
    Returns the replacement count (0 if the file has no version line)."""
    text = path.read_text(encoding="utf-8")
    new_text, n = _PYPROJECT_VERSION_RE.subn(rf"\g<1>{new_version}\g<2>", text, count=1)
    if n:
        path.write_text(new_text, encoding="utf-8")
    return n


def _read_plugin_json_version(path: Path) -> str:
    """Extract the version field via regex — avoids round-tripping JSON which
    would normalize formatting (unicode escapes, array layout, etc.)."""
    text = path.read_text(encoding="utf-8")
    match = _PLUGIN_VERSION_LINE_RE.search(text)
    if not match:
        raise ValueError(f"Could not find a version line in {path}")
    # Re-extract just the version string between the quotes
    inner = re.search(r'"version"\s*:\s*"([\d.]+)"', text)
    return inner.group(1) if inner else ""


def _write_plugin_json_version(path: Path, new_version: str) -> None:
    """Surgical replace of the version line — preserves all other formatting."""
    text = path.read_text(encoding="utf-8")
    new_text, n = _PLUGIN_VERSION_LINE_RE.subn(rf'\g<1>{new_version}\g<2>', text, count=1)
    if n != 1:
        raise ValueError(f"Could not find a version line to replace in {path}")
    path.write_text(new_text, encoding="utf-8")


def find_version_files(repo_root: Path) -> tuple[Path, Path]:
    """Locate the VERSION file and plugins/canopy/.claude-plugin/plugin.json."""
    version_path = repo_root / "VERSION"
    plugin_json_path = repo_root / "plugins" / "canopy" / ".claude-plugin" / "plugin.json"
    if not version_path.is_file():
        raise FileNotFoundError(f"VERSION not found at {version_path}")
    if not plugin_json_path.is_file():
        raise FileNotFoundError(f"plugin.json not found at {plugin_json_path}")
    return version_path, plugin_json_path


def find_marketplace_json(repo_root: Path) -> Path | None:
    """Locate `.claude-plugin/marketplace.json` if it exists; else None.

    The marketplace file is optional (some downstream tooling and tests build
    a repo skeleton without it). When present it carries two version fields
    that should track plugin.json: `metadata.version` and `plugins[0].version`.
    """
    mp_path = repo_root / ".claude-plugin" / "marketplace.json"
    return mp_path if mp_path.is_file() else None


def _read_marketplace_json_versions(path: Path) -> list[str]:
    """Return all version strings found in marketplace.json (preserves order).

    Used for verify-style checks where we want to confirm every version field
    agrees with the plugin's version. Returns an empty list if no version
    fields are found.
    """
    text = path.read_text(encoding="utf-8")
    return re.findall(r'"version"\s*:\s*"([\d.]+)"', text)


def _write_marketplace_json_version(path: Path, new_version: str) -> int:
    """Surgically replace every `"version": "x.y.z"` in marketplace.json.

    Mirrors the pattern in `_write_plugin_json_version` (regex-based replace
    to preserve formatting). Returns the number of substitutions made.
    """
    text = path.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r'("version"\s*:\s*")[\d.]+(")',
        rf'\g<1>{new_version}\g<2>',
        text,
    )
    if n > 0:
        path.write_text(new_text, encoding="utf-8")
    return n


def verify(repo_root: Path) -> tuple[bool, str, str]:
    """Return (matches, version_text, plugin_json_text). Use for CI checks."""
    v_path, p_path = find_version_files(repo_root)
    v = _read_version_file(v_path)
    p = _read_plugin_json_version(p_path)
    return (v == p, v, p)


def fetch_origin_main_version(repo_root: Path) -> str | None:
    """Fetch origin and read VERSION from origin/main. Returns None on failure."""
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=repo_root, capture_output=True, text=True, timeout=15, check=False,
        )
        result = subprocess.run(
            ["git", "show", "origin/main:VERSION"],
            cwd=repo_root, capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            return None
        text = result.stdout.strip()
        if VERSION_RE.match(text):
            return text
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def compute_next_version(local: str, origin: str | None) -> str:
    """Pick the next version: max(local, origin) with patch+1.

    Handles the case where another worktree already bumped on origin/main
    while we were working — we'll bump from origin's number, not ours.
    """
    candidates = [_parse(local)]
    if origin is not None:
        candidates.append(_parse(origin))
    base = max(candidates)
    bumped = (base[0], base[1], base[2] + 1)
    return _format(bumped)


def _git(repo_root: Path, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root, capture_output=True, text=True, timeout=timeout, check=False,
    )


def _changed_files_against_base(repo_root: Path, base_ref: str) -> list[str] | None:
    """Return paths changed on this branch relative to `base_ref`.

    Uses three-dot semantics so the diff is the work *added on this branch*
    since the merge base, ignoring changes that landed on main in parallel.

    Returns None if the base ref isn't reachable (e.g. shallow clone with no
    fetch of main yet) — callers should treat that as "can't decide" rather
    than implicit pass.
    """
    rev = _git(repo_root, "rev-parse", "--verify", base_ref)
    if rev.returncode != 0:
        return None
    diff = _git(repo_root, "diff", "--name-only", f"{base_ref}...HEAD")
    if diff.returncode != 0:
        return None
    return [line for line in diff.stdout.splitlines() if line.strip()]


def _read_version_at_rev(repo_root: Path, rev: str, path: str) -> str | None:
    result = _git(repo_root, "show", f"{rev}:{path}")
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    if path.endswith(".json"):
        m = re.search(r'"version"\s*:\s*"([\d.]+)"', text)
        return m.group(1) if m else None
    return text if VERSION_RE.match(text) else None


def verify_bump_when_plugin_changed(
    repo_root: Path,
    base_ref: str = "origin/main",
) -> dict:
    """Fail if any `plugins/canopy/` file changed without a VERSION bump on this branch.

    The check that catches the discipline failure CLAUDE.md flags as the #1
    mistake (forgetting to bump VERSION when plugin assets change). The
    existing `version verify` only confirms VERSION and plugin.json agree
    with each other; it can't see whether the branch advanced beyond main.

    Returns a result dict with:
        {
            "ok": bool,                # True => check passed (or N/A)
            "reason": str,             # human-readable explanation
            "plugin_files_changed": [...],   # list of plugin paths in the diff
            "local_version": "x.y.z" | None,
            "main_version": "x.y.z" | None,
            "main_plugin_json_version": "x.y.z" | None,
            "base_ref": base_ref,
            "skipped": bool,           # True if the check could not run
        }
    """
    info: dict = {
        "ok": False,
        "reason": "",
        "plugin_files_changed": [],
        "local_version": None,
        "main_version": None,
        "main_plugin_json_version": None,
        "base_ref": base_ref,
        "skipped": False,
    }

    # Best-effort fetch — if it fails (offline / no remote in CI), continue
    # with whatever ref state we have.
    _git(repo_root, "fetch", "origin", "main", timeout=15)

    changed = _changed_files_against_base(repo_root, base_ref)
    if changed is None:
        info["skipped"] = True
        info["ok"] = True
        info["reason"] = (
            f"Base ref `{base_ref}` not reachable — skipping plugin-bump check. "
            "(In CI, ensure the workflow fetches origin/main before running.)"
        )
        return info

    plugin_changed = [p for p in changed if p.startswith("plugins/canopy/")]
    info["plugin_files_changed"] = plugin_changed
    if not plugin_changed:
        info["ok"] = True
        info["reason"] = "No plugins/canopy/ files changed on this branch — bump not required."
        return info

    # Plugin assets changed — VERSION + plugin.json MUST advance past main's.
    v_path, p_path = find_version_files(repo_root)
    local_v = _read_version_file(v_path)
    local_pj = _read_plugin_json_version(p_path)
    info["local_version"] = local_v

    main_v = _read_version_at_rev(repo_root, base_ref, "VERSION")
    main_pj = _read_version_at_rev(
        repo_root, base_ref, "plugins/canopy/.claude-plugin/plugin.json"
    )
    info["main_version"] = main_v
    info["main_plugin_json_version"] = main_pj

    if local_v != local_pj:
        info["reason"] = (
            f"VERSION ({local_v}) and plugin.json ({local_pj}) disagree on this branch — "
            f"run `canopy version bump` to resolve."
        )
        return info

    if main_v is None:
        # No VERSION file on main yet — first-ever introduction. Anything
        # parses as a bump; just confirm local and plugin.json agree.
        info["ok"] = True
        info["reason"] = "No VERSION on base ref; treating local version as the first bump."
        return info

    try:
        if _parse(local_v) > _parse(main_v):
            info["ok"] = True
            info["reason"] = (
                f"VERSION advanced {main_v} → {local_v} on this branch "
                f"({len(plugin_changed)} plugin file(s) changed)."
            )
            return info
    except ValueError:
        info["reason"] = f"Could not parse VERSION ({local_v} vs {main_v})."
        return info

    info["reason"] = (
        f"{len(plugin_changed)} plugins/canopy/ file(s) changed but VERSION "
        f"({local_v}) did not advance beyond `{base_ref}` ({main_v}). "
        "Run `canopy version bump`, commit the bump, and push again. "
        "Without a bump, `/canopy:update` reports UP_TO_DATE and existing "
        "sessions never pick up your changes."
    )
    return info


def bump(repo_root: Path) -> dict:
    """Compute and write the next version. Returns a summary dict.

    Updates VERSION, plugins/canopy/.claude-plugin/plugin.json, and (if
    present) .claude-plugin/marketplace.json. The marketplace file is
    optional — pre-existing canopy clones may not have one, and the test
    skeleton in tests/test_version_bump.py doesn't create it.
    """
    v_path, p_path = find_version_files(repo_root)
    matches, local_v, plugin_v = verify(repo_root)
    if not matches:
        raise ValueError(
            f"VERSION ({local_v}) and plugin.json ({plugin_v}) disagree — "
            f"fix the mismatch before bumping."
        )
    origin_v = fetch_origin_main_version(repo_root)
    next_v = compute_next_version(local_v, origin_v)
    v_path.write_text(next_v + "\n", encoding="utf-8")
    _write_plugin_json_version(p_path, next_v)

    mp_path = find_marketplace_json(repo_root)
    mp_replacements = 0
    if mp_path is not None:
        mp_replacements = _write_marketplace_json_version(mp_path, next_v)

    # The CLI (Python package) ships with the plugin — keep pyproject's version in lockstep so
    # every release is a distinct build. Pinning it (it sat at 0.1.0) made `uv tool install` serve
    # stale cached builds and made `canopy --version` meaningless.
    pyproject_path = repo_root / "pyproject.toml"
    pyproject_bumped = False
    if pyproject_path.is_file():
        pyproject_bumped = _write_pyproject_version(pyproject_path, next_v) > 0

    return {
        "previous_local": local_v,
        "origin_main": origin_v,
        "new_version": next_v,
        "version_path": str(v_path),
        "plugin_json_path": str(p_path),
        "marketplace_json_path": str(mp_path) if mp_path else None,
        "marketplace_json_replacements": mp_replacements,
        "pyproject_bumped": pyproject_bumped,
    }
