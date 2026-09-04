#!/usr/bin/env python3
"""Fleet session-start auto-updater — canopy's whole-fleet self-heal hook.

canopy is installed user-scope alongside every fleet agent (eva, hal, ada,
echo, ace, …), so a canopy SessionStart hook fires on EVERY Claude Code
session across all projects. This script uses that reach to keep the whole
fleet's *installed plugin cache* in lock-step with each repo's `main`.

Why this exists (GitHub #357): fleet agents run from a versioned plugin cache
(`~/.claude/plugins/cache/<p>/<p>/<version>/`), NOT their repo. Merging a PR to
an agent's `main` changes the repo but does nothing to the running harness
(interactive sessions AND cron turns) until the plugin is re-installed. There
was no auto-update, so installs froze for days.

Design — SHA-driven, and it does the update itself:
  * Detection keys on the git commit SHA (installed `gitCommitSha` vs
    `origin/main`), NOT the manifest version. A SHA advances on EVERY merge, so
    this is immune to the "skill changed but version wasn't bumped" failure mode
    that makes a version-keyed `claude plugin update` a silent no-op.
  * It PERFORMS the update the way the `canopy:update` / `chrome-sales:update`
    skills do by hand — git-pull the marketplace clone, rsync the plugin source
    into a version-keyed cache dir, `npm install` if needed, then patch
    `installed_plugins.json`. Because the sync + registry-patch is done here
    (not delegated to the version-keyed CLI), self-heal does NOT depend on the
    version having bumped. The bump-on-merge CI (issue piece B) rides on top
    only to keep the human-facing version label + `claude plugin list` honest.

Blast radius is high (runs on every session everywhere), so this is built to be
inert and invisible on the happy path:
  * Registered `async: true` — the harness backgrounds it; it never blocks or
    slows session startup.
  * A non-blocking lock means concurrent sessions don't stack into a git storm.
  * Every subprocess is timeout-bounded; every error is swallowed and logged.
  * It emits NOTHING on stdout (no context injection / no per-session noise);
    one line per run goes to ~/.claude/canopy/fleet-update.log.

Opt out per machine (any one):
  * env  CANOPY_FLEET_AUTOUPDATE=0   (also: false / no / off)
  * touch ~/.claude/canopy/fleet-autoupdate-disabled

Tuning envs:
  * CANOPY_FLEET_UPDATE_PLUGINS   comma list — updates EXACTLY these plugin
                                  names instead of auto-discovering the fleet.
  * CANOPY_FLEET_UPDATE_EXCLUDE   comma list — drop these names from the fleet.
  * CANOPY_FLEET_UPDATE_MIN_INTERVAL  seconds — skip if a run finished more
                                  recently than this (default 0 = every session).

stdlib only — this runs under whatever `python3` the harness has (no PyYAML,
no `orchestrator` package on sys.path from a plugin-cache hook).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── paths & config ──────────────────────────────────────────────────────────

HOME = Path(os.path.expanduser("~"))
PLUGINS_DIR = HOME / ".claude" / "plugins"
REGISTRY = PLUGINS_DIR / "installed_plugins.json"
MARKETPLACES = PLUGINS_DIR / "known_marketplaces.json"
CANOPY_DIR = HOME / ".claude" / "canopy"
LOCK_FILE = CANOPY_DIR / "fleet-update.lock"
LOG_FILE = CANOPY_DIR / "fleet-update.log"
STAMP_FILE = CANOPY_DIR / "fleet-update.stamp"
DISABLE_FILE = CANOPY_DIR / "fleet-autoupdate-disabled"

FETCH_TIMEOUT = 60
PULL_TIMEOUT = 60
RSYNC_TIMEOUT = 120
NPM_TIMEOUT = 180


def _falsey(val: str) -> bool:
    return val.strip().lower() in {"0", "false", "no", "off", ""}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def run(cmd: list[str], timeout: int, cwd: str | None = None):
    """Run a command; return (rc, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except (OSError, ValueError) as exc:
        return 1, "", str(exc)


def load_json(path: Path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ── fleet discovery ─────────────────────────────────────────────────────────


def is_fleet_plugin(name: str, install_path: str) -> bool:
    """canopy itself, plus any factory-stamped agent (ships config/agent.json)."""
    if name == "canopy":
        return True
    try:
        return (Path(install_path) / "config" / "agent.json").is_file()
    except OSError:
        return False


def discover_fleet(registry: dict, marketplaces: dict) -> list[dict]:
    """Resolve the fleet into a list of update targets.

    Honors CANOPY_FLEET_UPDATE_PLUGINS (explicit allowlist) and
    CANOPY_FLEET_UPDATE_EXCLUDE (denylist) over the auto-detected set.
    """
    only = set(_env_list("CANOPY_FLEET_UPDATE_PLUGINS"))
    exclude = set(_env_list("CANOPY_FLEET_UPDATE_EXCLUDE"))

    plugins = (registry or {}).get("plugins", {})
    targets: list[dict] = []
    for key, entries in plugins.items():
        if not entries:
            continue
        entry = entries[0]
        # key is "<name>@<marketplace>"
        name, _, mkt = key.rpartition("@")
        if not name:
            name, mkt = mkt, key  # no '@' — degenerate; keep name
        install_path = entry.get("installPath", "")

        wanted = name in only if only else is_fleet_plugin(name, install_path)
        if not wanted or name in exclude:
            continue

        mkt_entry = (marketplaces or {}).get(mkt, {})
        clone = mkt_entry.get("installLocation") or str(
            PLUGINS_DIR / "marketplaces" / mkt
        )
        targets.append(
            {
                "key": key,
                "name": name,
                "marketplace": mkt,
                "install_path": install_path,
                "version": entry.get("version", "unknown"),
                "sha": entry.get("gitCommitSha", ""),
                "clone": clone,
            }
        )
    targets.sort(key=lambda t: t["name"])
    return targets


# ── update one plugin ───────────────────────────────────────────────────────


def _default_branch(clone: str) -> str:
    rc, out, _ = run(
        ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"], 10, cwd=clone
    )
    if rc == 0 and out.startswith("origin/"):
        return out.split("/", 1)[1]
    return "main"


def _current_branch(clone: str) -> str:
    rc, out, _ = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], 10, cwd=clone)
    return out if rc == 0 else ""


def _dirty(clone: str) -> bool:
    """Uncommitted changes to TRACKED files. Untracked junk (stray node_modules,
    a scratch file) is ignored — it is not work anyone can lose."""
    rc, out, _ = run(
        ["git", "status", "--porcelain", "--untracked-files=no"], 15, cwd=clone
    )
    return rc == 0 and bool(out.strip())


def ensure_on_branch(clone: str, branch: str) -> str:
    """Put the clone on `branch` before anything resets it. "" ok, else an error.

    `git reset --hard origin/<branch>` rewrites whatever ref HEAD points at, not
    the ref named in the argument. So when the clone sits on a feature branch —
    which happens, despite the clone being documented as never-a-workspace; the
    canopy one was found on `ddd/preflight-applies-scroll` on 2026-07-28 — the
    reset silently moved THAT branch to main's tip. Those commits survived only
    because they had been pushed.

    Two cases, deliberately different:
      * parked and clean  → switch to `branch`, leaving the parked ref where it is.
      * parked and dirty  → refuse. The clone has been repurposed as a workspace,
        and switching would strand the work while the reset destroys it. A logged
        error a human can act on beats a silent deletion.

    A dirty clone already ON `branch` is left alone to reset as before: that is
    the mirror contract this hook has always had, and blocking there would freeze
    fleet updates over a stray edit to a file that is meant to be overwritten.
    """
    current = _current_branch(clone)
    if current == branch:
        return ""
    where = current or "detached HEAD"
    if _dirty(clone):
        return (f"clone parked on '{where}' with uncommitted changes — refusing to "
                f"switch to {branch} (that would strand the branch and destroy the edit)")
    rc, _, err = run(["git", "checkout", branch], PULL_TIMEOUT, cwd=clone)
    if rc != 0:
        first = (err.splitlines() or [""])[0]
        return f"clone parked on '{where}', cannot switch to {branch}: {first or rc}"
    return ""


def _plugin_source_subdir(clone: str, name: str) -> str:
    """Read the plugin's `source` from the clone's marketplace.json (default './')."""
    mp = load_json(Path(clone) / ".claude-plugin" / "marketplace.json")
    if isinstance(mp, dict):
        for plug in mp.get("plugins", []):
            if plug.get("name") == name:
                return plug.get("source", "./") or "./"
    return "./"


def _read_version(plugin_dir: Path) -> str:
    data = load_json(plugin_dir / ".claude-plugin" / "plugin.json")
    if isinstance(data, dict) and data.get("version"):
        return str(data["version"])
    return ""


# ── runtime bundle (repo-root code shipped alongside the plugin) ────────────
#
# Some plugins (canopy) keep their executable runtime at the REPO root, outside
# the plugin source subdir, so a plain plugin sync would strand it. Such a
# plugin declares `.claude-plugin/runtime.json`:
#   {"dest": "runtime", "paths": ["src", "scripts", "evals", "pyproject.toml"]}
# and we rsync each listed clone-root path into <cache>/<dest>/ after the
# plugin sync. Skills resolve the bundle via scripts/canopy-runtime.sh, so the
# runtime a skill executes is version-locked to the installed plugin.


def _runtime_manifest(plugin_dir: Path) -> dict | None:
    data = load_json(plugin_dir / ".claude-plugin" / "runtime.json")
    if not isinstance(data, dict) or not isinstance(data.get("paths"), list):
        return None
    return data


def _runtime_present(cache_dir: Path, manifest: dict) -> bool:
    dest = cache_dir / str(manifest.get("dest") or "runtime")
    return all((dest / str(p).rstrip("/").split("/")[-1]).exists() for p in manifest["paths"])


def sync_runtime(clone: str, manifest: dict, cache_dir: Path) -> str:
    """Rsync manifest paths from the clone root into <cache>/<dest>/.

    Returns "" on success, else an error string. The clone has already been
    reset to origin/<branch> by the caller, so what we copy is exactly the
    installed SHA. `.venv`/`uv.lock` in dest are preserved (uv materializes
    them on first use; a same-version resync must not thrash them).
    """
    dest = cache_dir / str(manifest.get("dest") or "runtime")
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"mkdir runtime: {exc}"
    for rel in manifest["paths"]:
        src = Path(clone) / str(rel)
        if not src.exists():
            return f"runtime path missing in clone: {rel}"
        rc, _, err = run(
            ["rsync", "-a", "--exclude=.venv", "--exclude=__pycache__",
             "--exclude=node_modules", "--exclude=.git",
             str(src), f"{dest}/"],
            RSYNC_TIMEOUT,
        )
        if rc != 0:
            return f"rsync runtime {rel}: {err or rc}"
    return ""


def update_one(target: dict) -> dict:
    """Fetch → (if behind) pull + rsync into cache + npm + patch registry."""
    name = target["name"]
    result = {
        "name": name,
        "status": "unknown",
        "old_ver": target["version"],
        "new_ver": target["version"],
        "old_sha": (target["sha"] or "")[:8],
        "new_sha": (target["sha"] or "")[:8],
        "error": "",
    }
    clone = target["clone"]

    if not (Path(clone) / ".git").exists():
        result.update(status="error", error="no marketplace clone")
        return result
    if not shutil.which("git"):
        result.update(status="error", error="git not found")
        return result

    branch = _default_branch(clone)
    rc, _, err = run(["git", "fetch", "--quiet", "origin", branch], FETCH_TIMEOUT, cwd=clone)
    if rc != 0:
        result.update(status="error", error=f"fetch: {err or rc}")
        return result

    rc, remote_sha, err = run(["git", "rev-parse", f"origin/{branch}"], 10, cwd=clone)
    if rc != 0 or not remote_sha:
        result.update(status="error", error=f"rev-parse: {err or rc}")
        return result

    if remote_sha == target["sha"]:
        # Repair pass: a native Claude Code re-install syncs only the plugin
        # subdir, so a declared runtime bundle can be missing even at the right
        # SHA. Top it up in place.
        source = _plugin_source_subdir(clone, name)
        plugin_dir = (Path(clone) / source).resolve()
        manifest = _runtime_manifest(plugin_dir)
        install_path = Path(target["install_path"]) if target["install_path"] else None
        if manifest and install_path and install_path.is_dir() and not _runtime_present(install_path, manifest):
            err = sync_runtime(clone, manifest, install_path)
            if err:
                result.update(status="error", error=f"runtime repair: {err}")
            else:
                result.update(status="runtime-repaired")
            return result
        result.update(status="up-to-date")
        return result

    # Behind — hard-reset the (mirror) clone to origin, then sync into cache.
    # On `branch` FIRST: reset --hard rewrites the checked-out ref, whatever it is.
    err = ensure_on_branch(clone, branch)
    if err:
        result.update(status="error", error=err)
        return result
    rc, _, err = run(["git", "reset", "--hard", f"origin/{branch}"], PULL_TIMEOUT, cwd=clone)
    if rc != 0:
        result.update(status="error", error=f"reset: {err or rc}")
        return result

    source = _plugin_source_subdir(clone, name)
    plugin_dir = (Path(clone) / source).resolve()
    version = _read_version(plugin_dir)
    if not version:
        result.update(status="error", error="unreadable plugin.json version")
        return result

    cache_dir = PLUGINS_DIR / "cache" / name / name / version
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.update(status="error", error=f"mkdir cache: {exc}")
        return result

    if not shutil.which("rsync"):
        result.update(status="error", error="rsync not found")
        return result
    rc, _, err = run(
        [
            "rsync",
            "-a",
            "--delete",
            "--exclude=.git",
            f"{plugin_dir}/",
            f"{cache_dir}/",
        ],
        RSYNC_TIMEOUT,
    )
    if rc != 0:
        result.update(status="error", error=f"rsync: {err or rc}")
        return result

    # Best-effort JS deps (canopy-gws MCP etc.). Never fatal.
    if (cache_dir / "package.json").exists() and shutil.which("npm"):
        run(
            ["npm", "install", "--no-audit", "--no-fund"],
            NPM_TIMEOUT,
            cwd=str(cache_dir),
        )

    # Runtime bundle (repo-root code declared in .claude-plugin/runtime.json).
    # Fatal when declared: a cache without its runtime strands every skill that
    # shells into it, which is exactly the drift class this hook exists to kill.
    manifest = _runtime_manifest(plugin_dir)
    if manifest:
        err = sync_runtime(clone, manifest, cache_dir)
        if err:
            result.update(status="error", error=err)
            return result

    if not patch_registry(name, target["marketplace"], version, str(cache_dir), remote_sha):
        result.update(status="error", error="registry patch failed")
        return result

    result.update(
        status="updated",
        new_ver=version,
        new_sha=remote_sha[:8],
    )
    return result


def patch_registry(name: str, mkt: str, version: str, cache_dir: str, sha: str) -> bool:
    """Re-read installed_plugins.json, update just our entry, write atomically."""
    data = load_json(REGISTRY)
    if not isinstance(data, dict):
        return False
    entries = data.get("plugins", {}).get(f"{name}@{mkt}")
    if not entries:
        return False
    entries[0]["version"] = version
    entries[0]["installPath"] = cache_dir
    entries[0]["gitCommitSha"] = sha
    entries[0]["lastUpdated"] = _now_iso()
    try:
        tmp = REGISTRY.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, REGISTRY)
        return True
    except OSError:
        return False


# ── logging ─────────────────────────────────────────────────────────────────


def _fmt(result: dict) -> str:
    name, status = result["name"], result["status"]
    if status == "updated":
        return f"{name} {result['old_ver']}→{result['new_ver']} (sha {result['old_sha']}→{result['new_sha']})"
    if status == "up-to-date":
        return f"{name} up-to-date"
    if status == "runtime-repaired":
        return f"{name} runtime bundle repaired into installed cache"
    return f"{name} error: {result['error']}"


def log_run(results: list[dict]) -> None:
    try:
        CANOPY_DIR.mkdir(parents=True, exist_ok=True)
        line = f"{_now_iso()} fleet-update: " + "; ".join(_fmt(r) for r in results)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ── orchestration ───────────────────────────────────────────────────────────


def _disabled() -> bool:
    if _falsey(os.environ.get("CANOPY_FLEET_AUTOUPDATE", "1")):
        return True
    return DISABLE_FILE.exists()


def _throttled() -> bool:
    try:
        interval = int(os.environ.get("CANOPY_FLEET_UPDATE_MIN_INTERVAL", "0"))
    except ValueError:
        interval = 0
    if interval <= 0 or not STAMP_FILE.exists():
        return False
    return (time.time() - STAMP_FILE.stat().st_mtime) < interval


def main() -> int:
    if _disabled():
        return 0
    if _throttled():
        return 0

    try:
        CANOPY_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0

    # Non-blocking lock: if another session is already updating, bow out.
    try:
        import fcntl

        lock_fh = open(LOCK_FILE, "w", encoding="utf-8")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # held elsewhere
    except ImportError:
        lock_fh = None  # non-POSIX; proceed without a lock

    try:
        registry = load_json(REGISTRY)
        marketplaces = load_json(MARKETPLACES)
        if not isinstance(registry, dict):
            return 0
        targets = discover_fleet(registry, marketplaces or {})
        if not targets:
            return 0

        results = []
        for target in targets:
            try:
                results.append(update_one(target))
            except Exception as exc:  # noqa: BLE001 — never let one plugin abort the run
                results.append(
                    {
                        "name": target["name"],
                        "status": "error",
                        "old_ver": target["version"],
                        "new_ver": target["version"],
                        "old_sha": (target["sha"] or "")[:8],
                        "new_sha": (target["sha"] or "")[:8],
                        "error": f"unexpected: {exc}",
                    }
                )

        if any(r["status"] != "up-to-date" for r in results):
            log_run(results)
        try:
            STAMP_FILE.write_text(_now_iso(), encoding="utf-8")
        except OSError:
            pass
    finally:
        if lock_fh is not None:
            lock_fh.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — a session-start hook must NEVER fail loudly
        sys.exit(0)
