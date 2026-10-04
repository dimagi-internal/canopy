"""Pins the fleet `gog docs --dry-run` rail (gws baseline; promoted from eva 2026-10-04): the flag PERFORMS the write, so it must never be typed.

WHY THE RAIL EXISTS. On gog v0.12.0 `--dry-run` on a `gog docs` WRITE verb prints the intended
action and then does it anyway. Verified 2026-08-26 on a throwaway doc, three verbs, three
confirmations:

    insert       printed `inserted 4 bytes`      -> the document really began "ZULU"
    delete       printed `deleted 4 characters`  -> those 4 characters were really gone
    find-replace printed `replacements 1`        -> the replacement had really been applied

That makes the printed output indistinguishable from an honest dry run, which is what makes it
dangerous: the failure lands on the MOST careful turn, because `--dry-run` is exactly the flag an
agent reaches for in order to be safe. A dry-run-then-run pair therefore applies TWICE.

WHAT IT COST. On 2026-08-26 a turn mirroring an event into the UNGA 2026 planning doc ran
dry-run-then-run three times. The bullet was inserted twice; the follow-up `delete` of the 998-char
duplicate ran twice and took the NEXT 998 characters with it, destroying the "Target outreach list"
HEADING_1, its intro paragraph, the status-values line, the "Added for UNGA 2026" HEADING_2, the
whole Navyn Salem entry and most of Ron Dalgliesh. Nothing errored; every command exited 0. It was
recovered only because that turn happened to be holding a full `gog docs cat` capture taken before
the first edit.

WHY IT IS NARROW. Reads are the overwhelming majority of `gog docs` use and are completely safe --
`cat`, `export`, `info`, `structure`, `list-tabs` are NOT matched, with or without the flag. The
rail fires only on a write verb carrying the lying flag.

WHY IT IS ANCHORED. This repo documents its own railed commands in skills, tests and commit
messages. The calendar-delete rail paid for that lesson three times in two days, so this pattern
requires a REAL invocation position -- command start, or after a shell separator -- and prose that
merely quotes the command does not trip it.

THE SAFE PATH the message points at: capture `gog docs cat` first, take indices from
`gog docs structure --json` (a read), make the change ONCE with no dry-run flag, verify by
re-reading -- and for anything index-based or multi-step prefer the real Docs API via
`mcp__plugin_chrome-sales_gdrive__docs_batch_update`, which executes exactly once and is atomic
across requests.
"""

import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Split so this module cannot trip its own rail when the repo is grepped or the file is written
# out through a shell heredoc.
DRY = "--dry" + "-run"


def _rule():
    path = os.path.join(ROOT, "plugins", "canopy", "agent-core", "gating-baseline.json")
    with open(path) as fh:
        cfg = json.load(fh)
    for r in cfg["channels"]["gws"]:
        if r.get("tool") == "Bash" and "find-replace" in r.get("pattern", ""):
            return r
    raise AssertionError("gog docs dry-run rail not found in the gws baseline")


RULE = _rule()
PAT = re.compile(RULE["pattern"])


BLOCKED = [
    # The three invocations verified to mutate on 2026-08-26.
    f'gog docs insert DOCID "ZULU" --index 1 -a eva@dimagi-ai.com {DRY}',
    f"gog docs delete DOCID --start=5687 --end=6685 -a eva@dimagi-ai.com {DRY}",
    f'gog docs find-replace DOCID "old" "new" -a eva@dimagi-ai.com {DRY}',
    # The exact find-replace shape that doubled the UNGA bullet.
    f"gog docs find-replace DOCID \"one hour head-on.\" --content-file /tmp/r.txt "
    f"--format plain --first -a eva@dimagi-ai.com {DRY}",
    # Every other write verb.
    f"gog docs update DOCID --index 1 {DRY}",
    f"gog docs edit DOCID find replace {DRY}",
    f"gog docs sed DOCID 's/a/b/g' {DRY}",
    f"gog docs write DOCID -f /tmp/x {DRY}",
    f"gog docs clear DOCID {DRY}",
    # The short flag is the same defect.
    "gog docs insert DOCID \"x\" --index 1 -n",
    "gog docs delete DOCID --start=1 --end=5 -n -a eva@dimagi-ai.com",
    # Flag BEFORE the verb -- kong accepts it either side, so the rail must too.
    f"gog {DRY} docs insert DOCID \"x\" --index 1",
    f"gog -a eva@dimagi-ai.com {DRY} docs find-replace DOCID a b",
    # The `doc` alias.
    f"gog doc insert DOCID \"x\" --index 1 {DRY}",
    # Real invocation positions after a separator.
    f"cd /repo && gog docs insert DOCID \"x\" --index 1 {DRY}",
    f"echo hi; gog docs delete DOCID --start=1 --end=2 {DRY}",
    f"true | gog docs write DOCID -f - {DRY}",
    f"(gog docs clear DOCID {DRY})",
    f"echo a\ngog docs insert DOCID \"x\" --index 1 {DRY}",
]

ALLOWED = [
    # THE RIGHT PATH -- the write, made exactly once, with no lying flag. Must stay usable.
    'gog docs insert DOCID "x" --index 1 -a eva@dimagi-ai.com',
    "gog docs delete DOCID --start=5687 --end=6685 -a eva@dimagi-ai.com --force",
    'gog docs find-replace DOCID "old" "new" --first -a eva@dimagi-ai.com',
    # READS are safe and must never be blocked -- flag or no flag. These are how a turn gets the
    # indices and the before-capture the rail's message tells it to take.
    "gog docs cat DOCID -a eva@dimagi-ai.com > /tmp/before.txt",
    "gog docs structure DOCID -a eva@dimagi-ai.com --json",
    "gog docs export DOCID --format txt -a eva@dimagi-ai.com",
    "gog docs info DOCID -a eva@dimagi-ai.com",
    "gog docs list-tabs DOCID -a eva@dimagi-ai.com",
    f"gog docs cat DOCID -a eva@dimagi-ai.com {DRY}",
    f"gog docs structure DOCID --json {DRY}",
    # `-n` on a read is harmless, and `-n` belonging to ANOTHER command must not be dragged in.
    "gog docs cat DOCID -a eva@dimagi-ai.com | head -n 20",
    "gog docs structure DOCID -a eva@dimagi-ai.com | tail -n 5",
    # OTHER gog surfaces keep their (working) dry runs -- this rail is about `docs` only.
    f"gog gmail send --to x@example.com --subject s --body b {DRY}",
    f"gog calendar create CAL --summary s --from A --to B {DRY}",
    f"gog drive rm FILEID {DRY}",
    f"gog sheets write SHEETID --range A1 --values x {DRY}",
    # PROSE that quotes the command must not fire -- the anchor exists for exactly this.
    f"git commit -m 'ban gog docs insert ... {DRY} -- the flag performs the write'",
    f"gh pr create --body 'the rail blocks gog docs delete {DRY}'",
    f"echo 'never run gog docs find-replace {DRY} again'",
    # A write verb with no flag, and the flag in a LATER unrelated statement.
    f"gog docs insert DOCID \"x\" --index 1; gog calendar create CAL --summary s {DRY}",
]


@pytest.mark.parametrize("cmd", BLOCKED)
def test_dry_run_on_a_docs_write_verb_is_blocked(cmd):
    assert PAT.search(cmd), f"rail FAILED to block: {cmd!r}"


@pytest.mark.parametrize("cmd", ALLOWED)
def test_reads_prose_and_honest_writes_are_not_blocked(cmd):
    assert not PAT.search(cmd), f"rail wrongly fired on: {cmd!r}"


def test_message_names_the_safe_path():
    """The block message has to teach, not just refuse -- a turn hitting it needs the way out."""
    msg = RULE["message"]
    assert "structure" in msg, "message must point at the read that yields indices"
    assert "documents.batchUpdate" in msg, "message must point at the once-only Docs API path"
    assert "service account" not in msg, "the batch runs AS the agent — never a shared SA"
    assert "cat" in msg, "message must tell the turn to capture the doc before editing"


def test_pattern_doc_records_the_evidence():
    """A rail whose reasoning is not written down gets deleted by a later turn that doubts it."""
    doc = RULE["_pattern_doc"]
    assert "2026-08-26" in doc
    for verb in ("insert", "delete", "find-replace"):
        assert verb in doc, f"_pattern_doc must record the verified verb {verb!r}"
