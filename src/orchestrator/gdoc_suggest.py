r"""Edit someone else's Google Doc as TRACKED SUGGESTIONS — backs `canopy gdoc suggest`.

When a human asks an agent to edit their doc "in edit mode" / "with track changes", they
mean Suggesting mode: every change visible, accepted or rejected one by one. A direct write
(`gog docs find-replace`, `insert`, `delete`) lands silently and its only trace is version
history. Origin: eva, 2026-10-02, eight direct edits to a teammate's concept note that had
been asked for as suggestions, then undone by hand.

The Docs API does this natively: `documents.batchUpdate` with
`"writeControl": {"writeMode": "SUGGEST"}` turns every request into a suggestion. It is a
Workspace Developer Preview feature; the Dimagi Workspace and the Cloud project behind the
fleet's gog client were enrolled 2026-10-04 (verified as eva and fleet-wide). Without
enrollment the call fails `400 Unsupported WriteControl mode.` — reported here as exactly that.

Why edits are located by TEXT, not by index. An agent knows "change X to Y", not "delete
3412..3419". This resolves each `find` to a unique span in the doc as it currently reads —
including pending suggestions, which still occupy index space — refuses anything ambiguous,
and sends the whole set as ONE batch, highest index first so earlier edits cannot shift
later ones. It never uses `replaceAllText`, which would silently hit every occurrence.

Why it checks the response. A batch that came back without `createdSuggestionIds` went in
as a DIRECT edit. That is the exact failure this exists to prevent, so it is an error.
"""
from __future__ import annotations

import json
import subprocess
from typing import Callable

GOG_TIMEOUT = 120
PREVIEW_ERROR = "Unsupported WriteControl mode"


class SuggestError(Exception):
    pass


# ---- reading the doc as it currently reads (pure) ----------------------------------------

def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _runs(content: list) -> list[tuple[int, str, bool]]:
    """(startIndex, text, pending_deletion) for every text run, tables and all, in order."""
    out: list[tuple[int, str, bool]] = []
    for el in content or []:
        para = el.get("paragraph")
        if para:
            for pe in para.get("elements") or []:
                tr = pe.get("textRun")
                if tr and "startIndex" in pe:
                    out.append((pe["startIndex"], tr.get("content", ""),
                                bool(tr.get("suggestedDeletionIds"))))
        table = el.get("table")
        if table:
            for row in table.get("tableRows") or []:
                for cell in row.get("tableCells") or []:
                    out.extend(_runs(cell.get("content")))
    return out


def text_index(doc: dict) -> tuple[str, list[int]]:
    """The doc's visible text (pending deletions excluded) and, for each character of it,
    the Docs index where it starts. Indices are UTF-16 code units, as the API counts."""
    chars: list[str] = []
    idx: list[int] = []
    for start, text, deleted in _runs((doc.get("body") or {}).get("content")):
        if deleted:
            continue
        pos = start
        for ch in text:
            chars.append(ch)
            idx.append(pos)
            pos += _utf16_len(ch)
    return "".join(chars), idx


def locate(text: str, idx: list[int], find: str) -> tuple[int, int]:
    """The unique [start, end) Docs range of `find`. Raises on zero or several hits."""
    if not find:
        raise SuggestError("an edit has an empty `find`")
    hits = []
    at = text.find(find)
    while at != -1:
        hits.append(at)
        at = text.find(find, at + 1)
    if not hits:
        raise SuggestError(f"not found in the doc: {find[:80]!r}")
    if len(hits) > 1:
        raise SuggestError(f"{len(hits)} matches for {find[:80]!r} — lengthen it until it is "
                           "unique (a suggestion must land in exactly one place)")
    first, last = hits[0], hits[0] + len(find) - 1
    return idx[first], idx[last] + _utf16_len(text[last])


def plan(doc: dict, edits: list[dict]) -> list[dict]:
    """Turn text edits into one ordered batch of Docs requests.

    Each edit is {"find": <unique text>} plus exactly one of:
      "replace": <new text>   (empty string = delete)
      "insert_after": <text>
      "insert_before": <text>
      "delete": true
    """
    text, idx = text_index(doc)
    spans = []
    for n, e in enumerate(edits):
        ops = [k for k in ("replace", "insert_after", "insert_before", "delete") if k in e]
        if len(ops) != 1:
            raise SuggestError(f"edit {n + 1} needs exactly one of replace / insert_after / "
                               f"insert_before / delete (got {ops or 'none'})")
        start, end = locate(text, idx, e.get("find", ""))
        spans.append((start, end, ops[0], e[ops[0]], n))
    spans.sort(key=lambda s: s[0])
    for a, b in zip(spans, spans[1:]):
        if b[0] < a[1]:
            raise SuggestError(f"edits {a[4] + 1} and {b[4] + 1} overlap — merge them into one")
    requests: list[dict] = []
    for start, end, op, val, _ in sorted(spans, key=lambda s: s[0], reverse=True):
        if op in ("replace", "delete"):
            requests.append({"deleteContentRange": {"range": {"startIndex": start,
                                                              "endIndex": end}}})
            if op == "replace" and val:
                requests.append({"insertText": {"location": {"index": start}, "text": val}})
        elif op == "insert_after":
            requests.append({"insertText": {"location": {"index": end}, "text": val}})
        else:
            requests.append({"insertText": {"location": {"index": start}, "text": val}})
    return requests


def suggestion_ids(response: dict) -> list[str]:
    """Every suggestion the batch created or extended, de-duplicated in order.

    An insert right next to a delete in the same batch does not get its own suggestion: Docs
    folds it into the delete's and answers `updatedSummarySuggestionIds` instead of
    `createdSuggestionIds` (measured 2026-10-04 — every "replace" does this)."""
    ids: list[str] = []
    for r in response.get("suggestionResponses") or []:
        for k in ("createdSuggestionIds", "updatedSummarySuggestionIds"):
            for i in r.get(k) or []:
                if i not in ids:
                    ids.append(i)
    return ids


def unsuggested_requests(response: dict, n_requests: int) -> int:
    """How many requests came back with no suggestion at all — i.e. went in as direct edits."""
    resp = response.get("suggestionResponses") or []
    missing = max(0, n_requests - len(resp))
    for r in resp:
        if not (r.get("createdSuggestionIds") or r.get("updatedSummarySuggestionIds")):
            missing += 1
    return missing


def plain_text(doc: dict) -> str:
    return "".join(t for _, t, _ in _runs((doc.get("body") or {}).get("content")))


# ---- I/O ---------------------------------------------------------------------------------

def _gog(account: str, client: str, method: str, params: dict, body: dict | None,
         runner: Callable) -> dict:
    cmd = ["gog", "api", "call", "docs", "v1", method, "--params", json.dumps(params),
           "--account", account, "--client", client, "--json"]
    if body is not None:
        cmd += ["--body", json.dumps(body), "--allow-write", "--force"]
    try:
        p = runner(cmd, capture_output=True, text=True, timeout=GOG_TIMEOUT)
    except FileNotFoundError:
        raise SuggestError("gog CLI not found on PATH (brew install gogcli)")
    except subprocess.TimeoutExpired:
        raise SuggestError(f"gog api call docs {method} timed out after {GOG_TIMEOUT}s")
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        if PREVIEW_ERROR in err:
            raise SuggestError(
                "the Docs API refused SUGGEST mode (400 Unsupported WriteControl mode): the "
                "Workspace account or the Cloud project behind this gog client is not enrolled "
                "in the Google Workspace Developer Preview program. Nothing was written. Do NOT "
                "fall back to direct edits on someone else's doc — ask.")
        raise SuggestError(f"gog api call docs {method} failed as {account}: {err[:500]}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        raise SuggestError(f"gog api call docs {method} returned non-JSON: {p.stdout[:300]}")


def _base_text(account, client, doc_id, runner) -> str:
    """The doc with every pending suggestion rejected — what a direct edit would change."""
    return plain_text(_gog(account, client, "documents.get",
                           {"documentId": doc_id,
                            "suggestionsViewMode": "PREVIEW_WITHOUT_SUGGESTIONS"},
                           None, runner))


def suggest(*, account: str, client: str, doc_id: str, edits: list[dict],
            dry_run: bool = False, runner: Callable = subprocess.run) -> dict:
    """Plan the edits against the doc as it reads now, then send them as suggestions.

    Two independent proofs nothing went in directly: every request is answered with a
    suggestion id, AND the doc with all suggestions rejected reads the same before and after."""
    doc = _gog(account, client, "documents.get",
               {"documentId": doc_id, "suggestionsViewMode": "SUGGESTIONS_INLINE"}, None, runner)
    requests = plan(doc, edits)
    if dry_run:
        return {"documentId": doc_id, "dry_run": True, "requests": requests}
    before = _base_text(account, client, doc_id, runner)
    response = _gog(account, client, "documents.batchUpdate", {"documentId": doc_id},
                    {"requests": requests, "writeControl": {"writeMode": "SUGGEST"}}, runner)
    missing = unsuggested_requests(response, len(requests))
    after = _base_text(account, client, doc_id, runner)
    if missing or after != before:
        why = (f"{missing} of {len(requests)} request(s) came back with no suggestion id"
               if missing else "the doc's underlying text changed")
        raise SuggestError(
            f"{why} — some of the batch landed as DIRECT edits. Open the doc's version history "
            f"and check before doing anything else. Response: {json.dumps(response)[:400]}")
    ids = suggestion_ids(response)
    return {"documentId": doc_id, "edits": len(edits), "requests": len(requests),
            "suggestionIds": ids,
            "url": f"https://docs.google.com/document/d/{doc_id}/edit"}
