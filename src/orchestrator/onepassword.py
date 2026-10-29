"""1Password CLI (`op`) access helpers.

INTENTIONALLY UNREFERENCED. Dimagi's agent fleet keeps every credential in the Canopy vault
on canopy-web; 1Password is not a supported credential source and nothing in canopy imports
this module or exposes it on a CLI. It is kept only so the know-how (reading an item/field,
injecting a template, signing in as a person) survives in case a future use case needs it.
Do not wire it back into the credential path without a decision to do so.

Values travel through stdin/stdout and memory only; none is ever put in argv or printed.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Callable, Optional

Runner = Callable[..., subprocess.CompletedProcess]


class OnePasswordError(RuntimeError):
    """An `op` call failed. The message is op's last error line, which never carries a secret."""


def person_env(base: Optional[dict] = None) -> dict:
    """op's environment for a PERSON: no service-account token (it would win over the
    person's session), and the desktop app's sign-in when it is unlocked."""
    env = dict(os.environ if base is None else base)
    env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    env.setdefault("OP_BIOMETRIC_UNLOCK_ENABLED", "true")
    return env


class Op:
    def __init__(self, account: str, runner: Runner = subprocess.run,
                 env: Optional[dict] = None):
        self.account, self.runner = account, runner
        self.env = person_env(env) if env is None else dict(env)

    def _run(self, *args: str, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
        return self.runner(["op", *args, "--account", self.account], capture_output=True,
                           text=True, input=stdin, env=self.env)

    @staticmethod
    def _why(p: subprocess.CompletedProcess) -> str:
        lines = (p.stderr or "").strip().splitlines()
        return lines[-1][:300] if lines else f"exit {p.returncode}"

    def read(self, ref: str) -> str:
        """The value at an `op://vault/item/field` reference (raises when unreadable)."""
        p = self._run("read", "--no-newline", ref)
        if p.returncode != 0:
            raise OnePasswordError(f"could not read {ref}: {self._why(p)}")
        return p.stdout

    def get_item(self, vault: str, title: str) -> Optional[dict]:
        """The item's JSON document, or None when it does not exist."""
        p = self._run("item", "get", title, "--vault", vault, "--format", "json")
        return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None

    def get_field(self, vault: str, title: str, field_name: str) -> str:
        """One field of an item, "" when item or field is absent."""
        for f in (self.get_item(vault, title) or {}).get("fields") or []:
            if field_name in (f.get("id"), f.get("label")):
                return str(f.get("value") or "")
        return ""

    def inject(self, template: str, extra_env: Optional[dict] = None) -> str:
        """Resolve every `op://` reference in `template` text; the resolved text is returned
        in memory (it is secret — the caller must not log it)."""
        env = dict(self.env)
        env.update(extra_env or {})
        p = self.runner(["op", "inject", "--account", self.account], capture_output=True,
                        text=True, input=template, env=env)
        if p.returncode != 0:
            raise OnePasswordError(f"op inject failed: {self._why(p)}")
        return p.stdout
