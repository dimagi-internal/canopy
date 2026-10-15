"""The fleet rail around SES sending domains (gating-baseline `always`).

WHY (2026-10-15). canopy-web's add-mail-domain is the one path that sets a sending domain
up: Easy DKIM, the bounce.<domain> MAIL FROM, the tenant configuration set, and the DNS
records to publish. It refuses to adopt an identity that already exists in SES, so a
hand-made one (dimagi.ai, that day) skips the standard and blocks the proper add.

The NEGATIVE half: reading identities and other SES calls stay free.

Run: uv run pytest tests/test_gating_baseline_ses_identity.py
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "canopy"

# Built at runtime so the literals never sit on a Bash command line the guard scans.
CREATE = "aws sesv2 create-" + "email-identity"
VERIFY = "aws ses verify-" + "domain-"


@pytest.fixture
def call(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN))
    loader = importlib.machinery.SourceFileLoader(
        "gating_guard_ses", str(PLUGIN / "agent-core" / "gating_guard.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    guard = importlib.util.module_from_spec(spec)
    loader.exec_module(guard)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gating.json").write_text(
        json.dumps({"slug": "eva", "channels": [], "deny": [], "approve": []}))
    return lambda tool, tool_input: guard.run(
        str(tmp_path), {"tool_name": tool, "tool_input": tool_input})


@pytest.mark.parametrize("cmd", [
    CREATE + " --email-identity example.org --profile labs",
    "cd /x && " + CREATE + " --email-identity example.org",
    "AWS_PROFILE=labs " + CREATE + " --email-identity example.org",
    VERIFY + "identity --domain example.org",
    VERIFY + "dkim --domain example.org",
])
def test_hand_made_identity_is_blocked_and_names_the_path(call, cmd):
    code, _, err = call("Bash", {"command": cmd})
    assert code == 2, cmd
    assert "add_mail_domain" in err


@pytest.mark.parametrize("cmd", [
    "aws sesv2 list-email-identities --profile labs",
    "aws sesv2 get-email-identity --email-identity example.org",
    "aws sesv2 delete-email-identity --email-identity example.org",
    "aws ses describe-active-receipt-rule-set",
])
def test_other_ses_calls_pass(call, cmd):
    code, _, _ = call("Bash", {"command": cmd})
    assert code == 0, cmd
