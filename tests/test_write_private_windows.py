"""Writing a private file works where os.fchmod doesn't exist (Windows before Python 3.13).

An ace turn on a Windows laptop aborts if gog's OAuth client file is never written, so
`canopy agent bootstrap` must not write it through an unguarded os.fchmod.
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
