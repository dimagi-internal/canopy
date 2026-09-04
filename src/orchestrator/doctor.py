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
offline, and CI-gateable. The one external-tool check reads Homebrew's cached
data under a timeout and can only ever warn, so that contract holds.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from orchestrator.paths import CANOPY_DIR


@dataclass
class CheckResult:
    """Result of a single health check.

    ``warn`` is a third, non-gating state: the check ran, found something
    worth saying, but must not fail CI. It exists for conditions we do not
    control and cannot fix on demand — chiefly an upstream tool shipping a
    new release. ``overall_ok`` ignores it by design, so a warn never turns
    somebody else's release into our red build.
    """

    name: str
    ok: bool
    detail: str
    warn: bool = False

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
        data = json.loads(f.read_text(encoding="utf-8"))
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
            if _registers_post_tool_use(json.loads(hooks_json.read_text(encoding="utf-8"))):
                return CheckResult(name, True, f"registered by the canopy plugin ({hooks_json})")
        except (json.JSONDecodeError, OSError):
            pass  # fall through to the legacy registration

    settings = _claude_dir(home) / "settings.json"
    try:
        if _registers_post_tool_use(json.loads(settings.read_text(encoding="utf-8"))):
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
        lines = sum(1 for line in log.read_text(encoding="utf-8").splitlines() if line.strip())
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
        # NOT a failure. The PostToolUse hook writes this lazily, the first time a tool runs
        # inside a git repo that has a GitHub remote — so on a correct fresh install the file
        # legitimately does not exist yet, and it appears on its own. Reporting FAIL sent new
        # operators hunting for a broken install and a fix that does not exist.
        return CheckResult(
            name,
            True,
            "repo-map.json not created yet — the PostToolUse hook writes it the first time "
            "you use a tool inside a git repo with a GitHub remote; nothing to fix",
            warn=True,
        )
    try:
        data = json.loads(repo_map.read_text(encoding="utf-8"))
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
    """A canopy-web PAT must be resolvable — from the environment or the file.

    This MUST mirror `canopy_web.resolve_pat()`, which reads CANOPY_WEB_PAT first
    and only then falls back to the token file. Checking for the file alone made
    the doctor stricter than the runtime it reports on: on a headless box (the EC2
    cloud runner) the PAT arrives as an env var, every canopy-web call succeeds,
    and yet the doctor reported the agent BROKEN and told it to run
    `/canopy:setup` — a browser-loopback flow that cannot run there at all.

    CANOPY_TOKEN is accepted too: it is the variable the cloud runner already
    exports into each agent's turn environment.
    """
    home = home or Path.home()
    canopy_dir = canopy_dir or CANOPY_DIR
    name = "Workbench token"

    for var in ("CANOPY_WEB_PAT", "CANOPY_TOKEN"):
        if os.environ.get(var, "").strip():
            return CheckResult(name, True, f"PAT supplied via ${var}")

    token_file = _resolve_token_file(home, canopy_dir)
    if token_file is None:
        return CheckResult(
            name,
            False,
            f"no canopy-web PAT: ${{CANOPY_WEB_PAT}} unset and no workbench-token at "
            f"{canopy_dir / 'workbench-token'} — run /canopy:setup, or set CANOPY_WEB_PAT "
            f"(headless hosts should provision it via .env.tpl rather than the browser flow)",
        )
    try:
        contents = token_file.read_text(encoding="utf-8").strip()
    except OSError as e:
        return CheckResult(name, False, f"could not read {token_file}: {e}")
    if not contents:
        return CheckResult(name, False, f"workbench-token at {token_file} is empty — run /canopy:setup")

    # POSIX mode bits are not a thing on Windows: os.stat there synthesises
    # 0o666 (or 0o444 when read-only) from the archive bit, so `!= "600"` can
    # NEVER pass no matter how tightly the file is actually locked down with
    # icacls. Checking it there reports a false failure and, worse, sends the
    # operator to chmod — which does nothing. Access control on NTFS is an ACL
    # question; this check only has meaning where the mode bits are real.
    # (@smazumdar, first Windows bring-up, 2026-09-01.)
    if sys.platform == "win32":
        return CheckResult(
            name,
            True,
            f"workbench-token exists ({len(contents)} bytes; POSIX permission bits are not "
            f"meaningful on Windows — restrict it with icacls if it is on a shared machine)",
        )

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
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return CheckResult(name, False, f"installed_plugins.json unreadable: {e}")

    for key, val in data.get("plugins", {}).items():
        if "canopy" in key:
            entries = val if isinstance(val, list) else [val]
            if entries and isinstance(entries[0], dict):
                version = entries[0].get("version", "unknown")
                return CheckResult(name, True, f"canopy {version}")
    return CheckResult(name, False, "no canopy entry in installed_plugins.json")


def _uv_tool_dir(home: Path) -> Path:
    """Where `uv tool install` put canopy, on THIS platform.

    uv does not use one layout everywhere, and hardcoding the POSIX one made
    doctor report two false failures on Windows whose remediation LOOPED: the
    suggested fix is to reinstall, which puts the files back at the same
    (correct) place doctor was not looking, so the check fails again forever.
    Reported by @smazumdar on the first Windows bring-up, 2026-09-01.

      UV_TOOL_DIR   explicit override, honoured on every platform
      Windows       %APPDATA%\\uv\\tools      (~/AppData/Roaming/uv/tools)
      otherwise     $XDG_DATA_HOME/uv/tools  (~/.local/share/uv/tools)
    """
    if override := os.environ.get("UV_TOOL_DIR"):
        return Path(override)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "uv" / "tools"
    if xdg := os.environ.get("XDG_DATA_HOME"):
        return Path(xdg) / "uv" / "tools"
    return home / ".local" / "share" / "uv" / "tools"


def _uv_receipt(home: Path) -> Path:
    return _uv_tool_dir(home) / "canopy" / "uv-receipt.toml"


def _canopy_dist_infos(home: Path) -> list[Path]:
    """Installed canopy dist-info dirs, across uv's two site-packages layouts.

    POSIX installs to `lib/python3.x/site-packages`; Windows to `Lib/site-packages`
    with no interpreter-version segment. Globbing only the first found nothing on
    Windows even though the dist-info was present and correct.
    """
    tool_root = _uv_tool_dir(home) / "canopy"
    found = [
        *tool_root.glob("lib/python*/site-packages/canopy-*.dist-info"),
        *tool_root.glob("Lib/site-packages/canopy-*.dist-info"),
    ]
    # de-duplicate: a case-insensitive filesystem answers both globs with the
    # same real directory, which would otherwise show up twice.
    return sorted({p.resolve(): p for p in found}.values())


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

        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
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
        clone_version = clone_version_file.read_text(encoding="utf-8").strip()
    except OSError as e:
        return CheckResult(name, False, f"could not read {clone_version_file}: {e}")

    tool_lib = _uv_tool_dir(home) / "canopy"
    dist_infos = _canopy_dist_infos(home)
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


# Third-party CLIs canopy shells out to, by Homebrew formula name. Drift here
# is invisible until something silently lacks a verb: the fleet ran gogcli
# 0.12.0 against an upstream 0.38.1 for months, and the missing Docs
# batch-update verb read as a permissions problem instead of a stale install.
_EXTERNAL_TOOLS = {
    "gogcli": "gog — Google Workspace access (Gmail/Calendar/Drive/Docs/Sheets)",
    "gh": "gh — GitHub CLI (PRs, CI, issues)",
}


def check_external_tools(runner=None) -> CheckResult:
    """Warn when a third-party CLI canopy depends on has a newer release.

    Warn-only, never fail: a new upstream release is not a broken install, and
    ``canopy doctor`` gates CI. Deliberately reads Homebrew's *cached* API data
    (``HOMEBREW_NO_AUTO_UPDATE``) and times out, to honour this module's
    fast-and-offline contract.

    Degrades loudly rather than quietly. ``brew outdated`` exits non-zero with
    an EMPTY stdout and the reason only on stderr, so parsing stdout alone
    would either raise on the empty string or -- worse -- report "everything
    current" having checked nothing. Any failure to look is reported as such.
    """
    name = "External tool versions"
    runner = runner or _brew_outdated

    try:
        code, out, err = runner()
    except FileNotFoundError:
        return CheckResult(name, True, "brew not installed — skipping version check", warn=True)
    except Exception as e:  # subprocess timeout, OSError, ...
        return CheckResult(name, True, f"could not run brew outdated: {e}", warn=True)

    if not out.strip():
        reason = err.strip().splitlines()[0] if err.strip() else f"exit {code}, no output"
        return CheckResult(name, True, f"could not check tool versions: {reason}", warn=True)

    try:
        formulae = json.loads(out).get("formulae", [])
    except (ValueError, AttributeError) as e:
        return CheckResult(name, True, f"could not parse brew outdated: {e}", warn=True)

    stale = []
    for f in formulae:
        formula = f.get("name", "")
        if formula not in _EXTERNAL_TOOLS:
            continue
        installed = ", ".join(f.get("installed_versions") or ["?"])
        current = f.get("current_version", "?")
        stale.append(f"{formula} {installed} -> {current}")

    if not stale:
        return CheckResult(name, True, f"{len(_EXTERNAL_TOOLS)} tracked tools up to date")

    return CheckResult(
        name,
        True,
        f"update available: {'; '.join(sorted(stale))} — run: brew upgrade {' '.join(sorted(f.split()[0] for f in stale))}",
        warn=True,
    )


def _brew_outdated() -> tuple[int, str, str]:
    """Run ``brew outdated`` against cached data, returning (code, stdout, stderr)."""
    env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1"}
    p = subprocess.run(
        ["brew", "outdated", "--json=v2"],
        capture_output=True, text=True, timeout=20, env=env,
    )
    return p.returncode, p.stdout, p.stderr


# Order matters for display: registration → state → auth → CLI deploy.
_CHECKS = (
    check_hook_registered,
    check_session_log,
    check_repo_map,
    check_workbench_token,
    check_plugin_version,
    check_cli_install_source,
    check_cli_version_sync,
    check_external_tools,
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
        check_external_tools(),
    ]
    overall_ok = all(r.ok for r in results)
    return results, overall_ok
