"""External-dependency health for an agent box.

Both checks here exist because of one incident, 2026-09-07, and neither the
existing doctor checks nor `canopy agent doctor --all` could see any part of it.

`gog` is installed by Homebrew, whose prefix is VERSION-QUALIFIED
(`/opt/homebrew/Cellar/gogcli/<version>/bin/gog`). macOS keychain items record
their trusted reader as a `CodeSignatureAclSubject` pinning BOTH that absolute
path and the binary's cdhash. So every `brew upgrade gogcli` moves the binary
out from under the ACL: the path changes, the cdhash changes, and the stored
trust can never match again.

The failure that follows is nasty out of proportion to its cause. Each token
read raises a GUI keychain dialog, so every `gog` call BLOCKS — the runner's
inbox poll went from an 8s to a 26s median gap while it sat waiting on dialogs
nobody was there to answer. Worse, the obvious remedy does not work: gog is
ad-hoc signed, so "Always Allow" cannot durably attach a new grant (securityd
logs `-34018 Client has neither ... entitlements`) and the prompt returns on
the next read. On 2026-09-07 that was clicked ~50 times without effect.

The repair is to re-mint the items so their ACL pins the CURRENT binary: read
each refresh token out once, delete the stale item, and `gog auth import` it
back. What makes that repair hard to reach for is that nothing NAMED the cause
— the box just prompted forever. Hence `check_gog_keychain_trust`.

`check_dependency_upgrades` is the other half. Upgrading gog is the trigger, so
an agent box needs to know an upgrade is waiting BEFORE it takes it, and needs
the keychain consequence attached to that news rather than discovered after.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.doctor import CheckResult

#: Brew formulae the fleet's agents actually depend on at runtime. Kept
#: deliberately short: this check reports upgrades a human should take, not the
#: machine's entire outdated list (which on a dev laptop is hundreds of rows).
FLEET_BREW_DEPENDENCIES = ("gogcli",)

#: Named once so the check text and the docstring cannot drift apart.
KEYCHAIN_QUIRK = (
    "a brew upgrade moves gog to a new versioned path + cdhash, which "
    "permanently invalidates the keychain ACL that authorizes reading its "
    "tokens; 'Always Allow' cannot repair it (gog is ad-hoc signed)"
)


def _brew_outdated(runner) -> dict[str, tuple[str, str]] | None:
    """{formula: (installed, current)} for outdated formulae, or None if brew can't be read."""
    if not shutil.which("brew"):
        return None
    try:
        r = runner(["brew", "outdated", "--json=v2"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        payload = json.loads(r.stdout or "{}")
    except ValueError:
        return None
    out: dict[str, tuple[str, str]] = {}
    for f in payload.get("formulae", []) or []:
        installed = (f.get("installed_versions") or [""])[0]
        out[f.get("name", "")] = (installed, f.get("current_version", ""))
    return out


def check_dependency_upgrades(*, runner=subprocess.run) -> CheckResult:
    """Report upgrades waiting for the external CLIs the fleet depends on.

    Deliberately NOT a failure. An available upgrade is news, not unreadiness —
    making it red would leave every box permanently red and train everyone to
    ignore the one check that is supposed to be actionable. It reports ok with
    the upgrade named, and names the keychain consequence in the same breath so
    the person taking the upgrade learns the cost before paying it.
    """
    name = "Dependency upgrades"
    outdated = _brew_outdated(runner)
    if outdated is None:
        return CheckResult(name, True, "skipped — brew not installed or not introspectable")
    waiting = {d: outdated[d] for d in FLEET_BREW_DEPENDENCIES if d in outdated}
    if not waiting:
        deps = ", ".join(FLEET_BREW_DEPENDENCIES)
        return CheckResult(name, True, f"up to date ({deps})")
    parts = [f"{d} {have} -> {cur}" for d, (have, cur) in sorted(waiting.items())]
    detail = "upgrade available: " + "; ".join(parts)
    if "gogcli" in waiting:
        detail += (
            f" — NOTE: {KEYCHAIN_QUIRK}. After `brew upgrade gogcli`, re-mint the "
            f"tokens (see the `Gog keychain trust` check) or every gog call will "
            f"block on a dialog"
        )
    return CheckResult(name, True, detail)


def _gog_binary(runner=subprocess.run) -> Path | None:
    """Absolute, symlink-resolved path of the `gog` that would actually run."""
    found = shutil.which("gog")
    return Path(os.path.realpath(found)) if found else None


def _item_created_at(account: str, client: str, *, runner=subprocess.run) -> datetime | None:
    """Creation time of the gog keychain token for (client, account), UTC.

    Reads ATTRIBUTES only — no ``-w`` — so this never itself raises a prompt.
    """
    try:
        r = runner(
            ["security", "find-generic-password", "-s", "gogcli",
             "-a", f"token:{client}:{account}"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        if '"cdat"' not in line:
            continue
        stamp = line.split('"')[-2] if line.count('"') >= 2 else ""
        stamp = stamp.replace("\\000", "").strip()
        try:
            return datetime.strptime(stamp, "%Y%m%d%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def check_gog_keychain_trust(
    identity,
    *,
    runner=subprocess.run,
    binary_resolver=_gog_binary,
) -> CheckResult:
    """The gog keychain token must be NEWER than the gog binary that reads it.

    There is no prompt-free way to read an item's ACL back (the pinned path only
    surfaces in securityd's log at the moment it denies), so this compares
    mtimes instead: the item's ACL was written by whatever gog existed when the
    item was minted. If the binary on PATH is NEWER than the item, that binary
    is not the one on the ACL, and every read of that item will prompt.

    That ordering is the whole signal, and it is exactly right in both
    directions: it fires on the broken state (items minted Jul/Aug, binary
    installed Aug 28) and clears the moment the items are re-minted against the
    current binary — no version string to keep in sync, and it works for any
    future upgrade without being taught about it.
    """
    name = "Gog keychain trust"
    if identity is None:
        return CheckResult(name, False, "skipped — identity unresolved")
    binary = binary_resolver(runner) if binary_resolver is _gog_binary else binary_resolver()
    if binary is None or not binary.exists():
        return CheckResult(name, True, "skipped — gog not installed")
    minted = _item_created_at(identity.account, identity.client, runner=runner)
    if minted is None:
        return CheckResult(
            name, True,
            f"skipped — no gogcli keychain token for {identity.account} "
            f"under `{identity.client}` (see Auth client)")
    installed = datetime.fromtimestamp(binary.stat().st_mtime, tz=timezone.utc)
    if installed <= minted:
        return CheckResult(
            name, True,
            f"token for {identity.account} was minted "
            f"{minted:%Y-%m-%d} against the current gog ({binary})")
    return CheckResult(
        name, False,
        f"gog was upgraded after this token was minted — binary {binary} dates "
        f"{installed:%Y-%m-%d}, token dates {minted:%Y-%m-%d}, so the keychain ACL "
        f"pins a gog that no longer exists and EVERY gog call will block on a "
        f"keychain dialog ({KEYCHAIN_QUIRK}). Do NOT try 'Always Allow' — it "
        f"cannot stick. Re-mint instead: read the token out once "
        f"(security find-generic-password -s gogcli "
        f"-a token:{identity.client}:{identity.account} -w), extract "
        f".refresh_token, then: security delete-generic-password -s gogcli "
        f"-a token:{identity.client}:{identity.account} && gog auth import "
        f"--email={identity.account} --client={identity.client} "
        f"--refresh-token-stdin")
