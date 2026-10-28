"""Writing a private file works where os.fchmod doesn't exist (Windows before Python 3.13).

2026-10-28, failure investigation 17: an ace turn on a Windows laptop aborted because gog's
OAuth client file was never written, and `canopy agent bootstrap` — the prescribed fix — wrote
it through an unguarded os.fchmod.
"""
import os

import pytest

from orchestrator import agent_bootstrap, agent_instance, cred_cli


@pytest.mark.parametrize("mod", [agent_bootstrap, cred_cli, agent_instance])
def test_write_private_without_fchmod(mod, tmp_path, monkeypatch):
    monkeypatch.delattr(os, "fchmod", raising=False)
    target = tmp_path / "gogcli" / "credentials-canopy.json"
    mod._write_private(target, '{"installed": {}}')
    assert target.read_text(encoding="utf-8") == '{"installed": {}}'
