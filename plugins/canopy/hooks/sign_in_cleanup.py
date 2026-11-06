#!/usr/bin/env python3
"""SessionEnd: end every `canopy sign-in` whose lifetime has passed.

`canopy sign-in start|code` (``src/orchestrator/sign_in_cli.py``) signs a session in to a
third party with the person's approval; what the provider wrote must not outlive the work.
This runs when a Claude session ends and removes every sign-in whose lifetime has passed: the
provider's process is ended, the credential it made (only what THIS sign-in made, never one
already on the box) is dropped, and the state files go. It deliberately does NOT end a live
sign-in just because a session ended: in a canopy chat every turn is a new Claude process, so
that would tear the sign-in down between the turn that starts it and the one that uses it.
The lifetime (30 minutes, never extended) and `canopy sign-in stop` are the ends. The credential removal mirrors
``sign_in_cli._design_clear`` (stdlib only here; ``tests/test_sign_in_cli.py`` runs both).

Inert when ``~/.canopy/sign-in`` does not exist: every other session pays one `stat`.
Fail-open: never blocks the session from ending.
"""
import json
import os
import signal
import sys
import time

ROOT = os.path.expanduser("~/.canopy/sign-in")
CREDENTIALS = os.path.expanduser("~/.claude/.credentials.json")
#: provider name -> (credential key in CREDENTIALS, process argv marker)
PROVIDERS = {"design": ("designOauth", b"design-login")}


def _clear(key: str) -> None:
    try:
        with open(CREDENTIALS, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(doc, dict) or doc.pop(key, None) is None:
        return
    tmp = CREDENTIALS + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, CREDENTIALS)


def _kill(pid, marker: bytes) -> None:
    """End the provider's process group, only if `pid` is still that process."""
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as fh:
            ours = marker in fh.read().split(b"\0")
        if ours:
            os.killpg(int(pid), signal.SIGTERM)
    except (OSError, ValueError, TypeError):
        pass


def _end(name: str, doc: dict) -> None:
    cred, marker = PROVIDERS.get(str(doc.get("provider")), (None, b""))
    if cred:
        _kill(doc.get("pid"), marker)
        if doc.get("made_credential"):
            _clear(cred)
    for ext in ("json", "fifo", "out", "tmp"):
        try:
            os.unlink(os.path.join(ROOT, "%s.%s" % (name, ext)))
        except OSError:
            pass


def main() -> int:
    if not os.path.isdir(ROOT):
        return 0
    now = time.time()
    for fname in os.listdir(ROOT):
        if not fname.endswith(".json"):
            continue
        name = fname[:-5]
        try:
            with open(os.path.join(ROOT, fname), encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        try:
            expired = float(doc.get("expires_at") or 0) <= now
        except (TypeError, ValueError):
            expired = True
        if expired:
            _end(name, doc)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — never block a session from ending
        sys.exit(0)
