"""Health checks for the canopy plugin install.

Each check is a small, read-only function returning a CheckResult. They
degrade gracefully — an absent file or unreadable JSON reports ``ok=False``
with a human-readable detail rather than raising. ``run_doctor`` composes
them all and reports an overall pass/fail.

Ported from the documented checks in
``plugins/canopy/skills/canopy-doctor/SKILL.md``: hook registration, session
log, repo map, workbench token (presence + permissions), and plugin version.
Network-dependent checks (live workbench API connectivity, auth-preflight)
are intentionally left to the skill launcher so ``canopy doctor`` stays fast,
offline, and CI-gateable.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from orchestrator.paths import CANOPY_DIR


@dataclass
class CheckResult:
    """Result of a single health check."""

    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def _claude_dir(home: Path) -> Path:
    return home / ".claude"


def _registers_post_tool_use(data: dict) -> bool:
    """True if a hooks mapping registers the capture hook on PostToolUse.

    ``settings.json`` and a plugin's ``hooks.json`` share this shape.
    """
    entries = data.get("hooks", {}).get("PostToolUse", [])
    return any(
        "post_tool_use.py" in h.get("command", "")
        for entry in entries
        if isinstance(entry, dict)
        for h in entry.get("hooks", [])
        if isinstance(h, dict)
    )


def _installed_plugin_dir(home: Path) -> Path | None:
    """Install path of the canopy plugin, per installed_plugins.json."""
    f = _claude_dir(home) / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    for key, val in data.get("plugins", {}).items():
        if "canopy" in key:
            entries = val if isinstance(val, list) else [val]
            if entries and isinstance(entries[0], dict):
                path = entries[0].get("installPath")
                if path:
                    return Path(path)
    return None


def check_hook_registered(home: Path | None = None) -> CheckResult:
    """The PostToolUse capture hook must be registered — by EITHER path.

    The hook is plugin-managed now (``plugins/canopy/hooks/hooks.json``), and
    ``/canopy:setup`` deliberately removes the legacy ``~/.claude/settings.json``
    entry. Checking only settings.json therefore failed on every correctly
    configured machine, and its remediation ("run /canopy:setup") could never
    clear it — setup is what removed the thing it was looking for. Accept the
    plugin registration first, then fall back to the legacy one, which stays
    valid because the plugin copy defers to it.
    """
    home = home or Path.home()
    name = "Hook registration"

    plugin_dir = _installed_plugin_dir(home)
    if plugin_dir is not None:
        hooks_json = plugin_dir / "hooks" / "hooks.json"
        try:
            if _registers_post_tool_use(json.loads(hooks_json.read_text())):
                return CheckResult(name, True, f"registered by the canopy plugin ({hooks_json})")
        except (json.JSONDecodeError, OSError):
            pass  # fall through to the legacy registration

    settings = _claude_dir(home) / "settings.json"
    try:
        if _registers_post_tool_use(json.loads(settings.read_text())):
            return CheckResult(name, True, f"registered via legacy {settings}")
    except (json.JSONDecodeError, OSError):
        pass

    return CheckResult(
        name,
        False,
        "PostToolUse hook not registered by the plugin or settings.json — "
        "run /canopy:update (plugin-managed), or /canopy:setup if canopy is not installed",
    )


def check_session_log(canopy_dir: Path | None = None) -> CheckResult:
    """The session log should exist and have at least one entry."""
    canopy_dir = canopy_dir or CANOPY_DIR
    log = canopy_dir / "session-log.jsonl"
    name = "Session log"
    if not log.exists():
        return CheckResult(name, False, "session-log.jsonl not found — hook may not be firing")
    try:
        lines = sum(1 for line in log.read_text().splitlines() if line.strip())
    except OSError as e:
        return CheckResult(name, False, f"could not read session-log.jsonl: {e}")
    if lines == 0:
        return CheckResult(name, False, "session-log.jsonl is empty — hook may not be firing")
    return CheckResult(name, True, f"session-log.jsonl has {lines} entries")


def check_repo_map(canopy_dir: Path | None = None) -> CheckResult:
    """The repo map should exist and parse as JSON."""
    canopy_dir = canopy_dir or CANOPY_DIR
    repo_map = canopy_dir / "repo-map.json"
    name = "Repo map"
    if not repo_map.exists():
        return CheckResult(name, False, "repo-map.json not found — project identification won't work")
    try:
        data = json.loads(repo_map.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return CheckResult(name, False, f"repo-map.json unreadable: {e}")
    if not isinstance(data, dict):
        return CheckResult(name, False, "repo-map.json is not a JSON object")
    return CheckResult(name, True, f"repo-map.json has {len(data)} project mappings")


def _resolve_token_file(home: Path, canopy_dir: Path) -> Path | None:
    """Mirror the skill's resolution: CLAUDE_PLUGIN_DATA first, then canopy dir."""
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data:
        candidate = Path(plugin_data) / "workbench-token"
        if candidate.exists():
            return candidate
    fallback = canopy_dir / "workbench-token"
    if fallback.exists():
        return fallback
    return None


def check_workbench_token(
    home: Path | None = None, canopy_dir: Path | None = None
) -> CheckResult:
    """The workbench token must exist, be non-empty, and be mode 600."""
    home = home or Path.home()
    canopy_dir = canopy_dir or CANOPY_DIR
    name = "Workbench token"
    token_file = _resolve_token_file(home, canopy_dir)
    if token_file is None:
        return CheckResult(
            name,
            False,
            f"workbench-token not found at {canopy_dir / 'workbench-token'} — run /canopy:setup",
        )
    try:
        contents = token_file.read_text().strip()
    except OSError as e:
        return CheckResult(name, False, f"could not read {token_file}: {e}")
    if not contents:
        return CheckResult(name, False, f"workbench-token at {token_file} is empty — run /canopy:setup")

    perms = oct(token_file.stat().st_mode & 0o777)[2:]
    if perms != "600":
        return CheckResult(
            name,
            False,
            f"workbench-token exists ({len(contents)} bytes) but permissions are {perms} "
            f"(should be 600) — chmod 600 {token_file}",
        )
    return CheckResult(name, True, f"workbench-token exists ({len(contents)} bytes, permissions {perms})")


def check_plugin_version(home: Path | None = None) -> CheckResult:
    """The installed_plugins.json should record an installed canopy version."""
    home = home or Path.home()
    f = _claude_dir(home) / "plugins" / "installed_plugins.json"
    name = "Plugin version"
    if not f.exists():
        return CheckResult(name, False, "installed_plugins.json not found")
    try:
        data = json.loads(f.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return CheckResult(name, False, f"installed_plugins.json unreadable: {e}")

    for key, val in data.get("plugins", {}).items():
        if "canopy" in key:
            entries = val if isinstance(val, list) else [val]
            if entries and isinstance(entries[0], dict):
                version = entries[0].get("version", "unknown")
                return CheckResult(name, True, f"canopy {version}")
    return CheckResult(name, False, "no canopy entry in installed_plugins.json")


def _uv_receipt(home: Path) -> Path:
    return home / ".local/share/uv/tools/canopy/uv-receipt.toml"


def _marketplace_clone(home: Path) -> Path:
    return _claude_dir(home) / "plugins" / "marketplaces" / "canopy"


CLI_REMEDY = (
    "uv tool install --reinstall --force ~/.claude/plugins/marketplaces/canopy"
)


def _receipt_source_dir(home: Path) -> tuple[Path | None, str | None]:
    """Return (source directory the canopy CLI was installed from, error)."""
    receipt = _uv_receipt(home)
    if not receipt.exists():
        return None, f"no uv receipt at {receipt} — canopy CLI not installed via `uv tool`"
    try:
        import tomllib

        data = tomllib.loads(receipt.read_text())
    except (OSError, ValueError) as e:
        return None, f"could not read {receipt}: {e}"
    for req in data.get("tool", {}).get("requirements", []):
        if isinstance(req, dict) and req.get("directory"):
            return Path(req["directory"]), None
    return None, f"{receipt} records no directory requirement (installed from a index/VCS?)"


def check_cli_install_source(home: Path | None = None) -> CheckResult:
    """The `canopy` CLI must be installed from the marketplace clone.

    canopy is dual-surface: the plugin ships via the marketplace cache, but the
    CLI is a separate `uv tool install`. Installing it from a dev checkout
    couples the CLI to whatever branch happens to be checked out there — it then
    silently serves stale or in-progress code to every agent. `skills/update`
    has documented "NEVER an editable install of ~/emdash-projects/canopy" since
    the drift stranded `canopy harvest`, but nothing enforced it, and a machine
    was found back on the dev checkout (CLI two versions behind main) on
    2026-07-24. Prose lost; this check is the enforcement.
    """
    home = home or Path.home()
    name = "CLI install source"
    source, err = _receipt_source_dir(home)
    if err is not None:
        return CheckResult(name, False, f"{err} — run: {CLI_REMEDY}")

    expected = _marketplace_clone(home)
    try:
        matches = source.resolve() == expected.resolve()
    except OSError:
        matches = source == expected
    if not matches:
        return CheckResult(
            name,
            False,
            f"canopy CLI installed from {source}, not the marketplace clone "
            f"({expected}). A dev checkout drifts with whatever branch is "
            f"checked out. Fix: {CLI_REMEDY}",
        )
    return CheckResult(name, True, f"installed from {expected}")


def check_cli_version_sync(home: Path | None = None) -> CheckResult:
    """The installed CLI version must match the marketplace clone's VERSION.

    Catches the other half of the same failure: installed from the right place,
    but never re-installed after the clone was pulled, so `canopy <verb>` runs
    older code than the plugin that calls it.
    """
    home = home or Path.home()
    name = "CLI version sync"

    clone_version_file = _marketplace_clone(home) / "VERSION"
    if not clone_version_file.is_file():
        return CheckResult(name, False, f"{clone_version_file} not found — run /canopy:setup")
    try:
        clone_version = clone_version_file.read_text().strip()
    except OSError as e:
        return CheckResult(name, False, f"could not read {clone_version_file}: {e}")

    tool_lib = home / ".local/share/uv/tools/canopy/lib"
    dist_infos = sorted(tool_lib.glob("python*/site-packages/canopy-*.dist-info"))
    if not dist_infos:
        return CheckResult(
            name, False, f"no installed canopy dist-info under {tool_lib} — run: {CLI_REMEDY}"
        )
    # canopy-0.2.342.dist-info -> 0.2.342
    installed = dist_infos[-1].name[len("canopy-"):-len(".dist-info")]

    if installed != clone_version:
        return CheckResult(
            name,
            False,
            f"CLI is {installed} but the marketplace clone is {clone_version} — "
            f"the plugin calls a CLI older than itself. Fix: {CLI_REMEDY}",
        )
    return CheckResult(name, True, f"CLI {installed} matches marketplace clone")


# Order matters for display: registration → state → auth → CLI deploy.
_CHECKS = (
    check_hook_registered,
    check_session_log,
    check_repo_map,
    check_workbench_token,
    check_plugin_version,
    check_cli_install_source,
    check_cli_version_sync,
)


def run_doctor(
    home: Path | None = None, canopy_dir: Path | None = None
) -> tuple[list[CheckResult], bool]:
    """Run every check and return (results, overall_ok).

    ``home`` and ``canopy_dir`` are injectable for testing; production callers
    pass nothing and the real paths are used.
    """
    home = home or Path.home()
    canopy_dir = canopy_dir or CANOPY_DIR

    results = [
        check_hook_registered(home=home),
        check_session_log(canopy_dir=canopy_dir),
        check_repo_map(canopy_dir=canopy_dir),
        check_workbench_token(home=home, canopy_dir=canopy_dir),
        check_plugin_version(home=home),
        check_cli_install_source(home=home),
        check_cli_version_sync(home=home),
    ]
    overall_ok = all(r.ok for r in results)
    return results, overall_ok
