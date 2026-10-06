"""Pin that the scattered PAT/base-url helpers re-export the single canonical core
in orchestrator.canopy_web (the dedup target), rather than carrying their own copies."""
import scripts.ddd.auth as ddd_auth
from orchestrator import canopy_web


def test_canopy_web_canonical_values():
    assert canopy_web.DEFAULT_API == "https://canopy.dimagi.com"
    assert canopy_web.resolve_base_url("https://x/") == "https://x"


def test_ddd_auth_reexports_canopy_web():
    # Same function objects — not byte-copied reimplementations.
    assert ddd_auth.resolve_base_url is canopy_web.resolve_base_url
    assert ddd_auth.resolve_token is canopy_web.resolve_token
    assert ddd_auth.DEFAULT_API == canopy_web.DEFAULT_API
    assert ddd_auth.TOKEN_FILE == canopy_web.TOKEN_FILE
