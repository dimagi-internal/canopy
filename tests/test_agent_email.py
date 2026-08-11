"""Tests for the shared agent email engine (canopy email — shared-gog-gdrive.md §3)."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import review_receipt as _rr
from orchestrator.agent_email import (
    AgentEmailError,
    EmailIdentity,
    archive,
    build_send_command,
    collect_thread_attachments,
    derive_reply_all,
    fetch_attachment,
    mark_read,
    normalize,
    parse_send_result,
    preflight,
    read_thread,
    resolve_email_identity,
    send,
    to_html,
)


# --------------------------------------------------------------------------------------
# identity resolution
# --------------------------------------------------------------------------------------

def _agent_repo(tmp_path, *, email="hal@dimagi-ai.com", gog_client=None, slug="hal"):
    repo = tmp_path / slug
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": slug}))
    agent = {"name": slug.title(), "email": email}
    if gog_client is not None:
        agent["gog_client"] = gog_client
    (repo / "config").mkdir()
    (repo / "config" / "agent.json").write_text(json.dumps(agent))
    return repo


def test_resolve_identity_from_agent_json(tmp_path):
    repo = _agent_repo(tmp_path, gog_client="hal-oauth")
    ident = resolve_email_identity(repo)
    assert ident.slug == "hal"
    assert ident.account == "hal@dimagi-ai.com"
    assert ident.client == "hal-oauth"


def test_resolve_identity_client_defaults_to_slug(tmp_path):
    ident = resolve_email_identity(_agent_repo(tmp_path))
    assert ident.client == "hal"


def test_resolve_identity_requires_mailbox(tmp_path):
    repo = _agent_repo(tmp_path, email="")
    with pytest.raises(AgentEmailError, match="config/agent.json"):
        resolve_email_identity(repo)


def test_resolve_identity_requires_agent_repo(tmp_path):
    with pytest.raises(AgentEmailError):
        resolve_email_identity(tmp_path)  # no .claude-plugin/plugin.json


# --------------------------------------------------------------------------------------
# body shaping (the echo-proven wrapper behavior)
# --------------------------------------------------------------------------------------

def test_normalize_collapses_hard_wrapped_paragraphs():
    text = "one line\nsplit over\nthree lines\n\nsecond para\n"
    assert normalize(text) == "one line split over three lines\n\nsecond para\n"


def test_normalize_keeps_bullets_as_lines():
    text = "intro\n\n- alpha\n- beta\n1. gamma\n"
    assert normalize(text) == "intro\n\n- alpha\n- beta\n1. gamma\n"


def test_to_html_paragraphs_and_bullets():
    out = to_html("hello there\n\n- one\n- two\n")
    assert "<p>hello there</p>" in out
    assert "<ul><li>one</li><li>two</li></ul>" in out


def test_to_html_numbered_list_becomes_ol_keeping_numbers():
    # canopy #291: `1. `/`2. ` must render as <ol> (auto-numbered), not a number-stripped <ul>.
    out = to_html("1. alpha\n2. beta\n")
    assert "<ol><li>alpha</li><li>beta</li></ol>" in out
    assert "<ul>" not in out


def test_to_html_coalesces_list_run_across_blank_lines():
    # canopy #291: blank-separated bullets must be ONE <ul>, not several single-item lists.
    out = to_html("- one\n\n- two\n\n- three\n")
    assert "<ul><li>one</li><li>two</li><li>three</li></ul>" in out
    assert out.count("<ul>") == 1


def test_to_html_ace836_repro_ol_then_ul():
    # The exact repro from ace#836 / canopy #291: an ordered list then a bullet list.
    out = to_html("1. alpha\n2. beta\n\n- one\n- two\n")
    assert "<ol><li>alpha</li><li>beta</li></ol>" in out
    assert "<ul><li>one</li><li>two</li></ul>" in out
    assert out.count("<ol>") == 1 and out.count("<ul>") == 1


def test_to_html_linkifies_and_escapes():
    out = to_html("see https://example.com/x?a=1 & <tags>\n")
    assert '<a href="https://example.com/x?a=1">' in out
    assert "&lt;tags&gt;" in out
    assert "&amp;" in out


def test_to_html_uses_client_default_font():
    # No font-family/size/color override — render in the client's default font.
    out = to_html("hello\n")
    assert "font-family" not in out
    assert "<html><body>" in out


def test_to_html_markdown_link_gets_clean_anchor_text():
    out = to_html("shipped [PR #867](https://github.com/dimagi-internal/connect-labs/pull/867)\n")
    # anchor text is the label, not the raw URL
    assert '<a href="https://github.com/dimagi-internal/connect-labs/pull/867">PR #867</a>' in out
    # the raw URL must NOT appear as visible text outside the href
    assert ">https://github.com/dimagi-internal/connect-labs/pull/867<" not in out


def test_to_html_markdown_and_bare_url_coexist():
    out = to_html("see [the deck](https://example.com/w/abc) or https://example.com/raw\n")
    assert '<a href="https://example.com/w/abc">the deck</a>' in out
    # the bare URL is still auto-linked (shown as itself)
    assert '<a href="https://example.com/raw">https://example.com/raw</a>' in out


def test_to_html_markdown_link_inside_bullet():
    out = to_html("- [issue #865](https://example.com/i/865)\n")
    assert "<ul>" in out
    assert '<a href="https://example.com/i/865">issue #865</a>' in out


def test_to_html_bare_url_sheds_trailing_sentence_punctuation():
    # echo → Fiorenzo 2026-07-13: "…/edit." linkified whole sent two broken gdoc
    # links (Google 404s the id + trailing dot). Punctuation stays in the prose.
    out = to_html("guide is at https://docs.google.com/document/d/abc123/edit. Next point.\n")
    assert '<a href="https://docs.google.com/document/d/abc123/edit">' in out
    assert '</a>. Next point.' in out


def test_to_html_bare_url_sheds_trailing_comma_and_question_mark():
    out = to_html("see https://example.com/a, or https://example.com/b?\n")
    assert '<a href="https://example.com/a">https://example.com/a</a>,' in out
    assert '<a href="https://example.com/b">https://example.com/b</a>?' in out


def test_to_html_renders_inline_bold_and_code():
    # Agents write markdown bodies; before this the engine converted only links, so
    # `**bold**` and backticks reached the recipient as literal punctuation.
    out = to_html("the **one** ask: read `compiler.ts:786` first\n")
    assert "<strong>one</strong>" in out
    assert "<code>compiler.ts:786</code>" in out
    assert "**" not in out and "`" not in out


def test_to_html_inline_markdown_applies_in_bullets_and_link_text():
    out = to_html("- **#375** is the [real `parity` ask](https://example.com/i/375)\n")
    assert "<li><strong>#375</strong> is the " in out
    assert '<a href="https://example.com/i/375">real <code>parity</code> ask</a>' in out


def test_to_html_inline_markdown_never_rewrites_a_url():
    # An href must survive verbatim — a stray asterisk pair in a query string would
    # otherwise become a tag and break the link.
    out = to_html("see https://example.com/s?q=**a**b\n")
    assert '<a href="https://example.com/s?q=**a**b">' in out
    assert "<strong>" not in out


def test_to_html_code_span_wins_over_bold_inside_it():
    out = to_html("write `**literal**` when you mean it\n")
    assert "<code>**literal**</code>" in out
    assert "<strong>" not in out


def test_to_html_leaves_unpaired_markers_alone():
    # A lone asterisk or backtick in prose must not swallow the rest of the paragraph.
    out = to_html("2 * 3 is 6 and a lone ` stays put\n")
    assert "2 * 3 is 6 and a lone ` stays put" in out
    assert "<strong>" not in out and "<code>" not in out


def test_to_html_bare_url_keeps_interior_punctuation():
    # Only TRAILING punctuation is shed — query strings and dotted paths survive.
    out = to_html("https://example.com/x?a=1&b=2\n")
    assert '<a href="https://example.com/x?a=1&amp;b=2">' in out


# --------------------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------------------

IDENT = EmailIdentity(slug="hal", account="hal@dimagi-ai.com", client="hal")


def test_build_send_command_full():
    cmd = build_send_command(
        IDENT, to="a@x.com", subject="Re: hi", plain_path="/tmp/p.txt",
        html_body="<html/>", cc="c@x.com", reply_to_message_id="m123",
    )
    assert cmd[:3] == ["gog", "gmail", "send"]
    assert ("--account", "hal@dimagi-ai.com") == (cmd[3], cmd[4])
    assert ("--client", "hal") == (cmd[5], cmd[6])
    assert "--json" in cmd
    assert cmd[cmd.index("--cc") + 1] == "c@x.com"
    assert cmd[cmd.index("--reply-to-message-id") + 1] == "m123"


def test_parse_send_result_normalizes_key_variants():
    assert parse_send_result('{"id": "m1", "threadId": "t1"}') == {
        "message_id": "m1", "thread_id": "t1", "raw": {"id": "m1", "threadId": "t1"}}
    r = parse_send_result('{"message_id": "m2", "thread_id": "t2"}')
    assert (r["message_id"], r["thread_id"]) == ("m2", "t2")
    r = parse_send_result("not json")
    assert (r["message_id"], r["thread_id"]) == ("", "")


def test_send_dry_run_never_invokes_gog():
    def boom(*a, **k):
        raise AssertionError("runner must not be called on dry-run")
    result = send(IDENT, to="a@x.com", subject="s",
                  body_text="hello\nworld\n", dry_run=True, runner=boom)
    assert result["dry_run"] is True
    assert result["account"] == "hal@dimagi-ai.com"
    assert result["plain"] == "hello world\n"
    assert "<p>hello world</p>" in result["html"]
    # shape parity with a real send: same routing keys, empty — callers never branch
    assert result["message_id"] == "" and result["thread_id"] == ""
    # cc always present (empty string when unset) — recipients are verified from
    # the dry-run, so hiding cc'd people there defeats the verification.
    assert result["cc"] == ""


def test_send_dry_run_shows_cc():
    result = send(IDENT, to="a@x.com", cc="boss@x.com", subject="s",
                  body_text="hi\n", dry_run=True)
    assert result["cc"] == "boss@x.com"


# --------------------------------------------------------------------------------------
# attachments (canopy#462 — forwards used to silently degrade to links)
# --------------------------------------------------------------------------------------

def test_build_send_command_repeats_attach_per_file():
    cmd = build_send_command(
        IDENT, to="a@x.com", subject="s", plain_path="/tmp/p.txt", html_body="<html/>",
        attachments=["/tmp/one.pdf", "/tmp/two.pdf"],
    )
    assert [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--attach"] == [
        "/tmp/one.pdf", "/tmp/two.pdf"]


def test_build_send_command_omits_attach_when_none():
    cmd = build_send_command(
        IDENT, to="a@x.com", subject="s", plain_path="/tmp/p.txt", html_body="<html/>",
    )
    assert "--attach" not in cmd


def test_send_rejects_missing_attachment_before_invoking_gog(tmp_path):
    def boom(*a, **k):
        raise AssertionError("runner must not be called when an attachment is missing")

    with pytest.raises(AgentEmailError, match="nope.pdf"):
        send(IDENT, to="a@x.com", subject="s", body_text="x\n",
             attachments=[str(tmp_path / "nope.pdf")], runner=boom)


def test_send_dry_run_shows_attachments(tmp_path):
    """The dry-run is where an agent verifies what will actually ride along — the same
    reason cc is always present. A forward that lost its PDFs is exactly canopy#462."""
    f = tmp_path / "invoice.pdf"
    f.write_bytes(b"%PDF-")
    result = send(IDENT, to="a@x.com", subject="s", body_text="hi\n",
                  attachments=[str(f)], dry_run=True)
    assert result["attachments"] == [str(f)]


def test_send_dry_run_attachments_key_present_when_empty():
    result = send(IDENT, to="a@x.com", subject="s", body_text="hi\n", dry_run=True)
    assert result["attachments"] == []


def test_send_passes_attachments_through_to_gog(reviewed, tmp_path):
    f = tmp_path / "receipt.pdf"
    f.write_bytes(b"%PDF-")
    seen = {}

    def fake_runner(cmd, capture_output, text, timeout):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout='{"id": "m1", "threadId": "t1"}', stderr="")

    reviewed("x\n")
    send(IDENT, to="a@x.com", subject="s", body_text="x\n",
         attachments=[str(f)], runner=fake_runner)
    assert seen["cmd"][seen["cmd"].index("--attach") + 1] == str(f)


def _thread_runner(attachments_per_message, downloads):
    """Fake gog: `gmail read` returns a thread, `gmail attachment` returns a cached path."""
    def run(cmd, capture_output, text, timeout):
        if cmd[1:3] == ["gmail", "read"]:
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                {"thread": {"messages": [
                    {"id": mid, "payload": {"headers": [], "parts": [
                        {"filename": a["filename"],
                         "mimeType": a.get("mime_type", "application/pdf"),
                         "body": {"attachmentId": a["attachment_id"], "size": a["size"]}}
                        for a in atts]}}
                    for mid, atts in attachments_per_message
                ]}}))
        if cmd[1:3] == ["gmail", "attachment"]:
            att_id = cmd[4]
            downloads.append(att_id)
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                {"path": f"/cache/{att_id}.pdf", "bytes": 10, "cached": True}))
        raise AssertionError(f"unexpected gog call: {cmd[:3]}")
    return run


def test_collect_thread_attachments_downloads_every_file():
    downloads = []
    runner = _thread_runner([
        ("m1", [{"filename": "Invoice.pdf", "attachment_id": "a1", "size": 10}]),
        ("m2", [{"filename": "Receipt.pdf", "attachment_id": "a2", "size": 20}]),
    ], downloads)
    got = collect_thread_attachments(IDENT, "t1", runner=runner)
    assert [a["filename"] for a in got] == ["Invoice.pdf", "Receipt.pdf"]
    assert [a["path"] for a in got] == ["/cache/a1.pdf", "/cache/a2.pdf"]
    assert downloads == ["a1", "a2"]


def test_collect_thread_attachments_dedupes_quoted_forwards():
    """The same file quoted forward and back must not ship three times."""
    downloads = []
    runner = _thread_runner([
        ("m1", [{"filename": "Invoice.pdf", "attachment_id": "a1", "size": 10}]),
        ("m2", [{"filename": "Invoice.pdf", "attachment_id": "a9", "size": 10}]),
    ], downloads)
    got = collect_thread_attachments(IDENT, "t1", runner=runner)
    assert [a["filename"] for a in got] == ["Invoice.pdf"]
    assert downloads == ["a1"]


def test_collect_thread_attachments_empty_thread_is_empty_list():
    runner = _thread_runner([("m1", [])], [])
    assert collect_thread_attachments(IDENT, "t1", runner=runner) == []


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    """Arrange a passing pre-send review for whatever body a test sends.

    send() now requires a review receipt for the exact body (review_receipt.py). These
    tests are about gog invocation / failure / timeout, so they record the receipt as
    part of arranging a valid send — the rail itself is covered in test_review_receipt.py.
    """
    monkeypatch.setenv("CANOPY_REVIEW_RECEIPTS_DIR", str(tmp_path / "receipts"))

    def _review(body_text):
        _rr.record(IDENT.slug, body_text, caught=[], verdict="clean")
    return _review


def test_send_invokes_gog_and_parses_result(reviewed):
    seen = {}

    def fake_runner(cmd, capture_output, text, timeout):
        seen["cmd"] = cmd
        seen["timeout"] = timeout
        seen["plain"] = Path(cmd[cmd.index("--body-file") + 1]).read_text()
        return SimpleNamespace(returncode=0, stdout='{"id": "m9", "threadId": "t9"}', stderr="")

    reviewed("para one\nwrapped\n")
    result = send(IDENT, to="a@x.com", subject="s", body_text="para one\nwrapped\n",
                  runner=fake_runner)
    assert result == {"message_id": "m9", "thread_id": "t9",
                      "raw": {"id": "m9", "threadId": "t9"}}
    assert seen["plain"] == "para one wrapped\n"
    # the temp plain-text file is cleaned up after the send
    assert not os.path.exists(seen["cmd"][seen["cmd"].index("--body-file") + 1])


def test_send_failure_raises_with_stderr(reviewed):
    def fake_runner(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="invalid_grant: bad token")

    reviewed("x\n")
    with pytest.raises(AgentEmailError, match="invalid_grant"):
        send(IDENT, to="a@x.com", subject="s", body_text="x\n", runner=fake_runner)


def test_send_timeout_raises_instead_of_hanging_the_turn(reviewed):
    def hung_runner(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    reviewed("x\n")
    with pytest.raises(AgentEmailError, match="timed out"):
        send(IDENT, to="a@x.com", subject="s", body_text="x\n", runner=hung_runner)


# --------------------------------------------------------------------------------------
# reply-all derivation (§1b rule 3 — raw reads hide Cc and drop cc'd people)
# --------------------------------------------------------------------------------------

def _thread_json(message_id="m1", frm="Dr. C <c@partner.org>",
                 to="ace@dimagi-ai.com, hal@dimagi-ai.com",
                 cc="ops@partner.org"):
    return json.dumps({"thread": {"messages": [{
        "id": message_id,
        "payload": {"headers": [
            {"name": "From", "value": frm},
            {"name": "To", "value": to},
            {"name": "Cc", "value": cc},
        ]},
    }]}})


def _reply_runner(stdout, returncode=0):
    def runner(cmd, capture_output, text, timeout):
        assert cmd[:3] == ["gog", "gmail", "read"] and "--json" in cmd
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    return runner


def test_derive_reply_all_sender_to_and_others_cc_excluding_self():
    to, cc, msg_id = derive_reply_all(IDENT, message_id="m1",
                                      runner=_reply_runner(_thread_json()))
    assert to == "c@partner.org"
    # hal (self) and the sender are excluded; the other To recipient + Cc survive
    assert cc == "ace@dimagi-ai.com, ops@partner.org"
    assert msg_id == "m1"


def test_derive_reply_all_missing_from_header_raises():
    bad = _thread_json(frm="")
    with pytest.raises(AgentEmailError, match="no From header"):
        derive_reply_all(IDENT, message_id="m1", runner=_reply_runner(bad))


def test_derive_reply_all_read_failure_raises():
    with pytest.raises(AgentEmailError, match="could not read"):
        derive_reply_all(IDENT, message_id="m1", runner=_reply_runner("", returncode=1))


def test_derive_reply_all_requires_exactly_one_id():
    with pytest.raises(AgentEmailError, match="exactly one"):
        derive_reply_all(IDENT, runner=_reply_runner(_thread_json()))
    with pytest.raises(AgentEmailError, match="exactly one"):
        derive_reply_all(IDENT, thread_id="t1", message_id="m1",
                         runner=_reply_runner(_thread_json()))


def _multi_message_thread():
    """A thread where the LATEST message is the agent's own send — reply-all must
    target the latest NON-self message (echo's live lesson), not the agent's."""
    def msg(mid, frm, to, cc=""):
        return {"id": mid, "payload": {"headers": [
            {"name": "From", "value": frm},
            {"name": "To", "value": to},
            {"name": "Cc", "value": cc},
        ]}}
    return json.dumps({"thread": {"messages": [
        msg("m1", "Dr. C <c@partner.org>", "hal@dimagi-ai.com", "ops@partner.org"),
        msg("m2", "Colleague <k@partner.org>", "hal@dimagi-ai.com, c@partner.org"),
        msg("m3", "Hal <hal@dimagi-ai.com>", "k@partner.org"),
    ]}})


def test_derive_reply_all_thread_mode_targets_latest_non_self_message():
    to, cc, msg_id = derive_reply_all(IDENT, thread_id="t1",
                                      runner=_reply_runner(_multi_message_thread()))
    assert to == "k@partner.org"          # sender of the latest NON-hal message (m2)
    assert cc == "c@partner.org"          # m2's other recipient, minus hal + sender
    assert msg_id == "m2"                 # threading targets m2, NOT the thread id


# --------------------------------------------------------------------------------------
# mark-read (via gog thread modify — NEVER the macOS Keychain, dimagi-internal/ace#827)
# --------------------------------------------------------------------------------------

def test_mark_read_shells_gog_thread_modify_per_thread():
    calls = []

    def fake_runner(cmd, capture_output, text, timeout):
        calls.append(cmd)
        if cmd[4] == "bad2":
            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    results = mark_read(IDENT, ["t1", "bad2"], runner=fake_runner)
    assert results[0] == {"thread_id": "t1", "ok": True, "error": ""}
    assert results[1]["ok"] is False and "not found" in results[1]["error"]
    assert calls[0] == ["gog", "gmail", "thread", "modify", "t1", "--remove", "UNREAD",
                        "--account", "hal@dimagi-ai.com", "--client", "hal"]


def test_mark_read_never_touches_the_keychain():
    """The #827 hang class: `security find-generic-password` blocks forever on a GUI
    prompt in non-interactive shells. The runner must only ever see gog commands."""
    def fake_runner(cmd, capture_output, text, timeout):
        assert cmd[0] == "gog", f"non-gog subprocess in mark_read: {cmd}"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    mark_read(IDENT, ["t1", "t2"], runner=fake_runner)


def test_mark_read_timeout_is_per_thread_and_keeps_going():
    def flaky_runner(cmd, capture_output, text, timeout):
        if cmd[4] == "slow":
            raise subprocess.TimeoutExpired(cmd, timeout)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    results = mark_read(IDENT, ["slow", "t2"], runner=flaky_runner)
    assert results[0]["ok"] is False and "timed out" in results[0]["error"]
    assert results[1]["ok"] is True


# --------------------------------------------------------------------------------------
# archive (own-mailbox turn housekeeping — INBOX *and* UNREAD)
# --------------------------------------------------------------------------------------

def test_archive_removes_inbox_and_unread_in_one_call():
    """A thread archived but left UNREAD still reads as unread mail and re-surfaces on
    the next poll — which is the whole thing archiving is meant to stop."""
    calls = []

    def fake_runner(cmd, capture_output, text, timeout):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    results = archive(IDENT, ["t1"], runner=fake_runner)
    assert results[0] == {"thread_id": "t1", "ok": True, "error": ""}
    assert calls[0] == ["gog", "gmail", "thread", "modify", "t1", "--remove", "INBOX,UNREAD",
                        "--account", "hal@dimagi-ai.com", "--client", "hal"]


def test_archive_only_ever_touches_the_agents_own_mailbox():
    """The rail: --account is the agent's own box, never a sibling's."""
    def fake_runner(cmd, capture_output, text, timeout):
        assert cmd[0] == "gog", f"non-gog subprocess in archive: {cmd}"
        assert cmd[cmd.index("--account") + 1] == IDENT.account
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    archive(IDENT, ["t1", "t2"], runner=fake_runner)


def test_archive_failure_is_per_thread_and_keeps_going():
    def flaky_runner(cmd, capture_output, text, timeout):
        if cmd[4] == "slow":
            raise subprocess.TimeoutExpired(cmd, timeout)
        if cmd[4] == "bad":
            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    results = archive(IDENT, ["slow", "bad", "t3"], runner=flaky_runner)
    assert results[0]["ok"] is False and "timed out" in results[0]["error"]
    assert results[1]["ok"] is False and "not found" in results[1]["error"]
    assert results[2]["ok"] is True


# --------------------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------------------

def _gog_home(tmp_path, *, creds=True, mapped=True):
    home = tmp_path / "gogcli"
    home.mkdir()
    if creds:
        (home / "credentials-hal.json").write_text('{"client_id": "x", "client_secret": "y"}')
    if mapped:
        (home / "config.json").write_text(
            json.dumps({"account_clients": {"hal@dimagi-ai.com": "hal"}}))
    return str(home)


def test_preflight_missing_creds_gives_exact_login_remediation(tmp_path):
    ok, lines = preflight(IDENT, gog_dir=_gog_home(tmp_path, creds=False))
    assert not ok
    joined = "\n".join(lines)
    assert "credentials-hal.json" in joined
    assert "gog login hal@dimagi-ai.com --client hal --services" in joined


def _provision_repo(tmp_path, op_ref="op://AI-Agents/Hal - gog OAuth client/notesPlain"):
    """An agent repo whose config/secrets.yaml declares the gog client for provisioning."""
    repo = _agent_repo(tmp_path)
    (repo / "config" / "secrets.yaml").write_text(
        "secrets:\n"
        "  - name: hal gog OAuth client JSON\n"
        f'    op: "{op_ref}"\n'
        '    target: "~/Library/Application Support/gogcli/credentials-hal.json"\n'
    )
    return repo


def test_preflight_missing_creds_routes_through_provision_when_declared(tmp_path, monkeypatch):
    # Declared + 1Password item resolves -> tell the user to `canopy provision`, not hand-copy.
    from orchestrator import provision
    monkeypatch.setattr(provision, "_op_read", lambda ref: "{\"client_id\": \"x\"}")
    ident = EmailIdentity(slug="hal", account="hal@dimagi-ai.com", client="hal",
                          repo=_provision_repo(tmp_path))
    ok, lines = preflight(ident, gog_dir=_gog_home(tmp_path, creds=False))
    assert not ok
    joined = "\n".join(lines)
    assert "canopy provision" in joined
    assert "gog login hal@dimagi-ai.com --client hal" in joined
    assert "Copy hal's OWN OAuth client JSON there" not in joined  # not the manual fallback


def test_preflight_missing_1password_item_is_the_named_blocker(tmp_path, monkeypatch):
    # Declared but the 1Password item doesn't resolve -> that IS the blocker, named exactly.
    from orchestrator import provision
    def boom(ref):
        raise provision.ProvisionError(f"`op read {ref}` failed: isn't an item")
    monkeypatch.setattr(provision, "_op_read", boom)
    ident = EmailIdentity(slug="hal", account="hal@dimagi-ai.com", client="hal",
                          repo=_provision_repo(tmp_path))
    ok, lines = preflight(ident, gog_dir=_gog_home(tmp_path, creds=False))
    assert not ok
    joined = "\n".join(lines)
    assert "op://AI-Agents/Hal - gog OAuth client/notesPlain" in joined
    assert "canopy provision" in joined
    assert "isn't in 1Password yet" in joined


def test_preflight_unmapped_account(tmp_path):
    ok, lines = preflight(IDENT, gog_dir=_gog_home(tmp_path, mapped=False))
    assert not ok
    assert any("account_clients" in ln for ln in lines)


def test_preflight_live_check_passes(tmp_path):
    def fake_runner(cmd, capture_output, text, timeout):
        assert cmd[:3] == ["gog", "gmail", "search"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    ok, lines = preflight(IDENT, gog_dir=_gog_home(tmp_path), runner=fake_runner)
    assert ok
    assert "gog Gmail ready" in lines[0]


def test_preflight_bad_token_names_login_fix(tmp_path):
    def fake_runner(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="token expired or revoked")

    ok, lines = preflight(IDENT, gog_dir=_gog_home(tmp_path), runner=fake_runner)
    assert not ok
    assert any("gog login hal@dimagi-ai.com --client hal" in ln for ln in lines)


def test_preflight_api_not_enabled_self_heal(tmp_path):
    stderr = ("Error: Gmail API has not been used in project 123 before or it is disabled. "
              "Enable it by visiting https://console.developers.google.com/apis/api/gmail/overview?project=123 "
              "then retry.")

    def fake_runner(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr=stderr)

    ok, lines = preflight(IDENT, gog_dir=_gog_home(tmp_path), runner=fake_runner)
    assert not ok
    joined = "\n".join(lines)
    assert "NOT a token problem" in joined
    assert "https://console.developers.google.com" in joined


# --- engine staleness guard (the 2026-07-10 Arial-email regression) ---------

from orchestrator.agent_email import _version_tuple, engine_staleness_error


def _write_clone(tmp_path, version):
    clone = tmp_path / "marketplace-canopy"
    clone.mkdir()
    (clone / "pyproject.toml").write_text(
        f'[project]\nname = "canopy"\nversion = "{version}"\n'
    )
    return clone


def _running_version():
    from importlib.metadata import version

    return version("canopy")


def test_version_tuple_handles_plain_and_suffixed_parts():
    assert _version_tuple("0.2.270") == (0, 2, 270)
    assert _version_tuple("1.0.0rc1") == (1, 0, 0)
    assert _version_tuple("0.2.270") < _version_tuple("0.2.271")


def test_staleness_fires_when_clone_is_ahead(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPY_EMAIL_SKIP_ENGINE_CHECK", raising=False)
    clone = _write_clone(tmp_path, "999.0.0")
    err = engine_staleness_error(clone_dir=clone)
    assert err is not None
    assert "999.0.0" in err
    assert _running_version() in err
    assert "uv tool install --reinstall" in err


def test_staleness_quiet_when_running_matches_clone(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPY_EMAIL_SKIP_ENGINE_CHECK", raising=False)
    clone = _write_clone(tmp_path, _running_version())
    assert engine_staleness_error(clone_dir=clone) is None


def test_staleness_quiet_when_running_is_ahead_of_clone(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPY_EMAIL_SKIP_ENGINE_CHECK", raising=False)
    clone = _write_clone(tmp_path, "0.0.1")
    assert engine_staleness_error(clone_dir=clone) is None


def test_staleness_quiet_when_clone_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPY_EMAIL_SKIP_ENGINE_CHECK", raising=False)
    assert engine_staleness_error(clone_dir=tmp_path / "nope") is None


def test_staleness_env_bypass(tmp_path, monkeypatch):
    clone = _write_clone(tmp_path, "999.0.0")
    monkeypatch.setenv("CANOPY_EMAIL_SKIP_ENGINE_CHECK", "1")
    assert engine_staleness_error(clone_dir=clone) is None


# --------------------------------------------------------------------------------------
# narrow-reply guard (dropped_participants — hal 2026-07-10: a reply on a 4-person
# thread went to one person; the item's owner never saw the answer)
# --------------------------------------------------------------------------------------

from orchestrator.agent_email import dropped_participants


def _guard_runner(thread_json, get_ok=True):
    """Answers `gog gmail get <msg> --json` with a threadId, then
    `gog gmail read <thread> --json` with the thread fixture."""
    def runner(cmd, capture_output, text, timeout):
        if cmd[:3] == ["gog", "gmail", "get"]:
            if not get_ok:
                return SimpleNamespace(returncode=1, stdout="", stderr="404")
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {"message": {"threadId": "T1"}}), stderr="")
        assert cmd[:3] == ["gog", "gmail", "read"]
        return SimpleNamespace(returncode=0, stdout=thread_json, stderr="")
    return runner


def test_narrow_reply_lists_dropped_thread_participants():
    # thread audience: c@partner.org (sender) + ops@partner.org + ace (self excluded)
    dropped = dropped_participants(
        IDENT, message_id="m1", to="c@partner.org", cc=None,
        runner=_guard_runner(_thread_json()))
    assert dropped == ["ace@dimagi-ai.com", "ops@partner.org"]


def test_narrow_reply_quiet_when_everyone_is_covered():
    dropped = dropped_participants(
        IDENT, message_id="m1",
        to="c@partner.org", cc="ops@partner.org, ace@dimagi-ai.com",
        runner=_guard_runner(_thread_json()))
    assert dropped == []


def test_narrow_reply_guard_is_best_effort_on_read_failure():
    dropped = dropped_participants(
        IDENT, message_id="m1", to="c@partner.org", cc=None,
        runner=_guard_runner(_thread_json(), get_ok=False))
    assert dropped == []


def test_narrow_reply_accepts_thread_id_directly():
    dropped = dropped_participants(
        IDENT, thread_id="T1", to="c@partner.org", cc="ops@partner.org",
        runner=_guard_runner(_thread_json()))
    assert dropped == ["ace@dimagi-ai.com"]


def test_login_services_includes_appscript():
    """Apps Script must be in the standard login set — some agents drive Drive via it."""
    from orchestrator.agent_email import LOGIN_SERVICES
    assert "appscript" in {s.strip() for s in LOGIN_SERVICES.split(",")}


def test_granted_services_parses_matching_account():
    import json
    from types import SimpleNamespace
    from orchestrator.agent_email import EmailIdentity, granted_services
    payload = json.dumps({"accounts": [
        {"email": "hal@dimagi-ai.com", "client": "hal", "services": ["gmail", "appscript"]},
        {"email": "echo@dimagi-ai.com", "client": "echo", "services": ["gmail"]}]})
    ident = EmailIdentity(slug="hal", account="hal@dimagi-ai.com", client="hal")
    got = granted_services(ident, runner=lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=payload, stderr=""))
    assert got == {"gmail", "appscript"}


def test_granted_services_none_when_account_absent_or_gog_fails():
    from types import SimpleNamespace
    from orchestrator.agent_email import EmailIdentity, granted_services
    ident = EmailIdentity(slug="nope", account="nope@dimagi-ai.com", client="nope")
    # gog returns other accounts but not this one -> None
    assert granted_services(ident, runner=lambda *a, **k: SimpleNamespace(
        returncode=0, stdout='{"accounts": []}', stderr="")) is None
    # gog fails -> None
    assert granted_services(ident, runner=lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="", stderr="boom")) is None


# --- inbound read + attachment (canopy email read / fetch-attachment) --------------

_ACE = EmailIdentity(slug="ace", account="ace@dimagi-ai.com", client="ace")


def _runner_ok(stdout):
    return lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _inbound_thread_json(parts):
    return json.dumps({"thread": {"messages": [{
        "id": "msg-123", "snippet": "hi",
        "payload": {"headers": [
            {"name": "From", "value": "Enock <e@spark.org>"},
            {"name": "To", "value": "ace@dimagi-ai.com"},
            {"name": "Cc", "value": "sam@dimagi-associate.com"},
            {"name": "Subject", "value": "FCAP"},
            {"name": "Date", "value": "Fri, 24 Jul 2026 02:47:00 -0000"},
        ], "parts": parts},
    }]}})


def test_read_thread_normalizes_headers_and_finds_attachment():
    parts = [
        {"mimeType": "text/plain", "body": {"size": 5, "data": "aGk="}},
        {"filename": "fcap.xlsx", "mimeType": "application/xlsx",
         "body": {"attachmentId": "ATT_1", "size": 444892}},
    ]
    res = read_thread(_ACE, "thr-1", runner=_runner_ok(_inbound_thread_json(parts)))
    assert res["thread_id"] == "thr-1"
    m = res["messages"][0]
    assert m["from"] == "Enock <e@spark.org>" and m["cc"] == "sam@dimagi-associate.com"
    assert m["subject"] == "FCAP" and m["message_id"] == "msg-123"
    assert m["attachments"] == [{"attachment_id": "ATT_1", "filename": "fcap.xlsx",
                                 "mime_type": "application/xlsx", "size": 444892}]


def test_read_thread_finds_nested_attachment():
    parts = [{"mimeType": "multipart/mixed", "parts": [
        {"filename": "deep.pdf", "mimeType": "application/pdf",
         "body": {"attachmentId": "ATT_D", "size": 10}},
    ]}]
    res = read_thread(_ACE, "thr-1", runner=_runner_ok(_inbound_thread_json(parts)))
    assert res["messages"][0]["attachments"][0]["attachment_id"] == "ATT_D"


def test_read_thread_no_attachments_when_only_body():
    parts = [{"mimeType": "text/plain", "body": {"size": 5, "data": "aGk="}}]
    res = read_thread(_ACE, "thr-1", runner=_runner_ok(_inbound_thread_json(parts)))
    assert res["messages"][0]["attachments"] == []


def test_read_thread_raises_on_gog_failure():
    bad = lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="404 not found")
    with pytest.raises(AgentEmailError) as e:
        read_thread(_ACE, "bad", runner=bad)
    assert "THREAD id" in str(e.value)


def test_fetch_attachment_returns_path_not_data():
    out = _runner_ok('{"bytes": 444892, "cached": false, "path": "/cache/fcap.xlsx"}')
    res = fetch_attachment(_ACE, "msg-123", "ATT_1", runner=out)
    assert res["path"] == "/cache/fcap.xlsx" and res["bytes"] == 444892
    assert res["saved_to"] is None


def test_fetch_attachment_copies_to_out_dir(tmp_path):
    src = tmp_path / "src.xlsx"
    src.write_bytes(b"xlsxdata")
    out = _runner_ok(json.dumps({"bytes": 8, "cached": True, "path": str(src)}))
    dest = tmp_path / "landing"
    res = fetch_attachment(_ACE, "m", "a", out_dir=str(dest), runner=out)
    assert res["saved_to"] == str(dest / "src.xlsx")
    assert (dest / "src.xlsx").read_bytes() == b"xlsxdata"


def test_fetch_attachment_raises_when_no_path():
    out = _runner_ok('{"bytes": 5, "cached": false}')
    with pytest.raises(AgentEmailError) as e:
        fetch_attachment(_ACE, "m", "a", runner=out)
    assert "no path" in str(e.value)


def test_read_thread_decodes_and_trims_body_text():
    # base64url of: "Hello team, please review.\n\n> quoted old line\nOn Mon someone wrote:\nold"
    b64 = ("SGVsbG8gdGVhbSwgcGxlYXNlIHJldmlldy4KCj4gcXVvdGVkIG9sZCBsaW5lCk9u"
           "IE1vbiBzb21lb25lIHdyb3RlOgpvbGQ=")
    parts = [{"mimeType": "text/plain", "body": {"size": 10, "data": b64}}]
    res = read_thread(_ACE, "t", runner=_runner_ok(_inbound_thread_json(parts)))
    assert res["messages"][0]["body_text"] == "Hello team, please review."


def test_read_thread_computes_reply_all():
    parts = [{"mimeType": "text/plain", "body": {"size": 2, "data": "aGk="}}]
    res = read_thread(_ACE, "t", runner=_runner_ok(_inbound_thread_json(parts)))
    ra = res["reply_all"]
    # latest (only) sender is e@spark.org; self (ace@) dropped from Cc; sam@ kept
    assert ra["to"] == "e@spark.org"
    assert ra["cc"] == "sam@dimagi-associate.com"
    assert ra["reply_to_message_id"] == "msg-123"


# --------------------------------------------------------------------------------------
# reconcile_client — the OAuth client follows the token, because identity is the MAILBOX
#
# Echo went dark twice on the same defect pointing opposite ways (2026-07-31 and 2026-08-10).
# The fleet migrated every agent onto the shared `canopy` client, but that migration is
# per-MACHINE (re-login each mailbox on each box) while `gog_client` is one repo-global
# value. The laptop got migrated; the cloud box did not. Before 2026-08-06 the config said
# `echo` — right for cloud, wrong for the laptop. #117 flipped it to `canopy` and verified
# green on the laptop. Echo prefers the cloud runner, so the very next turn to land there
# failed the other way. Whichever value is pinned, one box is broken, and which one only
# surfaces when a turn happens to land on it.
# --------------------------------------------------------------------------------------

def _auth_accounts_runner(accounts, *, returncode=0):
    payload = json.dumps({"accounts": accounts})

    def run(cmd, capture_output, text, timeout):
        assert cmd[:3] == ["gog", "auth", "list"]
        return SimpleNamespace(returncode=returncode, stdout=payload, stderr="")
    return run


def _echo(client="canopy"):
    return EmailIdentity(slug="echo", account="echo@dimagi-ai.com", client=client)


def test_reconcile_client_leaves_a_matching_pin_alone():
    """The fast path must not touch a correctly-configured box."""
    from orchestrator.agent_email import reconcile_client
    runner = _auth_accounts_runner([
        {"email": "echo@dimagi-ai.com", "client": "canopy", "services": ["gmail"]}])
    ident = _echo()
    out = reconcile_client(ident, runner=runner)
    assert out.client == "canopy" and not out.changed and ident.client == "canopy"


def test_reconcile_client_follows_the_only_token_the_mailbox_has():
    """The cloud box: config pins `canopy`, the only echo@ token is under `echo`.

    gog looks credentials up by (mailbox, client) as a PAIR, so the configured pair has no
    token and every read and send dies with `No auth for gmail echo@dimagi-ai.com` — while a
    working token sits right there under another client."""
    from orchestrator.agent_email import reconcile_client
    runner = _auth_accounts_runner([
        {"email": "echo@dimagi-ai.com", "client": "echo", "services": ["gmail", "drive"]}])
    out = reconcile_client(_echo(), runner=runner)
    assert out.client == "echo" and out.changed
    assert out.declared == "canopy"
    assert "echo@dimagi-ai.com" in out.note and "canopy" in out.note


def test_reconcile_client_never_changes_the_mailbox():
    """Identity is the MAILBOX; the client is plumbing. A sibling's token must never be
    borrowed — identity bleed is the fleet's one hard rule."""
    from orchestrator.agent_email import reconcile_client
    runner = _auth_accounts_runner([
        {"email": "eva@dimagi-ai.com", "client": "canopy", "services": ["gmail"]}])
    out = reconcile_client(_echo(), runner=runner)
    assert out.account == "echo@dimagi-ai.com"
    assert out.client == "canopy" and not out.changed   # unchanged: no echo@ token at all


def test_reconcile_client_refuses_to_guess_between_two_candidates():
    """Two clients hold a token for this mailbox — picking one is a coin flip, so keep the
    declared value and hand the caller the candidates to report."""
    from orchestrator.agent_email import reconcile_client
    runner = _auth_accounts_runner([
        {"email": "echo@dimagi-ai.com", "client": "echo", "services": ["gmail"]},
        {"email": "echo@dimagi-ai.com", "client": "legacy", "services": ["gmail"]}])
    out = reconcile_client(_echo(), runner=runner)
    assert not out.changed and out.client == "canopy"
    assert out.ambiguous == ["echo", "legacy"]


def test_reconcile_client_is_a_noop_when_gog_cannot_be_read():
    """No introspection means no evidence; behave exactly as before rather than guessing."""
    from orchestrator.agent_email import reconcile_client
    out = reconcile_client(_echo(), runner=_auth_accounts_runner([], returncode=1))
    assert out.client == "canopy" and not out.changed and out.candidates is None


def test_reconcile_client_mutates_in_place_when_asked():
    """The runtime path wants the identity itself corrected before any gog command is built."""
    from orchestrator.agent_email import reconcile_client
    runner = _auth_accounts_runner([
        {"email": "echo@dimagi-ai.com", "client": "echo", "services": ["gmail"]}])
    ident = _echo()
    reconcile_client(ident, runner=runner, apply=True)
    assert ident.client == "echo" and ident.account == "echo@dimagi-ai.com"


def test_clients_for_account_lists_every_stored_client():
    from orchestrator.agent_email import clients_for_account
    runner = _auth_accounts_runner([
        {"email": "echo@dimagi-ai.com", "client": "echo", "services": ["gmail", "drive"]},
        {"email": "eva@dimagi-ai.com", "client": "canopy", "services": ["gmail"]}])
    got = clients_for_account("echo@dimagi-ai.com", runner=runner)
    assert got == [{"client": "echo", "services": {"drive", "gmail"}}]


def test_clients_for_account_distinguishes_unreadable_from_absent():
    """None means 'could not look'; [] means 'looked, this mailbox has no token'. Collapsing
    the two is what let the doctor skip the check in exactly the broken case."""
    from orchestrator.agent_email import clients_for_account
    assert clients_for_account("echo@dimagi-ai.com",
                               runner=_auth_accounts_runner([], returncode=1)) is None
    assert clients_for_account("echo@dimagi-ai.com",
                               runner=_auth_accounts_runner([])) == []
