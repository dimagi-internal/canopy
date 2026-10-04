#!/usr/bin/env python3
"""Fleet rail: a Google Doc the agent WROTE may not be handed over unreviewed.

Each agent's `hooks/gdoc_gate.py` is a thin loader that runs this file out of the
installed canopy plugin (the same pattern as decide_guard.py), wired twice in the
agent's `.claude/settings.json`:

    PostToolUse (matcher "Bash|^mcp__")  →  hooks/gdoc_gate.py record
    Stop                                 →  hooks/gdoc_gate.py check

WHY IT EXISTS. "It published" is not "it looks right": `--replace` has shipped
all-italic docs and docs emptied to a sentinel, and the converter has shipped literal
`**` and `|---|`. Eva had a checker for all of it and a skill that said to run it
before sharing a link. On 2026-08-28 a turn published three docs, handed over three
links and ran the checker zero times — with the rule in context the whole time. So
the check became a hook (in Eva first; promoted here 2026-10-04 for the fleet).

WHY THE STOP EVENT. The handoff that matters is a link typed into the turn's closing
message, not a tool call — a PreToolUse rail would see nothing.

THE RULE. Two marker trees under $CANOPY_AGENT_HOME/gdoc-gate/, one empty file per
doc id; the mtime is the whole signal:

    published/<docId>   the agent wrote the doc (this hook's `record` mode for raw
                        gog/MCP doc writes; `canopy gdoc publish|email-blocks` mark
                        it themselves)
    passed/<docId>      `canopy gdoc check` (or the review those commands run) passed

A doc id in the closing message is blocked iff `published/` has it and `passed/` is
missing or older — "did you check it after you last wrote it?" A link the agent never
wrote (a teammate's tracker, a doc a counterpart sent) has no `published/` marker and
can never trip this.

IT MUST NEVER WEDGE A SESSION. Each doc is blocked at most once per write (a
`blocked/` marker compared by mtime), `stop_hook_active` is honoured, and every
unexpected condition exits 0. Stdlib only.
"""
import json
import os
import re
import sys

STATE = os.path.join(os.environ.get("CANOPY_AGENT_HOME") or os.path.expanduser("~/.canopy"),
                     "gdoc-gate")

DOC_URL = re.compile(r"docs\.google\.com/document/(?:u/\d+/)?d/([A-Za-z0-9_-]{20,})")

# Raw tool calls that WRITE a Doc. `canopy gdoc publish|email-blocks` are deliberately
# absent: they mark `published` and run the review themselves, and recording them here
# (after the command returns) would stamp `published` AFTER the receipt and re-arm the
# rail on a doc that just passed.
WRITE_BASH = re.compile(
    r"(?:^|[\n;&|(])\s*gog\b[^;&|\n]*\bdocs?\s+"
    r"(?:create|new|insert|delete|find-replace|update|edit|sed|write|clear|format)\b"
    r"|(?:^|[\n;&|(])\s*gog\b[^;&|\n]*\bapi\s+call\s+docs\b[^;&|\n]*batchUpdate"
)
# Doc ids passed bare on the command line (no URL anywhere in the call or its output).
BARE_ID = re.compile(r"\b(?:docs?\s+\S+|documentId\W+)\s*['\"]?([A-Za-z0-9_-]{25,})")

WRITE_MCP = re.compile(
    r"__(?:docs_batch_update|docs_create\w*|docs_copy_template|docs_insert_email_blocks?"
    r"|docs_finalize_\w+|drive_create_doc_from_markdown|drive_create_file|drive_copy_file)$"
)


def _touch(kind, doc_id):
    try:
        d = os.path.join(STATE, kind)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, doc_id), "w", encoding="utf-8") as fh:
            fh.write("")
    except Exception:
        pass


def _mtime(kind, doc_id):
    try:
        return os.path.getmtime(os.path.join(STATE, kind, doc_id))
    except Exception:
        return None


def needs_review(doc_ids):
    """The doc ids written after they were last reviewed (order preserved)."""
    out = []
    for doc_id in doc_ids:
        pub = _mtime("published", doc_id)
        if pub is None:
            continue
        seen = _mtime("passed", doc_id)
        if seen is None or seen < pub:
            out.append(doc_id)
    return out


def last_assistant_text(transcript_path):
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except Exception:
        return ""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "assistant":
            continue
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        text = " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
        if text.strip():
            return text
    return ""


def record(payload):
    """PostToolUse: note every doc id this call WROTE."""
    tool = payload.get("tool_name", "") or ""
    inp = payload.get("tool_input")
    cmd = (inp.get("command", "") or "") if (tool == "Bash" and isinstance(inp, dict)) else ""
    if not (WRITE_MCP.search(tool) or (cmd and WRITE_BASH.search(cmd))):
        return 0
    blob = ""
    for part in (payload.get("tool_response"), inp):
        try:
            blob += part if isinstance(part, str) else json.dumps(part, default=str)
        except Exception:
            pass
    ids = set(DOC_URL.findall(blob))
    if cmd:
        ids.update(BARE_ID.findall(cmd))
    if isinstance(inp, dict):
        for key in ("document_id", "documentId", "doc_id", "file_id", "fileId"):
            v = inp.get(key)
            if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{20,}", v):
                ids.add(v)
    for doc_id in ids:
        _touch("published", doc_id)
    return 0


def check(payload):
    """Stop: refuse to end the turn on a link to a doc written and never reviewed."""
    if payload.get("stop_hook_active"):
        return 0
    ids = []
    for doc_id in DOC_URL.findall(last_assistant_text(payload.get("transcript_path") or "")):
        if doc_id not in ids:
            ids.append(doc_id)
    stale = [d for d in needs_review(ids)
             if (_mtime("blocked", d) or 0) < (_mtime("published", d) or 0)]
    if not stale:
        return 0
    for doc_id in stale:
        _touch("blocked", doc_id)
    sys.stderr.write(
        "BLOCKED: you are handing over " + str(len(stale)) + " Google Doc link(s) you WROTE "
        "and have not reviewed since. \"It published\" is not \"it looks right\" — review "
        "each, fix any FAIL, then close:\n"
        + "".join(f"  uv run --project \"$CANOPY_ROOT\" canopy gdoc check {d}\n" for d in stale)
        + "Add --expect-links N (counted from the source) for docs that cite records, and "
        "--expect-blocks N for email-draft docs. $CANOPY_ROOT is the plugin runtime bundle "
        "(agent-core/deliverables.md).\n")
    return 2


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        return record(payload) if mode == "record" else check(payload)
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
