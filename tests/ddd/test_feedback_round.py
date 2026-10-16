"""scripts/ddd/feedback_round.py — HTTP mocked, no network."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts.ddd import feedback_round as fr

ROUND = {"number": 1, "state": "closed", "notes": [
    {"id": 11, "target_ref": "supply-rutf", "anchor_id": "cut:buying", "body": "Too fast"},
    {"id": 12, "target_ref": "supply-rutf", "anchor_id": "cut:buying", "body": "Name the suppliers"},
    {"id": 13, "target_ref": "supply-rutf", "anchor_id": "", "body": "Lead with the forecast"},
], "decisions": [], "staged_edits": []}


def test_cluster_groups_by_narrative_and_anchor():
    c = fr.cluster(ROUND["notes"])
    assert sorted(c) == ["supply-rutf#*", "supply-rutf#cut:buying"]
    assert [n["id"] for n in c["supply-rutf#cut:buying"]] == [11, 12]


def test_missing_dispositions_names_every_unanswered_note():
    d = {"11": {"state": "answered", "note": "Slowed it"}}
    assert fr.missing_dispositions(ROUND, d) == [12, 13]


def test_a_disposition_needs_a_state_and_words():
    with pytest.raises(fr.UnansweredNotes, match="13"):
        fr.missing_or_raise(ROUND, {"11": {"state": "answered", "note": "x"},
                                    "12": {"state": "declined", "note": "y"},
                                    "13": {"state": "answered", "note": ""}})


def test_apply_writes_nothing_when_a_note_is_unanswered():
    calls = []
    with patch.object(fr, "read", return_value=ROUND), patch.object(fr, "_request",
                                                                     side_effect=lambda *a, **k: calls.append(a)):
        with pytest.raises(fr.UnansweredNotes):
            fr.apply("supply", 1, {"11": {"state": "answered", "note": "x"}}, version=4, run_id="r1")
    assert calls == []


def test_apply_resolves_records_and_consumes_in_order():
    calls = []
    disp = {str(n["id"]): {"state": "answered", "note": "done"} for n in ROUND["notes"]}
    with patch.object(fr, "read", return_value=ROUND), \
         patch.object(fr, "_request",
                      side_effect=lambda m, p, b=None, **k: calls.append((m, p, b)) or {"number": 2}), \
         patch.object(fr, "_record_on_run_doc") as rec:
        out = fr.apply("supply", 1, disp, version=4, run_id="r1")
    assert [c[1] for c in calls[:3]] == ["/api/feedback/11/resolve", "/api/feedback/12/resolve",
                                         "/api/feedback/13/resolve"]
    assert calls[0][2] == {"state": "answered", "note": "done", "resolved_in_version": 4}
    assert calls[-1][1] == "/api/storyboards/supply/rounds/1/consume"
    rec.assert_called_once_with("r1", slug="supply", number=1, feedback_ids=[11, 12, 13])
    assert out["next_round"] == 2


def _all_answered(state="answered"):
    return {str(n["id"]): {"state": state, "note": "done"} for n in ROUND["notes"]}


def test_apply_makes_no_http_writes_for_an_unknown_run():
    calls = []
    with patch.object(fr, "read", return_value=ROUND), \
         patch.object(fr, "_request", side_effect=lambda *a, **k: calls.append(a)), \
         patch("scripts.ddd.run_store.pull", return_value=None):
        with pytest.raises(fr.UnknownRun, match="r-typo"):
            fr.apply("supply", 1, _all_answered(), version=4, run_id="r-typo")
    assert calls == []


def test_recording_is_idempotent_per_storyboard_and_round():
    pushed = []
    doc = {"state_version": 3, "state": {"run_id": "r1", "feedback_rounds": [
        {"storyboard": "supply", "round": 1, "feedback_ids": [11]},
        {"storyboard": "supply", "round": 2, "feedback_ids": [99]}]}}
    with patch("scripts.ddd.run_store.pull", return_value=doc), \
         patch("scripts.ddd.run_store.push", side_effect=lambda s, base_version: pushed.append(s)):
        fr._record_on_run_doc("r1", slug="supply", number=1, feedback_ids=[11, 12, 13])
    rounds = pushed[0]["feedback_rounds"]
    assert [(r["round"], r["feedback_ids"]) for r in rounds] == [(2, [99]), (1, [11, 12, 13])]


def test_record_happens_before_the_first_resolve():
    order = []
    with patch.object(fr, "read", return_value=ROUND), \
         patch.object(fr, "_request", side_effect=lambda m, p, b=None, **k: order.append(p) or {"number": 2}), \
         patch.object(fr, "_record_on_run_doc", side_effect=lambda *a, **k: order.append("record")):
        fr.apply("supply", 1, _all_answered(), version=4, run_id="r1")
    assert order[0] == "record"


def test_declined_omits_resolved_in_version():
    calls = []
    with patch.object(fr, "read", return_value=ROUND), \
         patch.object(fr, "_request", side_effect=lambda m, p, b=None, **k: calls.append(b) or {"number": 2}), \
         patch.object(fr, "_record_on_run_doc"):
        fr.apply("supply", 1, _all_answered("declined"), version=4, run_id="r1")
    assert calls[0] == {"state": "declined", "note": "done"}


def _capture_urls(monkeypatch):
    urls = []
    monkeypatch.setattr(fr, "_json_request", lambda m, u, t, b=None: urls.append((m, u)) or {})
    monkeypatch.setattr(fr, "_resolve_base_url", lambda b: "http://x")
    monkeypatch.setattr(fr, "_resolve_token", lambda t: "tok")
    return urls


def test_requests_are_pinned_to_the_workspace(monkeypatch):
    urls = _capture_urls(monkeypatch)
    for m, p in [("POST", "/api/storyboards/"), ("GET", "/api/storyboards/supply/rounds/1"),
                 ("POST", "/api/feedback/11/resolve"), ("POST", "/api/storyboards/supply/rounds/1/consume")]:
        fr._request(m, p, {} if m == "POST" else None, workspace="connect")
    assert [u for _, u in urls] == [
        "http://x/api/w/connect/storyboards/", "http://x/api/w/connect/storyboards/supply/rounds/1",
        "http://x/api/w/connect/feedback/11/resolve", "http://x/api/w/connect/storyboards/supply/rounds/1/consume"]


def test_cli_prints_http_error_detail_without_traceback(capsys):
    import io, urllib.error
    err = urllib.error.HTTPError("u", 422, "Unprocessable", {}, io.BytesIO(b'{"detail": "no workspace"}'))
    with patch.object(fr, "read", side_effect=err):
        assert fr._main(["read", "supply", "1"]) == 1
    assert "422 no workspace" in capsys.readouterr().err
