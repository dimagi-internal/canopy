"""scripts/test-docs.sh promises a throwaway HOME and an environment with no host credentials.

The script sets TEST_DOCS_HERMETIC=1; under it this test fails if the promise is broken
(a real HOME, a ~/.canopy, or a variable that looks like a secret). Outside the script it skips.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("TEST_DOCS_HERMETIC") != "1", reason="run via scripts/test-docs.sh")

SECRETISH = re.compile(r"TOKEN|SECRET|PASSWORD|API_?KEY|CREDENTIAL|_PAT\b|^CANOPY_|^GH_|^GITHUB_|^AWS_|^ANTHROPIC", re.I)


def test_home_is_a_throwaway():
    home = Path(os.environ["HOME"])
    assert home.name.startswith("canopy-test-home."), home
    assert Path.home() == home
    assert not (home / ".canopy").exists()


# tests/conftest.py points these at nothing for every test; CANOPY_WEB_PATH is the one variable
# scripts/test-docs.sh passes through on purpose (a path to a canopy-web checkout, not a secret).
SET_BY_CONFTEST = {"CANOPY_WEB_PATH", "CANOPY_SESSION_WEB", "CANOPY_DEVICE_KEY_BIN", "CANOPY_DEVICE_KEY_DIR", "CANOPY_AWS_IMDS_URL"}


def test_environment_holds_no_credentials():
    leaked = sorted(
        k for k in os.environ if SECRETISH.search(k) and k not in SET_BY_CONFTEST | {"TEST_DOCS_HERMETIC"}
    )
    assert not leaked, f"host variables reached the test environment: {leaked}"
