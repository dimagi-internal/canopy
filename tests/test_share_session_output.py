"""What /canopy:share-session PRINTS — the two defects in dimagi-internal/canopy#484.

A share an agent made for a human was a black screen nobody could open. Neither
half was a server bug: the upload succeeded, and the two lines describing it were
wrong in ways that read as success. These tests pin the lines, because the failure
mode is a string, not an exception.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "share-session" / "upload.py"

_spec = importlib.util.spec_from_file_location("share_session_upload", _SCRIPT)
upload = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(upload)

API = "https://labs.connect.dimagi.com/canopy"


def _private(**kw) -> list[str]:
    return upload.format_session_result(
        API, visibility="private", slug="LATJRBGv", token=None, **kw
    )


def test_link_share_prints_the_share_url_alone():
    assert upload.format_session_result(
        API, visibility="link", slug="s", token="tok"
    ) == [f"Share: {API}/share/tok"]


def test_no_line_puts_a_bare_id_after_a_url():
    """The original bug: ``… <api>/sessions  (slug: LATJRBGv)`` on ONE line, which
    concatenates into ``<api>/sessions/LATJRBGv`` — not a route, so it renders the
    empty SPA shell. Any line carrying a URL must carry nothing after it."""
    for lines in (_private(owner_email="echo@dimagi-ai.com"),
                  upload.format_arc_result(
                      API, visibility="private", slug="ARC1", token=None,
                      owner_email="echo@dimagi-ai.com")):
        for line in lines:
            if "http" not in line:
                continue
            url = line[line.index("http"):]
            assert url == url.strip(), f"trailing content after URL: {line!r}"
            assert " " not in url, f"something follows the URL on its line: {line!r}"


def test_url_on_a_private_line_is_the_list_route_only():
    urls = [ln.strip() for ln in _private(owner_email="e@x.com") if "http" in ln]
    assert urls == [f"{API}/sessions"], urls


def test_private_names_the_owning_account_and_never_claims_dimagi_login():
    lines = _private(owner_email="echo@dimagi-ai.com")
    blob = "\n".join(lines)
    assert "echo@dimagi-ai.com" in blob
    # The false promise: private is owner-only, and an agent owns its own uploads.
    assert "dimagi login required" not in blob.lower()
    assert "nothing is shared" in blob.lower()


def test_private_degrades_honestly_when_the_server_omits_owner_email():
    """Deployment skew: a canopy-web predating owner_email on the upload response.
    Vaguer is fine; wrong is not."""
    blob = "\n".join(_private())
    assert "the account that uploaded it" in blob
    assert "dimagi login required" not in blob.lower()


def test_private_arc_says_it_has_no_page_at_all():
    """Worse than a private session: canopy-web has no arcs UI — ``/share/<token>``
    is the only arc surface, and a private arc has no token."""
    blob = "\n".join(
        upload.format_arc_result(
            API, visibility="private", slug="ARC1", token=None, owner_email="e@x.com"
        )
    )
    assert "NO page" in blob
    assert "e@x.com" in blob


def test_link_arc_prints_the_share_url():
    assert upload.format_arc_result(
        API, visibility="link", slug="ARC1", token="tok"
    ) == [f"Arc: {API}/share/tok"]


@pytest.mark.parametrize("fn", ["format_session_result", "format_arc_result"])
def test_private_tells_you_how_to_get_a_real_link(fn):
    blob = "\n".join(
        getattr(upload, fn)(API, visibility="private", slug="s", token=None)
    )
    assert "--private" in blob
