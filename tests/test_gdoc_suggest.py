"""Pins `canopy gdoc suggest` (gdoc_suggest.py): text edits → one SUGGEST-mode batch."""
from __future__ import annotations

import json

import pytest

from orchestrator.gdoc_suggest import (SuggestError, plan, suggest, suggestion_ids,
                                       text_index, unsuggested_requests)


def _doc(*runs):
    """runs: (startIndex, text[, pending_deletion])."""
    els = []
    for r in runs:
        tr = {"content": r[1]}
        if len(r) > 2 and r[2]:
            tr["suggestedDeletionIds"] = ["s.1"]
        els.append({"startIndex": r[0], "textRun": tr})
    return {"body": {"content": [{"paragraph": {"elements": els}}]}}


DOC = _doc((1, "The quick brown fox jumps.\n"))


def test_replace_is_a_delete_then_an_insert_at_the_same_place():
    reqs = plan(DOC, [{"find": "brown", "replace": "red"}])
    assert reqs == [
        {"deleteContentRange": {"range": {"startIndex": 11, "endIndex": 16}}},
        {"insertText": {"location": {"index": 11}, "text": "red"}},
    ]


def test_insert_after_before_and_delete():
    reqs = plan(DOC, [{"find": "quick", "insert_after": " sly"},
                      {"find": "The", "insert_before": ">> "},
                      {"find": " jumps", "delete": True}])
    # highest index first, so no edit shifts another
    starts = [r.get("insertText", {}).get("location", {}).get("index")
              or r["deleteContentRange"]["range"]["startIndex"] for r in reqs]
    assert starts == sorted(starts, reverse=True)
    assert {"insertText": {"location": {"index": 10}, "text": " sly"}} in reqs
    assert {"insertText": {"location": {"index": 1}, "text": ">> "}} in reqs


def test_ambiguous_or_missing_text_is_refused_not_guessed():
    with pytest.raises(SuggestError, match="2 matches"):
        plan(_doc((1, "a cat and a cat\n")), [{"find": "cat", "replace": "dog"}])
    with pytest.raises(SuggestError, match="not found"):
        plan(DOC, [{"find": "zebra", "replace": "x"}])


def test_overlapping_edits_are_refused():
    with pytest.raises(SuggestError, match="overlap"):
        plan(DOC, [{"find": "quick brown", "replace": "x"}, {"find": "brown fox", "delete": True}])


def test_each_edit_needs_exactly_one_operation():
    with pytest.raises(SuggestError, match="exactly one"):
        plan(DOC, [{"find": "fox", "replace": "x", "delete": True}])


def test_pending_deletions_are_skipped_but_keep_their_index_space():
    """A pending suggestion still occupies indices; text already suggested for deletion is
    not what the reader sees, so it must not be matched."""
    doc = _doc((1, "keep "), (6, "gone ", True), (11, "tail\n"))
    text, idx = text_index(doc)
    assert text == "keep tail\n"
    assert plan(doc, [{"find": "tail", "replace": "end"}])[0] == {
        "deleteContentRange": {"range": {"startIndex": 11, "endIndex": 15}}}


def test_indices_count_utf16_units():
    doc = _doc((1, "a😀b\n"))
    assert plan(doc, [{"find": "b", "delete": True}]) == [
        {"deleteContentRange": {"range": {"startIndex": 4, "endIndex": 5}}}]


def test_table_cells_are_searchable():
    doc = {"body": {"content": [{"table": {"tableRows": [{"tableCells": [
        {"content": [{"paragraph": {"elements": [
            {"startIndex": 5, "textRun": {"content": "cell text\n"}}]}}]}]}]}}]}}
    assert plan(doc, [{"find": "text", "replace": "copy"}])[0]["deleteContentRange"] == {
        "range": {"startIndex": 10, "endIndex": 14}}


class _Gog:
    def __init__(self, doc, response=None, rc=0, err="", after=None):
        self.doc, self.response, self.rc, self.err, self.calls = doc, response, rc, err, []
        self.after, self.written = after, False

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        method = cmd[5]
        if method == "documents.get":
            out = self.after if (self.written and self.after) else self.doc
            rc = 0
        else:
            self.written = True
            out, rc = self.response, self.rc
        return type("R", (), {"returncode": rc, "stdout": json.dumps(out), "stderr": self.err})()


def test_sends_one_suggest_batch_and_reads_inline_suggestions():
    gog = _Gog(DOC, {"suggestionResponses": [{"createdSuggestionIds": ["s.a"]},
                                             {"createdSuggestionIds": ["s.b"]}]})
    r = suggest(account="a@x", client="c", doc_id="D", edits=[{"find": "brown", "replace": "red"}],
                runner=gog)
    assert r["suggestionIds"] == ["s.a", "s.b"]
    get, base_before, batch, base_after = gog.calls
    assert "SUGGESTIONS_INLINE" in get[get.index("--params") + 1]
    assert "PREVIEW_WITHOUT_SUGGESTIONS" in base_after[base_after.index("--params") + 1]
    body = json.loads(batch[batch.index("--body") + 1])
    assert body["writeControl"] == {"writeMode": "SUGGEST"}


def test_a_response_without_suggestion_ids_is_a_direct_edit_and_an_error():
    gog = _Gog(DOC, {"replies": [{}, {}]})
    with pytest.raises(SuggestError, match="DIRECT"):
        suggest(account="a", client="c", doc_id="D",
                edits=[{"find": "brown", "replace": "red"}], runner=gog)


def test_not_enrolled_says_so_and_forbids_the_fallback():
    gog = _Gog(DOC, rc=1, err="googleapi: Error 400: Unsupported WriteControl mode.")
    with pytest.raises(SuggestError, match="Developer Preview"):
        suggest(account="a", client="c", doc_id="D",
                edits=[{"find": "fox", "delete": True}], runner=gog)


def test_dry_run_reads_but_never_writes():
    gog = _Gog(DOC)
    r = suggest(account="a", client="c", doc_id="D", edits=[{"find": "fox", "delete": True}],
                dry_run=True, runner=gog)
    assert r["dry_run"] and len(gog.calls) == 1


def test_suggestion_ids_flattens_responses():
    assert suggestion_ids({"suggestionResponses": [{"createdSuggestionIds": ["a"]}, {}]}) == ["a"]


def test_a_folded_insert_counts_as_suggested():
    """Measured live: the insert half of a replace is folded into the delete's suggestion and
    answered with updatedSummarySuggestionIds. That is a suggestion, not a direct edit."""
    resp = {"suggestionResponses": [{"createdSuggestionIds": ["s.1"]},
                                    {"updatedSummarySuggestionIds": ["s.1"]}]}
    assert unsuggested_requests(resp, 2) == 0
    assert suggestion_ids(resp) == ["s.1"]
    gog = _Gog(DOC, resp)
    assert suggest(account="a", client="c", doc_id="D",
                   edits=[{"find": "brown", "replace": "red"}], runner=gog)["suggestionIds"]


def test_a_changed_base_text_is_a_direct_edit_even_with_ids():
    resp = {"suggestionResponses": [{"createdSuggestionIds": ["s.1"]}]}
    gog = _Gog(DOC, resp, after=_doc((1, "The quick fox jumps.\n")))
    with pytest.raises(SuggestError, match="underlying text changed"):
        suggest(account="a", client="c", doc_id="D", edits=[{"find": "fox", "delete": True}],
                runner=gog)
