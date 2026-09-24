"""`canopy gdoc email-blocks` — anchor planning, request arithmetic, and the gog I/O."""
import json
import subprocess

import pytest

from orchestrator.gdoc_email_blocks import (
    EmailBlockError,
    GogDocs,
    fill_requests,
    find_anchors,
    insert_email_blocks,
    plan_blocks,
    style_requests,
)


def para(start, text, style="NORMAL_TEXT"):
    return {"startIndex": start, "endIndex": start + len(text),
            "paragraph": {"elements": [{"textRun": {"content": text}}],
                          "paragraphStyle": {"namedStyleType": style}}}


def email_table(base):
    """5x2 table; cell (r,c)'s paragraph starts at base + r*10 + c*5 and is empty (1 char)."""
    rows = [{"tableCells": [{"content": [{"startIndex": base + r * 10 + c * 5,
                                          "endIndex": base + r * 10 + c * 5 + 1}]} for c in range(2)]}
            for r in range(5)]
    return {"startIndex": base, "endIndex": base + 60, "table": {"tableRows": rows}}


def test_find_anchors_last_to_first_and_ignores_inline_tokens():
    body = [para(1, "Intro\n"), para(7, "@@EMAIL_a@@\n"),
            para(20, "see @@EMAIL_x@@ inline\n"), para(45, "@@EMAIL_b-2@@\n")]
    anchors = find_anchors(body)
    assert [a.key for a in anchors] == ["b-2", "a"]
    assert anchors[1].token == "@@EMAIL_a@@" and anchors[1].start_index == 7


def test_plan_orders_last_first_and_reports_unused():
    anchors = find_anchors([para(1, "@@EMAIL_a@@\n"), para(20, "@@EMAIL_b@@\n")])
    plan = plan_blocks(anchors, [{"anchor": "a", "subject": "A"}])
    assert [a.key for a, _ in plan.steps] == ["a"]
    assert plan.unused_anchors == ["@@EMAIL_b@@"]
    both = plan_blocks(anchors, [{"anchor": "a"}, {"anchor": "b"}])
    assert [a.key for a, _ in both.steps] == ["b", "a"]


def test_plan_refuses_every_problem_at_once():
    anchors = find_anchors([para(1, "@@EMAIL_a@@\n")])
    with pytest.raises(EmailBlockError) as e:
        plan_blocks(anchors, [{"anchor": "zzz"}, {"anchor": "a"}, {"anchor": "a"},
                              {"anchor": "bad key"}, {"anchor": "a2", "subj": "typo"}])
    msg = str(e.value)
    assert "@@EMAIL_zzz@@" in msg and "requested twice" in msg and "must match" in msg
    assert "unknown field" in msg


def test_plan_refuses_heading_anchor_and_duplicate_anchor():
    with pytest.raises(EmailBlockError, match="HEADING_2"):
        plan_blocks(find_anchors([para(1, "@@EMAIL_h@@\n", "HEADING_2")]), [{"anchor": "h"}])
    dup = find_anchors([para(1, "@@EMAIL_a@@\n"), para(20, "@@EMAIL_a@@\n")])
    with pytest.raises(EmailBlockError, match="appears 2 times"):
        plan_blocks(dup, [{"anchor": "a"}])


def test_labels_inserted_last_in_reverse_row_order():
    reqs = style_requests(100, email_table(100)["table"])
    inserts = [(r["insertText"]["location"]["index"], r["insertText"]["text"]) for r in reqs if "insertText" in r]
    assert inserts == [(130, "Subject"), (120, "Bcc"), (110, "Cc"), (100, "To")]
    assert next(i for i, r in enumerate(reqs) if "insertText" in r) == len(reqs) - 4


def test_fill_highest_index_first_and_skips_empty():
    reqs = fill_requests(email_table(100)["table"], {"to": "x@y.org", "subject": "S", "body": "B"})
    inserts = [(r["insertText"]["location"]["index"], r["insertText"]["text"]) for r in reqs if "insertText" in r]
    assert inserts == [(140, "B"), (135, "S"), (105, "x@y.org")]


class FakeDocs:
    def __init__(self, first, later):
        self.first, self.later, self.reads, self.calls = first, later, 0, []

    def get(self, _doc_id):
        self.reads += 1
        return {"body": {"content": self.first if self.reads == 1 else self.later}}

    def batch_update(self, _doc_id, reqs):
        self.calls.append(reqs)


def test_bad_request_writes_nothing():
    docs = FakeDocs([para(1, "@@EMAIL_a@@\n")], [])
    with pytest.raises(EmailBlockError):
        insert_email_blocks(docs, "doc", [{"anchor": "missing"}])
    assert docs.calls == []


def test_insert_style_fill_then_delete_tokens():
    docs = FakeDocs([para(1, "@@EMAIL_a@@\n")], [para(1, "\n"), email_table(2)])
    res = insert_email_blocks(docs, "doc", [{"anchor": "a", "subject": "Hi"}])
    assert res == {"documentId": "doc", "inserted": ["@@EMAIL_a@@"], "unusedAnchors": []}
    assert list(docs.calls[0][0]) == ["insertTable"]
    assert docs.calls[-1] == [{"replaceAllText": {"containsText": {"text": "@@EMAIL_a@@", "matchCase": True},
                                                  "replaceText": ""}}]
    assert len(docs.calls) == 4


def test_gog_docs_builds_the_api_call_as_the_agent():
    seen = []

    def runner(cmd, **_kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": 1}), stderr="")

    g = GogDocs("eva@dimagi-ai.com", "canopy", runner=runner)
    g.get("D")
    g.batch_update("D", [{"insertText": {}}])
    read, write = seen
    assert read[:6] == ["gog", "api", "call", "docs", "v1", "documents.get"]
    assert "--allow-write" not in read
    assert write[5] == "documents.batchUpdate" and "--allow-write" in write and "--force" in write
    assert write[write.index("--account") + 1] == "eva@dimagi-ai.com"
    assert json.loads(write[write.index("--body") + 1]) == {"requests": [{"insertText": {}}]}


def test_gog_failure_names_the_account():
    def runner(cmd, **_kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="403 PERMISSION_DENIED")

    with pytest.raises(EmailBlockError, match="eva@dimagi-ai.com.*403"):
        GogDocs("eva@dimagi-ai.com", "canopy", runner=runner).get("D")
