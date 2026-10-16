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
