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
    skills do by hand — git-pull the marketplace clone, mirror the plugin source
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

It also keeps the `canopy` CLI (the engine most hooks shell out to — `canopy local`,
`canopy agent`, credential reads) at the plugin's version: canopy ships as a plugin AND
a `uv tool` CLI, and a plugin update alone left a machine running new hooks against an
old engine. When `canopy --version` is BEHIND the canopy marketplace clone it reinstalls
from that clone (see refresh_cli). The canopy runner app does the same on a timer
(canopy-web `cli_refresh`), but a person who only uses an agent in their own Claude Code
has no runner app — this hook is the one thing that runs there. Opt out of just this
step with CANOPY_CLI_REFRESH=0 (the runner's switch too).

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
import re
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


# ── release channel ─────────────────────────────────────────────────────────
#
# A merge used to reach every machine on its next session. A repo can now publish a
# `fleet-stable` tag (canopy's `.github/workflows/fleet-stable.yml` advances it to the newest
# main commit whose required checks passed and that has soaked on the `main` channel). The
# fleet follows the tag; a canary machine follows `main` with CANOPY_FLEET_CHANNEL=main or a
# `main` line in ~/.claude/canopy/fleet-channel. A repo without the tag is followed at main.

STABLE_TAG = "fleet-stable"
CHANNEL_FILE = CANOPY_DIR / "fleet-channel"


def fleet_channel() -> str:
    val = os.environ.get("CANOPY_FLEET_CHANNEL", "").strip().lower()
    if not val:
        try:
            val = CHANNEL_FILE.read_text(encoding="utf-8").strip().lower()
        except OSError:
            val = ""
    return "main" if val == "main" else "stable"


def _is_ancestor(clone: str, older: str, newer: str) -> bool:
    if not older or not newer:
        return False
    rc, _, _ = run(["git", "merge-base", "--is-ancestor", older, newer], 15, cwd=clone)
    return rc == 0


def target_ref(clone: str, branch: str) -> str:
    """The ref this machine should run: the repo's fleet-stable tag, else origin/<branch>.

    The tag is honoured only while it points into main's history, so a stray tag can never
    send the fleet somewhere main has not been."""
    main_ref = f"origin/{branch}"
    if fleet_channel() == "main":
        return main_ref
    rc, _, _ = run(["git", "fetch", "--quiet", "--force", "origin",
                    f"+refs/tags/{STABLE_TAG}:refs/tags/{STABLE_TAG}"], FETCH_TIMEOUT, cwd=clone)
    if rc != 0:
        return main_ref                      # no tag published: follow main, as before
    rc, tag_sha, _ = run(["git", "rev-parse", f"{STABLE_TAG}^{{commit}}"], 10, cwd=clone)
    if rc != 0 or not _is_ancestor(clone, tag_sha, main_ref):
        return main_ref
    return f"refs/tags/{STABLE_TAG}"


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
# and we mirror each listed clone-root path into <cache>/<dest>/ after the
# plugin sync. Skills resolve the bundle via scripts/canopy-runtime.sh, so the
# runtime a skill executes is version-locked to the installed plugin.


def mirror_tree(src: Path, dest: Path, *, exclude: set[str], delete: bool = False) -> str:
    """Copy `src` into `dest` — the rsync this hook used to shell out to, in pure Python.

    WHY NOT rsync: it does not exist on Windows, and this hook runs on EVERY Claude Code
    session, so on a Windows machine the fleet self-updater failed every single time —
    `fleet-update: canopy error: rsync not found`, eight consecutive failures and no
    successes, written to a log nobody opens. The agent stayed frozen on whatever plugin
    version it was installed with, and the only visible symptom was the *other* half of the
    same cause: `canopy doctor` reporting the CLI and the marketplace clone drifting apart.

    Semantics kept from the two call sites: `exclude` matches a path COMPONENT (as
    `--exclude=node_modules` did), and `delete=True` reproduces `--delete` by removing
    entries in dest that are no longer in src — except the ones we deliberately preserve
    (`.venv`/`uv.lock`, which uv materializes and a same-version resync must not thrash).

    Returns "" on success, else an error string — matching the callers' contract.
    """
    PRESERVE = {".venv", "uv.lock"}

    def _skip(name: str) -> bool:
        return name in exclude

    try:
        if delete and dest.is_dir():
            keep = {p.name for p in src.iterdir()} | PRESERVE | exclude
            for existing in dest.iterdir():
                if existing.name in keep:
                    continue
                if existing.is_dir() and not existing.is_symlink():
                    shutil.rmtree(existing, ignore_errors=True)
                else:
                    existing.unlink(missing_ok=True)

        for item in src.iterdir():
            if _skip(item.name):
                continue
            target = dest / item.name
            if item.is_dir() and not item.is_symlink():
                shutil.copytree(
                    item, target,
                    ignore=shutil.ignore_patterns(*exclude),
                    dirs_exist_ok=True,
                )
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
    except (OSError, shutil.Error) as exc:
        return f"copy {src} -> {dest}: {exc}"
    return ""


def _runtime_manifest(plugin_dir: Path) -> dict | None:
    data = load_json(plugin_dir / ".claude-plugin" / "runtime.json")
    if not isinstance(data, dict) or not isinstance(data.get("paths"), list):
        return None
    return data


def _runtime_present(cache_dir: Path, manifest: dict) -> bool:
    dest = cache_dir / str(manifest.get("dest") or "runtime")
    return all((dest / str(p).rstrip("/").split("/")[-1]).exists() for p in manifest["paths"])


def sync_runtime(clone: str, manifest: dict, cache_dir: Path) -> str:
    """Mirror manifest paths from the clone root into <cache>/<dest>/.

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
    excl = {".venv", "__pycache__", "node_modules", ".git"}
    for rel in manifest["paths"]:
        src = Path(clone) / str(rel)
        if not src.exists():
            return f"runtime path missing in clone: {rel}"
        # `rsync -a <src> <dest>/` (no trailing slash on src) copies the DIRECTORY into dest,
        # which is what _runtime_present() checks for; a file copies as itself.
        if src.is_dir():
            err = mirror_tree(src, dest / src.name, exclude=excl)
        else:
            try:
                shutil.copy2(src, dest / src.name)
                err = ""
            except OSError as exc:
                err = str(exc)
        if err:
            return f"sync runtime {rel}: {err}"
    return ""


def install_node_deps(name: str, cache_dir: Path) -> None:
    """Best-effort JS deps for a freshly mirrored version (canopy-gws MCP etc.). Never fatal.

    A plugin that ships `bin/<name>-install-deps` owns how its deps are installed, and
    this defers to it. ACE's links one runtime-only store per lockfile into each version
    and prunes long-orphaned versions — a full `npm install` per version, here, had left
    one machine holding 93 ACE versions, 16GB (2026-10-19). Anything else, or no bash to
    run the script with, gets the plain in-place install it always had.
    """
    script = cache_dir / "bin" / f"{name}-install-deps"
    if script.is_file() and os.access(script, os.X_OK) and shutil.which("bash"):
        rc, _out, _err = run(["bash", str(script), str(cache_dir)], NPM_TIMEOUT, cwd=str(cache_dir))
        if rc == 0:
            return
    if (cache_dir / "package.json").exists() and shutil.which("npm"):
        run(
            ["npm", "install", "--no-audit", "--no-fund"],
            NPM_TIMEOUT,
            cwd=str(cache_dir),
        )


def update_one(target: dict) -> dict:
    """Fetch → (if behind) pull + mirror into cache + npm + patch registry."""
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

    ref = target_ref(clone, branch)
    rc, remote_sha, err = run(["git", "rev-parse", f"{ref}^{{commit}}"], 10, cwd=clone)
    if rc != 0 or not remote_sha:
        result.update(status="error", error=f"rev-parse {ref}: {err or rc}")
        return result

    if remote_sha != target["sha"] and _is_ancestor(clone, remote_sha, target["sha"]):
        # Installed is AHEAD of the channel (a manual /canopy:update, or a machine that left
        # the main channel): never move a machine backwards; it rejoins when the channel passes.
        result.update(status="up-to-date")
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
    rc, _, err = run(["git", "reset", "--hard", remote_sha], PULL_TIMEOUT, cwd=clone)
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
    err = build_and_swap(name, Path(plugin_dir), clone, cache_dir)
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


# ── build off to the side, check, then swap ─────────────────────────────────
#
# Running sessions execute hooks and skills out of the version dir. A commit that does not bump
# the version lands in the SAME dir, so mirroring into it in place deleted `runtime/` and
# `node_modules/` under live sessions for as long as the rebuild took. Instead the new version is
# built complete in a staging dir on the same filesystem, checked, and renamed into place; a
# failed build or check leaves the installed version exactly as it was.

STAGING_DIR = "canopy-update-staging"   # under PLUGINS_DIR, outside cache/ so no scanner sees it
RETIRED_KEEP_SECONDS = 24 * 3600
# Carried over from the old version dir by hard link (a symlink stays a symlink), so a
# same-version update never has a moment without them and does not reinstall from scratch.
REUSE = ("node_modules", "runtime/.venv", "runtime/uv.lock")


def _staging_root() -> Path:
    return PLUGINS_DIR / STAGING_DIR


def _link_tree(src: Path, dest: Path) -> None:
    def _link(s, d):
        try:
            os.link(s, d)
        except OSError:
            shutil.copy2(s, d)
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dest, copy_function=_link, symlinks=True, dirs_exist_ok=True)
    elif src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        _link(src, dest)


def preflight(name: str, staged: Path) -> str:
    """Check a staged version before it goes live. "" when it may, else why not.

    Every plugin: the JSON its hooks read must parse. canopy: each PreToolUse guard must run
    under this python (the one the hook launcher found) — allow a plain command, and the gating
    guard must block a raw send. Guards fail closed, so a guard that cannot start would block
    every tool call in every session on this machine."""
    for rel in ("hooks/hooks.json", ".claude-plugin/plugin.json", "config/gating.json",
                "agent-core/gating-baseline.json"):
        p = staged / rel
        if p.exists() and not isinstance(load_json(p), dict):
            return f"preflight: {rel} is not a JSON object"
    if name != "canopy":
        return ""
    agent = _staging_root() / f"preflight-agent-{os.getpid()}"
    try:
        (agent / "config").mkdir(parents=True, exist_ok=True)
        (agent / "config" / "gating.json").write_text(
            json.dumps({"slug": "preflight", "channels": ["email"], "deny": [], "approve": []}))
        env = {**os.environ, "CANOPY_PLUGIN_DIR": str(staged)}
        for var in ("CANOPY_AGENT", "CANOPY_AGENT_SLUG", "CLAUDE_PROJECT_DIR"):
            env.pop(var, None)
        def payload(cmd):
            return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd},
                               "cwd": str(agent)})
        checks = [
            (["agent-core/gating_guard.py", "--session"], "git status", 0),
            (["agent-core/gating_guard.py", "--session"],
             "gog gmail send --to a@b.c --subject s --body b", 2),
            (["hooks/profile_guard.py"], "git status", 0),
            (["hooks/one_shot_turn_guard.py"], "git status", 0),
            (["hooks/credential_guard.py", "--pre"], "git status", 0),
        ]
        for argv, cmd, want in checks:
            script = staged / argv[0]
            if not script.exists():
                continue
            try:
                proc = subprocess.run([sys.executable, str(script), *argv[1:]],
                                      input=payload(cmd), env=env, cwd=str(agent),
                                      capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as exc:
                return f"preflight: {argv[0]} did not run: {exc}"
            if proc.returncode != want:
                tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
                return (f"preflight: {argv[0]} exited {proc.returncode} on `{cmd.split()[0]} …` "
                        f"(want {want}) under {sys.executable}: {tail[0][:200]}")
        return ""
    finally:
        shutil.rmtree(agent, ignore_errors=True)


def _clean_retired(now: float | None = None) -> None:
    """Drop retired version dirs once no session can plausibly still be running from them."""
    now = time.time() if now is None else now
    root = _staging_root()
    if not root.is_dir():
        return
    for entry in root.iterdir():
        try:
            if entry.name.startswith("retired-") and now - entry.stat().st_mtime > RETIRED_KEEP_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
            elif entry.name.startswith("build-") and now - entry.stat().st_mtime > 3600:
                shutil.rmtree(entry, ignore_errors=True)   # a crashed run's leftovers
        except OSError:
            continue


def build_and_swap(name: str, plugin_dir: Path, clone: str, cache_dir: Path) -> str:
    """Build `cache_dir`'s new contents in staging, preflight them, then rename into place.

    Returns "" on success, else an error; on error the installed dir is untouched."""
    root = _staging_root()
    stamp = f"{name}-{cache_dir.name}-{os.getpid()}-{int(time.time())}"
    staged = root / f"build-{stamp}"
    try:
        _clean_retired()
        root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True)
        if cache_dir.is_dir():
            for rel in REUSE:
                _link_tree(cache_dir / rel, staged / rel)
    except (OSError, shutil.Error) as exc:
        shutil.rmtree(staged, ignore_errors=True)
        return f"staging: {exc}"

    # Pure-Python mirror, not rsync: rsync does not exist on Windows.
    err = mirror_tree(plugin_dir, staged, exclude={".git"})
    if not err:
        # Runtime bundle (repo-root code declared in .claude-plugin/runtime.json). Fatal when
        # declared: a cache without its runtime strands every skill that shells into it.
        manifest = _runtime_manifest(plugin_dir)
        if manifest:
            err = sync_runtime(clone, manifest, staged)
    if not err:
        err = preflight(name, staged)
    if err:
        shutil.rmtree(staged, ignore_errors=True)
        return err if err.startswith("preflight") else f"sync: {err}"

    retired = root / f"retired-{stamp}"
    try:
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        if cache_dir.exists():
            os.replace(cache_dir, retired)       # the gap between these two renames is the
        os.replace(staged, cache_dir)            # only moment the dir is missing
    except OSError as exc:
        # Windows refuses to rename a dir a running process holds open. Put the old one back
        # and try again next session rather than mirror over files in use.
        if retired.exists() and not cache_dir.exists():
            try:
                os.replace(retired, cache_dir)
            except OSError:
                pass
        shutil.rmtree(staged, ignore_errors=True)
        return f"swap: {exc} (installed version left in place; retried next session)"
    # JS deps install against the FINAL path: a plugin's own installer (ACE's) keys its shared
    # store and its pruning on the cache layout. A same-version update already carries the old
    # node_modules (linked above), so this refreshes a working tree rather than an empty one.
    install_node_deps(name, cache_dir)
    return ""


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
    if status == "cli":
        return f"{name} {result['detail']}"
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


# ── the canopy CLI ──────────────────────────────────────────────────────────

CLI_TIMEOUT = 300


def _version_tuple(v: str) -> tuple:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else ()


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for cand in (HOME / ".local" / "bin" / name, HOME / ".local" / "bin" / f"{name}.exe",
                 HOME / ".cargo" / "bin" / name, Path("/opt/homebrew/bin") / name,
                 Path("/usr/local/bin") / name):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _uv_receipt(uv: str) -> Path | None:
    rc, out, _ = run([uv, "tool", "dir"], 30)
    return Path(out) / "canopy" / "uv-receipt.toml" if rc == 0 and out else None


def refresh_cli(clone: str) -> str:
    """Reinstall the `canopy` CLI from the marketplace clone when it is BEHIND the
    clone's plugin version. Returns what it did: "current", "refreshed", or why not.

    Only behind: a CLI ahead of the clone is a developer's install and is left alone.
    No CLI at all is left alone too — installing it is setup's job. Same reinstall as
    the /canopy:update skill and scripts/canopy-setup.sh: when the CLI already comes from
    this clone, `uv tool upgrade --reinstall canopy` rebuilds it and keeps what was
    installed beside it (`--with canopy-kb`); otherwise `install --reinstall --force`.
    On Windows a canopy.exe that is running cannot be replaced: that fails, is logged,
    and the next session tries again."""
    if _falsey(os.environ.get("CANOPY_CLI_REFRESH", "1")):
        return "off"
    want = _version_tuple(_read_version(Path(clone) / _plugin_source_subdir(clone, "canopy")))
    if not want:
        return "no canopy plugin version in the clone"
    cli = _which("canopy")
    if not cli:
        return "no canopy CLI"
    rc, out, _ = run([cli, "--version"], 30)
    have = _version_tuple(out) if rc == 0 else ()
    if have and have >= want:
        return "current"
    uv = _which("uv")
    if not uv:
        return "behind, and no uv to reinstall it"
    receipt = _uv_receipt(uv)
    try:
        text = receipt.read_text(encoding="utf-8") if receipt else ""
    except OSError:
        text = ""
    # TOML escapes a Windows path's backslashes.
    from_clone = any(f'directory = "{c}"' in text for c in (clone, clone.replace("\\", "\\\\")))
    cmd = ([uv, "tool", "upgrade", "--reinstall", "canopy"] if from_clone
           else [uv, "tool", "install", "--reinstall", "--force", clone])
    rc, out, err = run(cmd, CLI_TIMEOUT)
    if rc != 0:
        return f"reinstall failed: {(err or out)[-200:]}"
    fmt = lambda v: ".".join(map(str, v)) or "?"   # noqa: E731
    return f"refreshed {fmt(have)}→{fmt(want)}"


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

        canopy = next((t for t in targets if t["name"] == "canopy"), None)
        if canopy:
            try:
                cli = refresh_cli(canopy["clone"])
            except Exception as exc:  # noqa: BLE001 — never let the CLI step abort the run
                cli = f"error: {exc}"
            if cli not in ("current", "off", "no canopy CLI"):
                results.append({"name": "canopy-cli", "status": "cli", "detail": cli})

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


# ── `--sync-cache`: the /canopy:update skill's Step 2, sharing this module's mirror ─────────
#
# The update SKILL.md used to do this in bash with two `rsync` calls. rsync does not exist on
# Windows, and both calls sat inside a single `&&` chain, so Step 2 died at `rsync: command not
# found` (exit 127) AFTER the git pull and BEFORE the cache sync and the registry patch. The
# result is the worst available state and it is SILENT: the marketplace clone is on the new
# commit while installed_plugins.json still points at the old cache dir, so the plugin is
# half-updated with nothing to indicate it. Measured on @smazumdar's machine 2026-09-14 — cache
# stuck at 0.2.459 with the clone at 0.2.476.
#
# The mirror this needs already existed here (`mirror_tree`, added for exactly this reason on the
# hook path). Re-deriving it in the skill is what produced a fourth rsync call site; the fix is
# for the skill to CALL this, so there is one mirror in the codebase and the hook's tests cover
# the updater too.

SYNC_RETRIES = 3
SYNC_RETRY_DELAY = 2.0


def _retry_sync(what: str, fn) -> str:
    """Run a mirror step, retrying transient file-locking failures.

    Windows Defender holds freshly-written files open for a moment, so a copy or an unlink
    intermittently fails with `Access is denied. (os error -2147024891)` and then succeeds on a
    plain retry — the same pattern `claude plugin marketplace add` shows. Reported 2026-09-14.
    Retries are bounded and only for errors that look like contention; anything else returns at
    once, because retrying a real error just delays the message.
    """
    transient = ("access is denied", "permission denied", "being used by another process",
                 "resource busy", "errno 13", "winerror 5", "winerror 32")
    err = ""
    for attempt in range(1, SYNC_RETRIES + 1):
        err = fn()
        if not err:
            return ""
        if not any(t in err.lower() for t in transient) or attempt == SYNC_RETRIES:
            return err
        print(f"  {what}: {err} — retry {attempt}/{SYNC_RETRIES - 1}", file=sys.stderr)
        time.sleep(SYNC_RETRY_DELAY)
    return err


def sync_cache(clone: str, version: str, name: str = "canopy", marketplace: str = "canopy") -> int:
    """Mirror <clone>/plugins/<name> into the versioned cache, sync runtime, patch the registry.

    Prints one line per stage and returns 0 only when ALL of them succeeded — the skill chains on
    that exit code, so a partial sync must never look like a whole one.
    """
    clone_path = Path(clone).expanduser()
    plugin_dir = clone_path / _plugin_source_subdir(str(clone_path), name)
    cache_dir = PLUGINS_DIR / "cache" / marketplace / name / version

    if not plugin_dir.is_dir():
        print(f"SYNC FAILED: plugin source not found at {plugin_dir}", file=sys.stderr)
        return 1
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"SYNC FAILED: mkdir {cache_dir}: {exc}", file=sys.stderr)
        return 1

    # `delete=True` reproduces the skill's `rsync --delete`: the cache dir is keyed by VERSION, so
    # a reused version number finds the directory already populated with the OTHER commit's files
    # and a plain overlay would leave them. `node_modules`/`runtime` are excluded (and so kept, as
    # `mirror_tree` never deletes an excluded name) because the two stages below rebuild them.
    err = _retry_sync("plugin", lambda: mirror_tree(
        plugin_dir, cache_dir, exclude={".git", "node_modules", "runtime"}, delete=True))
    if err:
        print(f"SYNC FAILED: plugin mirror: {err}", file=sys.stderr)
        return 1
    print(f"PLUGIN CACHE: synced -> {cache_dir}")

    manifest = _runtime_manifest(plugin_dir)
    if manifest:
        err = _retry_sync("runtime", lambda: sync_runtime(str(clone_path), manifest, cache_dir))
        if err:
            print(f"SYNC FAILED: runtime bundle: {err}", file=sys.stderr)
            return 1
        print("RUNTIME BUNDLE: synced")

    if (cache_dir / "package.json").exists():
        if shutil.which("npm"):
            rc, _out, _err = run(["npm", "install", "--no-audit", "--no-fund"], NPM_TIMEOUT, cwd=str(cache_dir))
            print(f"GWS DEPS: {'installed' if rc == 0 else 'skipped (npm install failed)'}")
        else:
            print(f"GWS DEPS: skipped (npm missing — canopy-gws MCP needs: cd {cache_dir} && npm install)")

    rc, sha, err_txt = run(["git", "rev-parse", "HEAD"], 20, cwd=str(clone_path))
    if rc != 0 or not sha:
        print(f"SYNC FAILED: could not read clone HEAD sha: {err_txt}", file=sys.stderr)
        return 1

    if not patch_registry(name, marketplace, version, str(cache_dir), sha):
        print("SYNC FAILED: registry patch failed", file=sys.stderr)
        return 1
    print(f"VERIFIED: v{version} installed and registered (sha {sha[:8]})")
    return 0


def _sync_cache_from_argv(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="fleet_session_start_update.py --sync-cache")
    ap.add_argument("--sync-cache", action="store_true")
    ap.add_argument("--clone", required=True, help="marketplace clone root")
    ap.add_argument("--version", required=True, help="version to install as")
    ap.add_argument("--name", default="canopy")
    ap.add_argument("--marketplace", default="canopy")
    args = ap.parse_args(argv)
    return sync_cache(args.clone, args.version, args.name, args.marketplace)


if __name__ == "__main__":
    # `--sync-cache` is a real CLI the updater chains on, so it must FAIL LOUDLY. The bare hook
    # path below must do the opposite — a session-start hook that raises breaks every session —
    # which is why the blanket `except: sys.exit(0)` cannot be allowed to cover both.
    if "--sync-cache" in sys.argv[1:]:
        sys.exit(_sync_cache_from_argv(sys.argv[1:]))
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — a session-start hook must NEVER fail loudly
        sys.exit(0)
