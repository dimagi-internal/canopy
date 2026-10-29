"""`orchestrator/onepassword.py` is kept for possible future use and is deliberately unreferenced.

1Password is not a supported credential source: every secret resolves through the Canopy vault on
canopy-web. This pins that nothing wires the module back in, and that its helpers still work.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from orchestrator import onepassword as opw

ROOT = Path(__file__).resolve().parents[1]
IMPORT = re.compile(r"(?:from\s+orchestrator(?:\.onepassword|\s+import\s+[^\n]*\bonepassword)|"
                    r"import\s+orchestrator\.onepassword|from\s+\.onepassword|from\s+\.\s+import\s+[^\n]*onepassword)")


def test_nothing_imports_the_module():
    hits = []
    for base in ("src", "plugins", "packages", "hooks", "scripts"):
        for p in (ROOT / base).rglob("*.py"):
            if p.name == "onepassword.py":
                continue
            if IMPORT.search(p.read_text(encoding="utf-8", errors="replace")):
                hits.append(str(p.relative_to(ROOT)))
    assert hits == [], f"onepassword.py must stay unreferenced, but is imported by: {hits}"


def test_no_cli_surface():
    text = (ROOT / "src/orchestrator/cli.py").read_text(encoding="utf-8")
    assert "onepassword" not in text


class _Runner:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        rc, out = self.responses.get(tuple(argv[1:3]), (1, ""))
        return subprocess.CompletedProcess(argv, rc, out, "" if rc == 0 else "[ERROR] nope")


def test_read_and_item_helpers():
    r = _Runner({("read", "--no-newline"): (0, "s3cret"),
                 ("item", "get"): (0, json.dumps({"fields": [{"id": "credential", "value": "v"}]}))})
    op = opw.Op("acct.example", runner=r, env={})
    assert op.read("op://V/i/f") == "s3cret"
    assert op.get_field("V", "i", "credential") == "v"
    assert op.get_field("V", "i", "missing") == ""
    assert all(c[0][-2:] == ["--account", "acct.example"] for c in r.calls)


def test_read_failure_raises_without_a_value():
    op = opw.Op("a", runner=_Runner({}), env={})
    try:
        op.read("op://V/i/f")
    except opw.OnePasswordError as e:
        assert "could not read" in str(e)
    else:
        raise AssertionError("expected OnePasswordError")


def test_inject_pipes_the_template_through_stdin():
    seen = {}

    def runner(argv, **kw):
        seen.update(argv=argv, stdin=kw.get("input"))
        return subprocess.CompletedProcess(argv, 0, "A=1\n", "")

    assert opw.Op("a", runner=runner, env={}).inject("A=op://V/i/f\n") == "A=1\n"
    assert seen["argv"][:2] == ["op", "inject"] and seen["stdin"] == "A=op://V/i/f\n"
    assert "A=" not in " ".join(seen["argv"])   # never in argv


def test_person_env_drops_a_service_account_key():
    env = opw.person_env({"OP_SERVICE_ACCOUNT_TOKEN": "x", "PATH": "/bin"})
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in env and env["OP_BIOMETRIC_UNLOCK_ENABLED"] == "true"
