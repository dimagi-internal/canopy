"""Email draft blocks in a Google Doc — `canopy gdoc email-blocks`.

An email block is the To / Cc / Bcc / Subject / Body table Docs renders with a Gmail icon
in the right margin; a human clicks the icon and gets a pre-filled Gmail draft. It is how
the fleet delivers emails a person will send (agent-core/email-drafts.md).

The Docs API has no request that inserts a building block by name. This builds the same
5x2 table with the native block's styling constants — originally chrome-sales'
`docs_insert_email_block`, then ACE's `lib/docs-email-block.ts` (ace#2477), ported here so
every agent gets it under its OWN identity. That removes the step Eva's `email-macros` had
to take: sharing each eva@-owned doc to chrome-sales' service account as writer before any
block could go in.

The hard part is index arithmetic (a table insert shifts every later index; cells are
addressed by absolute index; a block inserted at a heading boundary inherits the heading's
18pt style). Callers never do it: they write an anchor paragraph `@@EMAIL_<key>@@` where a
block goes, and this module turns anchors into request batches — pure functions, tested
against synthetic documents. The I/O is `gog api call docs v1 …` as the agent's account.

ACE keeps a TypeScript twin (`docs_insert_email_blocks`, ace-gdrive) because ACE's run docs
are owned by its Drive service account — ace@ itself gets a 403 on them — and its
marketplace panel turn has no shell to run this command from. Change the two together.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

ANCHOR_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ANCHOR_PARA_RE = re.compile(r"^@@EMAIL_([A-Za-z0-9_-]+)@@$")
EMAIL_LABELS = ("To", "Cc", "Bcc", "Subject")
FIELDS = ("to", "cc", "bcc", "subject", "body")

# Styling constants matched from the native @email building block (via chrome-sales).
_LABEL_BG = {"color": {"rgbColor": {"red": 0.94509804, "green": 0.9529412, "blue": 0.95686275}}}
_BORDER_COLOR = {"color": {"rgbColor": {"red": 0.7411765, "green": 0.75686276, "blue": 0.7764706}}}


def _border(mag: float) -> dict:
    return {"color": _BORDER_COLOR, "width": {"magnitude": mag, "unit": "PT"}, "dashStyle": "SOLID"}


def _pad(mag: float) -> dict:
    return {"magnitude": mag, "unit": "PT"}


class EmailBlockError(Exception):
    """A request the document cannot satisfy — raised before anything is written."""


def anchor_token(key: str) -> str:
    return f"@@EMAIL_{key}@@"


@dataclass
class Anchor:
    key: str
    token: str
    start_index: int
    end_index: int
    named_style: str


def find_anchors(body_content: list[dict]) -> list[Anchor]:
    """Top-level `@@EMAIL_<key>@@` paragraphs, LAST-TO-FIRST (so inserting in this order
    never invalidates an anchor not yet reached). Anchors inside prose are ignored."""
    out: list[Anchor] = []
    for el in body_content:
        para = el.get("paragraph")
        if not para or el.get("startIndex") is None or el.get("endIndex") is None:
            continue
        text = "".join((e.get("textRun") or {}).get("content", "") for e in para.get("elements") or []).strip()
        m = _ANCHOR_PARA_RE.match(text)
        if not m:
            continue
        out.append(Anchor(
            key=m.group(1), token=anchor_token(m.group(1)),
            start_index=el["startIndex"], end_index=el["endIndex"],
            named_style=(para.get("paragraphStyle") or {}).get("namedStyleType") or "NORMAL_TEXT",
        ))
    out.sort(key=lambda a: a.start_index, reverse=True)
    return out


@dataclass
class Plan:
    steps: list[tuple[Anchor, dict]]
    unused_anchors: list[str] = field(default_factory=list)


def plan_blocks(anchors: list[Anchor], blocks: list[dict]) -> Plan:
    """Pair requested blocks with the doc's anchors. Refuses rather than guesses — every
    problem is reported at once, and nothing has been written yet."""
    by_key: dict[str, list[Anchor]] = {}
    for a in anchors:
        by_key.setdefault(a.key, []).append(a)
    problems: list[str] = []
    seen: set[str] = set()
    steps: list[tuple[Anchor, dict]] = []
    for b in blocks:
        key = str(b.get("anchor", ""))
        unknown = sorted(set(b) - set(FIELDS) - {"anchor"})
        if unknown:
            problems.append(f'block "{key}" has unknown field(s) {unknown}; allowed: anchor, {", ".join(FIELDS)}')
            continue
        if not ANCHOR_KEY_RE.match(key):
            problems.append(f'anchor key "{key}" must match {ANCHOR_KEY_RE.pattern}')
            continue
        if key in seen:
            problems.append(f'anchor key "{key}" is requested twice')
            continue
        seen.add(key)
        found = by_key.get(key, [])
        if not found:
            problems.append(f"no paragraph containing only {anchor_token(key)} in the document")
            continue
        if len(found) > 1:
            problems.append(f"{anchor_token(key)} appears {len(found)} times; each anchor must be unique")
            continue
        if found[0].named_style != "NORMAL_TEXT":
            problems.append(
                f"{anchor_token(key)} is styled {found[0].named_style}; put it on its own Normal-text "
                "line (blank line either side), or the block inherits the heading font")
            continue
        steps.append((found[0], {f: b[f] for f in FIELDS if b.get(f)}))
    if problems:
        raise EmailBlockError("email blocks refused:\n- " + "\n- ".join(problems))
    steps.sort(key=lambda s: s[0].start_index, reverse=True)
    return Plan(steps=steps, unused_anchors=[a.token for a in anchors if a.key not in seen])


def insert_table_request(index: int) -> dict:
    return {"insertTable": {"location": {"index": index}, "rows": 5, "columns": 2}}


def find_table_at_or_after(body_content: list[dict], index: int) -> tuple[int, dict]:
    for el in body_content:
        if el.get("table") and (el.get("startIndex") or -1) >= index:
            return el["startIndex"], el["table"]
    raise EmailBlockError(f"no table found at or after index {index} after insertTable")


def _cell_para(table: dict, row: int, col: int) -> dict:
    try:
        return table["tableRows"][row]["tableCells"][col]["content"][0]
    except (KeyError, IndexError):
        raise EmailBlockError(f"email block table has no cell ({row},{col})")


def style_requests(table_start: int, table: dict) -> list[dict]:
    """Pass 2: column width, merged body row, cell styling, then the four labels (last,
    in reverse row order, so earlier requests in the batch keep valid indices)."""
    loc = {"index": table_start}
    reqs: list[dict] = [
        {"updateTableColumnProperties": {
            "tableStartLocation": loc, "columnIndices": [0],
            "tableColumnProperties": {"widthType": "FIXED_WIDTH", "width": {"magnitude": 67.35, "unit": "PT"}},
            "fields": "widthType,width"}},
        {"mergeTableCells": {"tableRange": {
            "tableCellLocation": {"tableStartLocation": loc, "rowIndex": 4, "columnIndex": 0},
            "rowSpan": 1, "columnSpan": 2}}},
    ]
    for row in range(5):
        for col in range(2):
            is_label = col == 0 and row < 4
            is_body = row == 4 and col == 0
            reqs.append({"updateTableCellStyle": {
                "tableRange": {"tableCellLocation": {"tableStartLocation": loc, "rowIndex": row, "columnIndex": col},
                               "rowSpan": 1, "columnSpan": 1},
                "tableCellStyle": {
                    "backgroundColor": _LABEL_BG if is_label else {},
                    "borderTop": _border(1), "borderBottom": _border(1),
                    "borderLeft": _border(0 if col == 0 else 1),
                    "borderRight": _border(1 if (col == 0 and row < 4) else 0),
                    "paddingLeft": _pad(7.2), "paddingRight": _pad(7.2),
                    "paddingTop": _pad(12 if is_body else 7.2), "paddingBottom": _pad(7.2),
                    "contentAlignment": "TOP"},
                "fields": "backgroundColor,borderTop,borderBottom,borderLeft,borderRight,"
                          "paddingLeft,paddingRight,paddingTop,paddingBottom,contentAlignment"}})
            if is_label:
                p = _cell_para(table, row, 0)["startIndex"]
                reqs.append({"updateParagraphStyle": {
                    "range": {"startIndex": p, "endIndex": p + 1},
                    "paragraphStyle": {"alignment": "END", "lineSpacing": 100, "spacingMode": "COLLAPSE_LISTS",
                                       "spaceAbove": {"unit": "PT"}, "spaceBelow": {"unit": "PT"}},
                    "fields": "alignment,lineSpacing,spacingMode,spaceAbove,spaceBelow"}})
    for row in range(len(EMAIL_LABELS) - 1, -1, -1):
        reqs.append({"insertText": {"location": {"index": _cell_para(table, row, 0)["startIndex"]},
                                    "text": EMAIL_LABELS[row]}})
    return reqs


def fill_requests(table: dict, fields: dict) -> list[dict]:
    """Pass 3, against the re-read table: bold 10pt labels (the native block's label size),
    then the values, highest index first."""
    reqs: list[dict] = []
    for row in range(len(EMAIL_LABELS)):
        p = _cell_para(table, row, 0)
        start, end = p["startIndex"], p["endIndex"] - 1  # exclude the cell's trailing newline
        if end > start:
            reqs.append({"updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "textStyle": {"bold": True, "fontSize": {"magnitude": 10, "unit": "PT"}},
                "fields": "bold,fontSize"}})
    for row, col, name in ((4, 0, "body"), (3, 1, "subject"), (2, 1, "bcc"), (1, 1, "cc"), (0, 1, "to")):
        text = fields.get(name)
        if text:
            reqs.append({"insertText": {"location": {"index": _cell_para(table, row, col)["startIndex"]},
                                        "text": text}})
    return reqs


def delete_anchor_requests(tokens: list[str]) -> list[dict]:
    return [{"replaceAllText": {"containsText": {"text": t, "matchCase": True}, "replaceText": ""}}
            for t in tokens]


# --------------------------------------------------------------------------------------
# I/O — `gog api call docs v1 …` as the agent
# --------------------------------------------------------------------------------------

GOG_TIMEOUT = 120


class GogDocs:
    """documents.get / documents.batchUpdate through gog, as `account` via `client`."""

    def __init__(self, account: str, client: str, runner: Callable = subprocess.run):
        self.account, self.client, self.runner = account, client, runner

    def _call(self, method: str, doc_id: str, body: dict | None = None) -> dict:
        cmd = ["gog", "api", "call", "docs", "v1", method,
               "--params", json.dumps({"documentId": doc_id}),
               "--account", self.account, "--client", self.client, "--json"]
        if body is not None:
            cmd += ["--body", json.dumps(body), "--allow-write", "--force"]
        try:
            p = self.runner(cmd, capture_output=True, text=True, timeout=GOG_TIMEOUT)
        except FileNotFoundError:
            raise EmailBlockError("gog CLI not found on PATH (brew install steipete/tap/gogcli)")
        except subprocess.TimeoutExpired:
            raise EmailBlockError(f"gog api call docs {method} timed out after {GOG_TIMEOUT}s")
        if p.returncode != 0:
            raise EmailBlockError(
                f"gog api call docs {method} failed as {self.account}: "
                f"{(p.stderr or p.stdout or '').strip()[:500]}")
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError:
            raise EmailBlockError(f"gog api call docs {method} returned non-JSON: {p.stdout[:300]}")

    def get(self, doc_id: str) -> dict:
        return self._call("documents.get", doc_id)

    def batch_update(self, doc_id: str, requests: list[dict]) -> dict:
        return self._call("documents.batchUpdate", doc_id, {"requests": requests})


def insert_email_blocks(docs, doc_id: str, blocks: list[dict]) -> dict:
    """Replace each requested anchor with a filled block (last first), then delete the
    consumed tokens. `docs` has .get(doc_id) and .batch_update(doc_id, requests)."""
    content = lambda d: (d.get("body") or {}).get("content") or []  # noqa: E731
    plan = plan_blocks(find_anchors(content(docs.get(doc_id))), blocks)
    inserted: list[str] = []
    for anchor, fields in plan.steps:
        docs.batch_update(doc_id, [insert_table_request(anchor.start_index)])
        start, table = find_table_at_or_after(content(docs.get(doc_id)), anchor.start_index)
        docs.batch_update(doc_id, style_requests(start, table))
        _, table = find_table_at_or_after(content(docs.get(doc_id)), anchor.start_index)
        fill = fill_requests(table, fields)
        if fill:
            docs.batch_update(doc_id, fill)
        inserted.append(anchor.token)
    if plan.steps:
        docs.batch_update(doc_id, delete_anchor_requests([a.token for a, _ in plan.steps]))
    return {"documentId": doc_id, "inserted": list(reversed(inserted)), "unusedAnchors": plan.unused_anchors}
