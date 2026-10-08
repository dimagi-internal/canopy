"""Every canopy-web link the CLI prints or stores is ``/w/<workspace>/…``.

canopy-web#1337 removes the flat ``/walkthrough/``, ``/review/``, ``/share/`` and
``/ddd/`` pages outright (no redirect). On 2026-10-08 ACE emailed an external
reviewer a flat ``/review/<id>`` link it copied from ``scripts.ddd.narrative
post`` output — the server's raw ``url`` echoed into the JSON. So this is a
ratchet over OUTPUT, not just over helpers: each link-printing path is driven
with the flat (and ``localhost``) links an older canopy-web returns, and the
captured stdout/stderr/return value/run_state must hold no flat artifact link.
A source scan backs it, so a new path that glues a flat route onto the base
URL fails here before it ships.
"""
from __future__ import annotations

import importlib.util
import json
import re
import types
from pathlib import Path

import pytest

from orchestrator import canopy_web as cw
from orchestrator.canopy_web import FLAT_ARTIFACT_URL_RE, app_url, scope_link

REPO = Path(__file__).resolve().parents[1]
API = "https://canopy.test"


def assert_no_flat(*texts: object) -> None:
    for text in texts:
        hits = FLAT_ARTIFACT_URL_RE.findall(str(text))
        assert not hits, f"flat canopy artifact link(s) in CLI output: {hits}\n---\n{text}"


@pytest.fixture
def workspace(monkeypatch):
    """A write with its workspace configured and the read-back stubbed."""
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")
    monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
    monkeypatch.setenv("CANOPY_WEB_API_URL", API)
    monkeypatch.setattr(cw, "confirm_landed", lambda *a, **k: {})
    return "connect"


# --------------------------------------------------------------------------
# The detector and the two builders
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://canopy.dimagi.com/review/4bca7921-0000-0000-0000-000000000000/?t=x",
    "https://canopy.dimagi.com/walkthrough/abc",
    "https://canopy.dimagi.com/share/tok",
    "https://canopy.dimagi.com/ddd/reef/reef-2026-01-01-001",
    "https://labs.connect.dimagi.com/canopy/review/abc/",
    "https://localhost/walkthrough/abc?t=s",
    # the byte stream is scoped too since canopy-web#1338
    "https://canopy.dimagi.com/walkthrough/abc/content?t=x",
    # pre-tenancy walkthrough form
    "https://canopy.dimagi.com/w/22222222-2222-2222-2222-222222222222/content",
    # moved under /w/<ws>/ by canopy-web#1340
    "https://canopy.dimagi.com/storyboard/chlorine?t=x",
    "https://canopy.dimagi.com/narrative/chlorine?b=2",
    "https://canopy.dimagi.com/ddd-release/chlorine/chlorine-2026-10-08-004?t=x",
])
def test_detector_flags_flat_artifact_links(url):
    assert FLAT_ARTIFACT_URL_RE.search(url)


@pytest.mark.parametrize("url", [
    "https://canopy.dimagi.com/w/connect/review/abc?t=x",
    "https://canopy.dimagi.com/w/connect/ddd/reef",
    "https://canopy.dimagi.com/w/connect/walkthrough/abc/content?t=x",
    # third-party URL with /review/ deeper in its path
    "https://labs.connect.dimagi.com/microplans/program/133/plan/3536/review/",
])
def test_detector_leaves_scoped_and_non_page_links(url):
    assert not FLAT_ARTIFACT_URL_RE.search(url)


def test_app_url_requires_a_workspace():
    assert app_url("/review/r1", "connect", API) == f"{API}/w/connect/review/r1"
    with pytest.raises(cw.WorkspaceRequiredError):
        app_url("/review/r1", None, API)
    with pytest.raises(cw.WorkspaceRequiredError):
        app_url("/review/r1", "  ", API)


@pytest.mark.parametrize("raw, expected", [
    ("/review/r1/?t=x", f"{API}/w/connect/review/r1/?t=x"),
    (f"{API}/walkthrough/w1?t=s#scene-2", f"{API}/w/connect/walkthrough/w1?t=s#scene-2"),
    # minted on localhost by an in-process MCP call (canopy-web#1289) → rebased
    ("https://localhost/share/tok", f"{API}/w/connect/share/tok"),
    # already scoped: the server's workspace wins
    (f"{API}/w/dimagi/review/r1", f"{API}/w/dimagi/review/r1"),
    ("/ddd-release/n/r1?t=x", f"{API}/w/connect/ddd-release/n/r1?t=x"),
    ("/storyboard/chlorine", f"{API}/w/connect/storyboard/chlorine"),
    # deployment mount is kept
    ("https://labs.connect.dimagi.com/canopy/review/r1/",
     "https://labs.connect.dimagi.com/canopy/w/connect/review/r1/"),
    # not a canopy artifact page → untouched
    ("https://drive.google.com/file/d/x/view", "https://drive.google.com/file/d/x/view"),
])
def test_scope_link(raw, expected):
    assert scope_link(raw, "connect", API) == expected


def test_scope_link_refuses_a_flat_link_it_cannot_scope():
    with pytest.raises(cw.WorkspaceRequiredError):
        scope_link(f"{API}/review/r1", None, API)


# --------------------------------------------------------------------------
# Output ratchet: each link-printing path, fed an old server's flat links
# --------------------------------------------------------------------------

FLAT_REVIEW = {
    "id": "4bca7921-0000-0000-0000-000000000000",
    "url": "/review/4bca7921-0000-0000-0000-000000000000/?t=tok",
    "share_token": "tok",
    "workspace": "connect",
}


def test_narrative_post_output(workspace, monkeypatch, capsys):
    """The path that produced the link ACE emailed."""
    from scripts.ddd import narrative
    from scripts.ddd import review as rv

    monkeypatch.setattr(narrative, "post_narrative_version", lambda *a, **k: dict(FLAT_REVIEW))
    monkeypatch.setattr(rv, "_resolve_base_url", lambda _b: API)
    spec = Path(__file__)  # exists; post itself is stubbed
    narrative._cmd_post(str(spec), "chlorine-2026-10-08-001", force=True)

    cap = capsys.readouterr()
    out = json.loads(cap.out.strip().splitlines()[-1])
    assert out["url"] == out["share_url"] == f"{API}/w/connect/review/{FLAT_REVIEW['id']}/?t=tok"
    assert out["internal_url"] == f"{API}/w/connect/review/{FLAT_REVIEW['id']}"
    assert_no_flat(cap.out, cap.err)


def test_narrative_run_state_stamp(workspace, tmp_path, monkeypatch):
    import scripts.ddd.runstate as rs
    from scripts.ddd.narrative import _stamp_run_state
    from scripts.ddd.schemas.models import RunState

    monkeypatch.setattr(rs, "_resolve_ddd_dir", lambda: tmp_path)
    monkeypatch.setattr(cw, "resolve_base_url", lambda _b=None: API)
    run_id = "chlorine-2026-10-08-001"
    rs.save(RunState(run_id=run_id, narrative_slug="chlorine", phase="converged"))
    _stamp_run_state(run_id, dict(FLAT_REVIEW))
    stamped = rs.load(run_id).narrative_review_url
    assert stamped and "/w/connect/review/" in stamped
    assert_no_flat(stamped)


def test_publish_artifact_and_upload_video(workspace, monkeypatch, tmp_path, capsys):
    """ddd-upload's artifact links and `snippets upload-video`'s printout."""
    from scripts.ddd import review as rv
    from scripts.ddd import snippets, upload

    monkeypatch.setattr(
        upload, "_default_post",
        lambda *a, **k: {"id": "w1", "share_url": "https://localhost/walkthrough/w1?t=s"},
    )
    monkeypatch.setattr(
        rv, "get_narrative",
        lambda *a, **k: {"current_version": {"review_id": "r1", "version": 3}},
    )
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"\0")
    snippets.main(["upload-video", "chlorine", str(mp4)])
    cap = capsys.readouterr()
    assert f"{API}/w/connect/walkthrough/w1?t=s" in cap.out
    assert f"{API}/w/connect/ddd/chlorine" in cap.out
    assert_no_flat(cap.out, cap.err)

    assert upload.run_package_url("chlorine", "chlorine-2026-10-08-001") == \
        f"{API}/w/connect/ddd/chlorine/chlorine-2026-10-08-001"


def test_release_gate_prints_a_scoped_review_link(workspace, monkeypatch, capsys):
    from scripts.ddd import review as rv
    from scripts.ddd import upload

    monkeypatch.setattr(rv, "post_review_request", lambda *a, **k: dict(FLAT_REVIEW))
    monkeypatch.setattr(upload.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    upload._default_gate(object(), None, None)
    cap = capsys.readouterr()
    assert "/w/connect/review/" in cap.err
    assert_no_flat(cap.out, cap.err)


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_walkthrough_share_output(workspace, monkeypatch, tmp_path, capsys):
    ws_up = _load("scripts/walkthrough-share/upload.py", "walkthrough_share_upload")
    monkeypatch.setattr(ws_up, "resolve_pat", lambda: "test-pat")
    monkeypatch.setattr(ws_up.canopy_web, "confirm_landed", lambda *a, **k: {})
    monkeypatch.setattr(
        ws_up, "upload_multipart",
        lambda *a, **k: (201, {"id": "w9", "share_url": f"{API}/walkthrough/w9?t=s"}),
    )
    mp4 = tmp_path / "demo.mp4"
    mp4.write_bytes(b"\0")
    assert ws_up.main([str(mp4), "--public", "--workspace", "connect", "--api-url", API]) == 0
    cap = capsys.readouterr()
    assert f"View: {API}/w/connect/walkthrough/w9" in cap.out
    assert f"Share: {API}/w/connect/walkthrough/w9?t=s" in cap.out
    assert_no_flat(cap.out, cap.err)


def test_share_session_output():
    ss = _load("scripts/share-session/upload.py", "share_session_upload_ratchet")
    lines = ss.format_session_result(
        API, visibility="link", slug="s", token="tok",
        share_url=f"{API}/share/tok", workspace="connect",
    ) + ss.format_arc_result(
        API, visibility="link", slug="a", token="tok2", workspace="connect",
    ) + ss.format_session_result(API, visibility="link", slug="s", token="tok3")
    assert_no_flat("\n".join(lines))


# --------------------------------------------------------------------------
# Source ratchet: no new code glues a flat artifact route onto a base URL
# --------------------------------------------------------------------------

# `{<base>}/review/…` in an f-string — a link built outside app_url/scope_link.
# A `{…}` right after `/w/` is the workspace segment, i.e. already scoped.
_FLAT_BUILD_RE = re.compile(
    r"(?<!/w/)\{[^{}]*\}/(?:%s)/" % "|".join(cw.ARTIFACT_ROUTES)
)

_SCANNED = ("scripts", "src", "plugins/canopy/scripts", "plugins/canopy/hooks")


def test_no_source_builds_a_flat_artifact_link():
    offenders = []
    for root in _SCANNED:
        for py in (REPO / root).rglob("*.py"):
            for n, line in enumerate(py.read_text(errors="ignore").splitlines(), 1):
                if _FLAT_BUILD_RE.search(line):
                    offenders.append(f"{py.relative_to(REPO)}:{n}: {line.strip()}")
    assert not offenders, (
        "build canopy-web links with orchestrator.canopy_web.app_url / scope_link "
        "(always /w/<workspace>/…, canopy-web#1337):\n" + "\n".join(offenders)
    )


# --------------------------------------------------------------------------
# Send-path rail: `canopy email send` refuses a flat canopy link
# --------------------------------------------------------------------------

def _ident():
    from orchestrator.agent_email import EmailIdentity
    return EmailIdentity(slug="ace", account="ace@dimagi-ai.com", client="canopy")


def test_send_refuses_a_flat_canopy_link():
    """The 2026-10-08 email, as it went out."""
    from orchestrator.agent_email import AgentEmailError, send

    body = ("To watch all the cuts in one place: https://canopy.dimagi.com/review/"
            "4bca7921-9a57-49f5-ad9b-6cd518c9c138/?t=tok (open the Cuts tab).")
    with pytest.raises(AgentEmailError, match="flat canopy-web link"):
        send(_ident(), to="x@dimagi.com", subject="s", body_text=body, dry_run=True)


def test_send_allows_scoped_and_third_party_links():
    from orchestrator.agent_email import send

    body = ("Cuts: https://canopy.dimagi.com/w/connect/review/4bca7921?t=tok\n\n"
            "Someone else's share page: https://example.org/share/abc")
    assert send(_ident(), to="x@dimagi.com", subject="s", body_text=body, dry_run=True)["dry_run"]


def test_send_judges_only_the_agents_own_words():
    """A forward quotes someone else's message; only the note is the agent's."""
    from orchestrator.agent_email import send

    quoted = "Original: https://canopy.dimagi.com/walkthrough/abc?t=s"
    out = send(_ident(), to="x@dimagi.com", subject="Fwd", body_text="FYI\n\n" + quoted,
               review_text="FYI", dry_run=True)
    assert out["dry_run"]
