"""Tests for the pre-send review rail (agent-core: review-receipt).

The rail exists because the pre-send review is an invariant that fails under load, and
prose can't hold it: an agent reviews draft v1, revises twice as new findings land, and
sends v3 unreviewed — each revision feeling like "improving reviewed work" rather than a
new draft needing review. (Eva, 2026-07-15: the re-review caught a named shortlist target
missing from the body entirely.) Keying the receipt to the BODY's fingerprint is what
makes that specific failure impossible: revise the body, the fingerprint moves, the stale
receipt no longer matches.
"""
import json

import pytest

from orchestrator import review_receipt as rr
from orchestrator.agent_email import AgentEmailError, EmailIdentity, send


@pytest.fixture(autouse=True)
def receipts_dir(tmp_path, monkeypatch):
    d = tmp_path / "receipts"
    monkeypatch.setenv("CANOPY_REVIEW_RECEIPTS_DIR", str(d))
    return d


IDENT = EmailIdentity(slug="eva", account="eva@dimagi-ai.com", client="canopy")


# --- fingerprint -----------------------------------------------------------------------

def test_fingerprint_is_stable_and_body_specific():
    assert rr.fingerprint("hello world") == rr.fingerprint("hello world")
    assert rr.fingerprint("hello world") != rr.fingerprint("hello world!")


def test_fingerprint_follows_send_path_normalization_not_layout():
    """The fingerprint must track what GOES OUT, not how the draft file is laid out.

    send() renders through normalize(), which rejoins hard-wrapped lines into one line per
    paragraph and collapses blank-line runs. So re-wrapping a body file must NOT invalidate
    a review (the rendered mail is identical), while changing a word must.
    """
    # re-wrapping a paragraph: same rendered output, same receipt
    assert rr.fingerprint("hello\nworld") == rr.fingerprint("hello world")
    # collapsing blank-line runs: same rendered output, same receipt
    assert rr.fingerprint("a\n\n\n\nb") == rr.fingerprint("a\n\nb")
    # changing content: different mail, receipt must not carry over
    assert rr.fingerprint("a\n\nb") != rr.fingerprint("a\n\nc")


# --- record / lookup -------------------------------------------------------------------

def test_record_then_lookup_roundtrips(receipts_dir):
    body = "Hi — draft one.\n\nRegards"
    p = rr.record("eva", body, caught=["named target missing"], verdict="fixed")
    assert p.exists()
    got = rr.lookup("eva", body)
    assert got["verdict"] == "fixed"
    assert got["caught"] == ["named target missing"]
    assert got["slug"] == "eva"
    assert got["fingerprint"] == rr.fingerprint(body)


def test_lookup_is_none_for_unreviewed_body():
    assert rr.lookup("eva", "never reviewed") is None


def test_receipt_is_scoped_per_agent():
    """One agent's review must never satisfy another's send — identity isolation."""
    body = "shared text"
    rr.record("eva", body, caught=[], verdict="clean")
    assert rr.lookup("eva", body) is not None
    assert rr.lookup("hal", body) is None


def test_revising_the_body_invalidates_the_receipt():
    """THE regression this rail exists for: review v1, revise, send v2."""
    v1 = "The honest shortlist is two: Rethink Priorities."
    rr.record("eva", v1, caught=[], verdict="clean")
    v2 = v1 + "\n\nThe second is Happier Lives Institute."
    assert rr.lookup("eva", v1) is not None
    assert rr.lookup("eva", v2) is None


# --- the rail in send() ----------------------------------------------------------------

def _fail_runner(*a, **k):  # pragma: no cover - must never be reached
    raise AssertionError("gog was invoked despite a missing review receipt")


def test_send_is_blocked_without_a_receipt():
    with pytest.raises(AgentEmailError) as e:
        send(IDENT, to="a@b.c", subject="s", body_text="unreviewed body",
             runner=_fail_runner)
    msg = str(e.value)
    assert "review" in msg.lower()
    # A rail names the right path (deny-rails philosophy), not just the wrong one.
    assert "canopy email review-receipt" in msg


def test_send_proceeds_once_the_receipt_exists():
    body = "reviewed body"
    rr.record("eva", body, caught=[], verdict="clean")
    calls = []

    def runner(cmd, **k):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": '{"id":"m1","threadId":"t1"}',
                              "stderr": ""})()

    out = send(IDENT, to="a@b.c", subject="s", body_text=body, runner=runner)
    assert out["message_id"] == "m1"
    assert calls, "expected gog to be invoked"


def test_dry_run_never_needs_a_receipt():
    """Dry-run is HOW an agent iterates and verifies recipients — gating it would make the
    rail actively harmful."""
    out = send(IDENT, to="a@b.c", subject="s", body_text="unreviewed", dry_run=True,
               runner=_fail_runner)
    assert out["dry_run"] is True


def test_receipt_matches_the_body_as_the_send_path_normalizes_it():
    """record() and send() must agree on the fingerprint after normalization, or the rail
    blocks a body that WAS reviewed — the failure that would make agents rip it out."""
    body = "line one\n\n\n\nline two   "
    rr.record("eva", body, caught=[], verdict="clean")

    def runner(cmd, **k):
        return type("R", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()

    send(IDENT, to="a@b.c", subject="s", body_text=body, runner=runner)  # must not raise


def test_error_names_the_fingerprint_so_the_agent_can_act():
    with pytest.raises(AgentEmailError) as e:
        send(IDENT, to="a@b.c", subject="s", body_text="xyz", runner=_fail_runner)
    assert rr.fingerprint("xyz")[:12] in str(e.value)


# --- the CLI the rail's own error message tells the agent to run -------------------------

def test_cli_review_receipt_unblocks_the_send(tmp_path, monkeypatch):
    """End-to-end through the documented recovery path: blocked -> record -> send.

    If the command named in rail_message() does not actually unblock the send, the rail
    is a brick wall instead of a rail.
    """
    from click.testing import CliRunner
    from orchestrator.agent_email import email_group

    body = tmp_path / "body.txt"
    body.write_text("Hi — the draft.\n\nEva\n")

    res = CliRunner().invoke(email_group, [
        "review-receipt", "--account", "eva@dimagi-ai.com", "--client", "canopy",
        "--body-file", str(body), "--caught", "shortlist target missing", "--verdict", "fixed",
    ])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["recorded"] is True
    assert out["caught"] == ["shortlist target missing"]

    # the recorded receipt satisfies the rail for that same body
    got = rr.lookup(out["slug"], body.read_text())
    assert got is not None and got["verdict"] == "fixed"


def test_cli_receipt_does_not_unblock_a_different_body(tmp_path):
    from click.testing import CliRunner
    from orchestrator.agent_email import email_group

    body = tmp_path / "body.txt"
    body.write_text("version one\n")
    res = CliRunner().invoke(email_group, [
        "review-receipt", "--account", "eva@dimagi-ai.com", "--client", "canopy",
        "--body-file", str(body),
    ])
    assert res.exit_code == 0, res.output
    slug = json.loads(res.output)["slug"]
    assert rr.lookup(slug, "version one\n") is not None
    assert rr.lookup(slug, "version two\n") is None


# --- commitment scan (§6 as a gate) -----------------------------------------------------
# The rule ("name the executable mechanism or cut it") was already written; what failed was
# applying it to EVERY instance. On 2026-07-23 a review caught three body defects, recorded
# clean, and still shipped "Happy to walk anyone through it live" — a real-time human
# session an agent cannot hold. These lock the gate that makes that impossible.

THE_MISS = (
    "Hi all,\n\nThe demo is live.\n\n"
    "Happy to walk anyone through it live, adjust the narrative, or fold in the real app.\n\n"
    "— ACE\n"
)


def test_scan_flags_the_offer_and_the_realtime_human_phrase():
    kinds = {h["kind"] for h in rr.scan_commitments(THE_MISS)}
    assert "real-time human" in kinds
    assert "offer" in kinds


def test_record_refuses_while_a_commitment_is_unruled():
    with pytest.raises(AgentEmailError) as e:
        rr.record("eva", THE_MISS)
    msg = str(e.value)
    assert "REFUSED" in msg
    assert "walk anyone through" in msg
    # and it tells you how to clear it
    assert "grounded:" in msg and "cut" in msg


def test_ruling_every_hit_lets_the_receipt_issue():
    p = rr.record("eva", THE_MISS,
                  commitments=["walk anyone through=cut", "happy to=cut"])
    assert p.exists()
    assert rr.lookup("eva", THE_MISS) is not None


def test_a_body_with_no_commitments_is_unaffected():
    body = "Hi all,\n\nThe run finished; the report is attached.\n\n— ACE\n"
    assert rr.scan_commitments(body) == []
    assert rr.record("eva", body).exists()


def test_grounded_ruling_is_recorded_on_the_receipt():
    rr.record("eva", THE_MISS,
              commitments=["walk anyone through=cut",
                           "happy to=grounded:reply on the thread"])
    got = rr.lookup("eva", THE_MISS)
    assert any("grounded:" in c for c in got["commitments"])


def test_send_stays_blocked_when_the_receipt_was_refused():
    # No receipt was ever issued (record refused), so the send rail still fires.
    with pytest.raises(AgentEmailError):
        rr.require("eva", THE_MISS)
