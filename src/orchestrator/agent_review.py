"""Agent self-improvement lens — point canopy's analyze→propose loop at an agent's turns.

Build 2 of docs/agent-operating-model.md: the active learning loop reef never had. Reviews an
agent's recent TURN transcripts for operating-model friction — dropped checklist steps, tool
failures/retries, gating blocks, repeated manual work that should be a skill — and produces
findings + recommended fixes scoped to the agent's repo (skill edit / hook rule / CLAUDE update /
channel fix). The deterministic friction extraction is the testable core; an optional claude -p
pass synthesizes ranked findings on top of it (the evaluator–optimizer step, §6.3).

Reuses the existing machinery: transcripts.py (parse/extract), repo_paths.py (resolve), and the
analyzer's claude -p pattern. It does NOT fork the pipeline — it's a lens on the same loop.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from orchestrator.repo_paths import resolve_repo_path
from orchestrator.session_sources import (
    corpus_confidence,
    local_transcript_dirs,
    session_sources,
)
from orchestrator.transcripts import (
    extract_assistant_text,
    extract_tool_calls,
    extract_user_messages,
    read_transcript,
)

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Kept as the module default for `find_turn_transcripts`, but `run_review` no longer
# scans it directly — see the `session_sources` seam below. One home is one account,
# and an agent's corpus is routinely split across the two this human alternates
# between; a review drawn from half a corpus reads as complete and isn't.

# Wall-clock budgets for the two claude -p passes. Named constants, not magic numbers
# buried at the call site, so the CLI can expose them and tests can assert the default.
#
# BOTH DEFAULT TO None — NO TIMEOUT. They used to be 180s and 300s, and the budget was
# routinely shorter than the work: a 7-turn hal corpus timed out the synthesis pass on
# the 180s default, repeatably. That is the worst possible failure for this tool,
# because a timed-out pass returns ZERO findings — so the budget didn't bound a slow
# review, it silently converted one into an empty one, which is exactly the "no
# findings == clean bill of health" misread the surrounding code already shouts about.
# A review that takes six minutes is fine; a review that invents a clean result is not.
# (Jonathan, 2026-08-13: "just get rid of the timeout, there is no need to have one".)
#
# The timeout PLUMBING stays — `--timeout` still works, and TimeoutExpired is still
# caught into a well-formed error — so an operator or a cron that wants a bound can
# set one and still get the fail-loud error shape that a live incident paid for.
SYNTHESIS_TIMEOUT = None   # `run_review`'s synthesis pass
VERIFY_TIMEOUT = None      # `_call_verify_llm`'s source-verification pass


def _error_text(value: object, fallback: str) -> str:
    """Every `error` this module returns is a NON-EMPTY, human-readable STRING.

    A live cron run hit a synthesis-pass failure whose `error` came back a bare,
    non-string value; the consumer did `len(error)` and died with
    `TypeError: object of type 'int' has no len()` — a ~$2 pass yielded nothing and
    the failure was never flagged. Consumers must be able to treat `error` as text
    unconditionally, so every assignment funnels through here.
    """
    if isinstance(value, str) and value.strip():
        return value
    if value is None or (isinstance(value, str) and not value.strip()):
        return fallback
    return f"{fallback} ({value!r})"

# Operating-model friction taxonomy. Findings get tagged with one of these.
FRICTION_TYPES = (
    "human_correction",  # the human had to correct/override the agent (HIGHEST signal — read these first)
    "tool_failure",    # a tool call errored
    "retry_loop",      # the same tool was re-tried after a failure
    "gating_block",    # a PreToolUse hook blocked an action (deny)
    "checklist_gap",   # an expected turn step never ran
    "skill_capture",   # a multi-step manual pattern that should be a skill
    "auth_friction",   # auth/credential/setup blockers
    "skill_collision", # loaded ANOTHER plugin's same-named skill (e.g. ace:turn) over its own
    "over_claim",      # the agent asserted a completion verb with no tool_use to back it in-turn
    "verify_late",     # a completion claim whose only substantiating tool_use lands in a LATER turn
)

# Human-correction mining — the lens agent-review was BLIND to (echo's last turn taught us: it
# flagged git pathspec errors but missed Jonathan demanding "NEVER EVER submit without review").
# A human overriding a safety behavior, or expressing confusion, outranks any mechanical friction.
_CORRECTION_PATTERNS = (
    # safety override — the agent did (or was about to do) something it must NOT do autonomously
    ("safety_override", re.compile(
        r"\bnever\b.{0,40}\b(submit|send|post|publish|delete|push|merge|pay|buy|email|reply)\b|"
        r"without (?:human |explicit )?(?:review|approval|sign-?off|permission)|"
        r"\bmust not\b|\bshould never\b|\bdo ?n['’o]?t ever\b|\bnever ever\b", re.I)),
    # confusion — the agent's output didn't make sense to the human.
    # The CAN'T variants were missing until 2026-08-18, when this lens was run live against a
    # session whose human turn was "I can't follow what the fuck you're doing" and scored it as a
    # neutral steer. `i don['’]t follow` was there; `i can't follow` was not, and inability is the
    # more common phrasing when someone is actually lost. Same for the intensified form of "what
    # are you doing" — a human who swears mid-sentence is not less confused for it.
    ("confusion", re.compile(
        r"\bi['’ ]?m lost\b|\bi am lost\b|\bconfus|why are you|why did you|"
        r"what (?:the \S+ )?are you doing|"
        r"that['’]s not what i|does ?n['’]t make sense|makes no sense|not making (?:any )?sense|"
        r"i don['’]t (?:get|follow|understand)|"
        r"i (?:can['’]?t|cannot) (?:follow|tell|understand)", re.I)),
    # strong correction — a forceful "no, do it differently"
    ("strong_correction", re.compile(
        r"\bstop\b|\byou['’]re wrong\b|that['’]s wrong|that is wrong|^\s*no[,.! ]|"
        r"\binstead of\b|not what i (?:asked|wanted|meant)|\bredo\b|\bundo\b", re.I)),
)


# Blocks the HARNESS injects as `user` turns. They are string-content messages like a real
# human turn, but nobody typed them — and their own boilerplate trips the patterns above
# (the local-command caveat contains "DO NOT respond to these messages", which scores
# `emphasis`; task notifications carry "instead of" and sentence-initial "no"). Measured on
# ACE 2026-07-28: 4 of 6 reported "human corrections" were these. Stripped wherever they
# appear, not just as a prefix, because the harness APPENDS system-reminders to genuine
# messages — so a prefix test would keep the human's words and still score the boilerplate.
_HARNESS_BLOCK_RX = re.compile(
    r"<(local-command-caveat|local-command-stdout|local-command-stderr|task-notification|"
    r"system-reminder|command-message|command-name|command-args|user-prompt-submit-hook)>"
    r".*?</\1>|"
    r"<(local-command-caveat|task-notification|system-reminder|command-message|command-name|"
    r"command-args)>.*",
    re.S | re.I,
)
# Whole user turns the harness AUTHORS, identified by their opening line: the skill loader
# injects a skill body, and compaction injects a summary of the prior conversation. The
# compaction summary is the nastiest of these — it restates the human's earlier asks verbatim,
# so it scores every correction in the session a second time, in a turn nobody typed.
_HARNESS_AUTHORED_PREFIXES = (
    "Base directory for this skill:",
    "This session is being continued from a previous conversation",
)

# The same failure class, one layer out: `user` turns that are plain strings like a real
# human turn, but that NO HUMAN AUTHORED. The blocks above are tag-delimited, so a regex
# finds them; these are not, so they need a structural tell instead of a phrase match.
# Two producers, two tells:
#
# 1. HOOK FEEDBACK. A Stop/PreToolUse hook's `reason` is replayed into the conversation as
#    a `user` turn — "Stop hook feedback: This turn is ending without its close-out. Run it
#    now: bin/hal-turn-close …". A close-out rail is written to be forceful, so it scores
#    `strong_correction` every time it fires. Claude Code stamps these `isMeta: true`, which
#    is the harness's own statement that nobody typed it — exact, and it needs no phrase list.
#
# 2. AGENT-AUTHORED DISPATCH BRIEFS. A dispatched turn opens with a brief another agent
#    wrote, delivered to the runner as if typed. Nothing in the transcript distinguishes it
#    (`origin: {kind: human}`, `promptSource: typed` — the harness genuinely was handed it as
#    input), so the marker has to come from the dispatcher. `agent_dispatch.stamp_dispatched`
#    puts DISPATCH_MARKER on every prompt canopy sends; this strips any turn carrying it.
#
# Measured on hal 2026-08-14 (canopy #488): of 6 reported "human corrections", 5 were these —
# two hook replays, two agent dispatch briefs, one generated resume brief; ONE was Jonathan.
# The cost was not cosmetic. A generated brief became a fleet-scope finding, promoted on the
# grounds that it appeared in two agents' transcripts in the same window — which is what a
# six-session dispatch batch looks like. Co-occurrence is this bug's SIGNATURE, and it reads
# as exactly the evidence that justifies escalating.
#
# Self-amplifying, too: agent-review's own findings are delivered to agents AS dispatch
# briefs, which the next cycle mines back as `strong_correction`.
# Imported, not re-declared: this used to be a hand-kept copy of the stamper's literal, which
# was safe only while the marker was a fixed string. It now carries an optional `from=<slug>`, so
# a copy would drift the moment a sender was attached — and a stripper that misses the marker
# silently un-suppresses every brief. One definition, one regex, one import.
from orchestrator.agent_dispatch import (  # noqa: E402
    DISPATCH_MARKER,
    HUMAN_REPLY_RX,
    SENDER_MARKER_RX,
    dispatched_by,
    human_reply_in,
)


def _is_harness_authored_entry(entry: dict) -> bool:
    """True when the HARNESS wrote this `user` entry rather than replaying what a human typed.

    `isMeta` is Claude Code's own flag for it. Read defensively — an older transcript, or a
    different harness version, simply won't carry the key, and this must degrade to "assume
    human" rather than start dropping real corrections.
    """
    return bool(entry.get("isMeta"))


def _human_text(raw: str) -> str:
    """What the human actually typed, with harness-injected blocks removed.

    A dispatched turn is dropped whole — EXCEPT for any region canopy-web delimited as the
    human's own words. A board card's brief travels to the working agent with the decider's
    reply appended to it, so before this the stamp that stopped the brief being mined threw the
    reply away with it. That reply is the highest-value human signal on the board: it is a person
    overruling, narrowing, or redirecting an agent's proposal, which is what this lens is for.
    Everything around it still goes — the brief, the provenance line, and canopy-web's own
    trailing boilerplate, which says OVERRIDES and "instead of" and would score as a forceful
    correction entirely on its own.
    """
    s = _HARNESS_BLOCK_RX.sub(" ", raw or "").strip()
    if s.startswith(_HARNESS_AUTHORED_PREFIXES):
        return ""
    if DISPATCH_MARKER in s:
        return human_reply_in(s)
    return s


def human_authored_messages(entries: list[dict]) -> list[str]:
    """`extract_user_messages`, minus the `user` turns the HARNESS itself authored.

    Deliberately NOT folded into `extract_user_messages`: its other callers (the
    checklist-gap haystack, fleet-align, shareout) want every word that appeared in the
    conversation, machine-written or not. Only the corrections lens cares who typed it.
    """
    out: list[str] = []
    for entry in entries:
        if entry.get("type") != "user" or _is_harness_authored_entry(entry):
            continue
        msg = entry.get("message", {})
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            out.append(content)
    return out


def human_corrections(entries: list[dict]) -> list[dict]:
    """Mine the HUMAN side of a turn for corrections/overrides/confusion — the highest-signal
    friction. A forceful safety correction ("NEVER submit without review") matters more than ten
    git errors, but the mechanical signals miss it entirely. Returns [{kinds, quote}].

    Only genuinely human-authored text is mined: harness-injected `user` turns are stripped
    first (`_human_text`), so the caveat/notification boilerplate can't masquerade as Jonathan
    overriding something. `isMeta` turns (replayed hook feedback) and canopy-dispatched prompts
    (another agent's brief) are dropped whole — see DISPATCH_MARKER above."""
    out: list[dict] = []
    for m in human_authored_messages(entries):
        s = _human_text(m)
        if not s:
            continue
        kinds = [kind for kind, pat in _CORRECTION_PATTERNS if pat.search(s)]
        # ALL-CAPS emphasis (2+ shouted words, or NEVER/ALWAYS/STOP) = a forceful demand
        if re.search(r"\b[A-Z]{3,}\b[^a-z]{0,30}\b[A-Z]{3,}\b", s) or re.search(
                r"\b(NEVER|ALWAYS|STOP|DO NOT|MUST)\b", s):
            kinds.append("emphasis")
        if kinds:
            out.append({"kinds": sorted(set(kinds)), "quote": s.replace("\n", " ")[:240]})
    return out


# Dispatch-outcome mining — the OTHER half of DISPATCH_MARKER, and the reason it is worth more
# than the suppression it was built for.
#
# The marker was added to stop a dispatched brief being mined as the human shouting. That is
# subtraction. But it also, for the first time, makes a fact machine-readable that nothing else in
# a transcript records: THIS SESSION WAS STARTED BY A MACHINE. Everything after the brief is the
# outcome of whatever judgment call the dispatcher made — so the marker is an anchor, not just a
# mute button, and the span after it is the only place a dispatching agent can learn whether its
# own findings were any good.
#
# That loop has never existed. Ada dispatches fix briefs to the fleet every cycle and gets back
# exactly nothing about which ones were worth sending: a brief built on a stale review window and
# a brief that shipped clean are indistinguishable to her the next morning. She keeps grading the
# agents and never gets graded.
#
# THE SIGNAL THIS RESTS ON is deliberately the crispest one available: a genuine human message in
# a session a machine started. Nobody typed into that session by accident — the dispatcher aimed
# it at an agent and walked away. So a human turn there means a person had to come back and steer
# something that was supposed to run unattended, and a human turn carrying a CORRECTION means they
# had to argue with it. No phrase list decides this and no LLM judges it; it is a structural fact
# about who authored which entry, which is why it is trustworthy enough to grade a dispatcher on.
#
# WHAT IS DELIBERATELY NOT HERE: `rejected_on_revalidation` — the case where the receiving agent
# re-validates the brief, finds the finding already fixed or a misread, and stops. Every fix brief
# mandates that step, so it is the outcome most worth counting, and it is exactly the one that
# needs judgment rather than a regex: the agent's report quotes the brief's own "already fixed"
# language back, so a lexical test scores the brief instead of the verdict. Registered in
# DISPATCH_VERDICTS, detected by nothing, and left for an LLM pass rather than shipped half-right
# — the same call `overclaim_signals` made for `verify_late`. A dispatcher scorecard that quietly
# guessed at its own worst category would be worse than one that admits the gap.

DISPATCH_VERDICTS = (
    "contested",        # a human had to argue with it — the strongest evidence it was a bad send
    "human_touched",    # a human stepped in, but to steer/answer rather than to correct
    "shipped_clean",    # ran unattended and landed a merge
    "ran_unattended",   # ran unattended, no merge to point at
    "rejected_on_revalidation",   # REGISTERED, NOT DETECTED — see the note above
)

# `gh pr merge` is the crispest "it landed" tell available, and it is read off TOOL CALLS rather
# than prose: an agent that merely SAYS it merged is what `overclaim_signals` is for.
_MERGE_RX = re.compile(r"\bgh\s+pr\s+merge\b", re.I)


def dispatch_outcomes(entries: list[dict]) -> dict | None:
    """Grade a DISPATCHED session on what happened after the brief. None if not dispatched.

    Returns {verdict, n_human_turns_after, interventions: [{kinds, quote}], shipped,
    brief_excerpt}. The caller attributes it to whoever SENT the brief, not to the agent that
    received it — this is the dispatcher's report card, and it is the one lens in this module
    whose findings belong to a different agent than the one being reviewed.

    Anchored on the FIRST marked human turn, not the first turn overall: a dispatch can land
    mid-session when the runner resolves onto a live session instead of spawning a fresh one
    (the missing-thread_key failure), and that session's earlier human turns are a real
    conversation that has nothing to do with the brief. A later second brief in the same session
    folds into the first one's span rather than opening a new one — rare, and splitting it would
    invent a boundary the transcript does not actually carry.
    """
    anchor = None
    for i, entry in enumerate(entries):
        if not _is_genuine_human_message(entry) or _is_harness_authored_entry(entry):
            continue
        if DISPATCH_MARKER in (entry.get("message", {}).get("content", "") or ""):
            anchor = i
            break
    if anchor is None:
        return None

    brief = entries[anchor].get("message", {}).get("content", "") or ""
    after = entries[anchor + 1:]

    # A human turn AFTER the brief. `_human_text` still strips harness blocks and drops any
    # FURTHER marked turn, so a second dispatch into the same session never counts as a person
    # intervening — that would grade the dispatcher for its own second message.
    human_after: list[str] = []
    for entry in after:
        if not _is_genuine_human_message(entry) or _is_harness_authored_entry(entry):
            continue
        text = _human_text(entry.get("message", {}).get("content", "") or "")
        if text:
            human_after.append(text)

    interventions: list[dict] = []
    for text in human_after:
        kinds = [kind for kind, pat in _CORRECTION_PATTERNS if pat.search(text)]
        if re.search(r"\b[A-Z]{3,}\b[^a-z]{0,30}\b[A-Z]{3,}\b", text) or re.search(
                r"\b(NEVER|ALWAYS|STOP|DO NOT|MUST)\b", text):
            kinds.append("emphasis")
        if kinds:
            interventions.append({"kinds": sorted(set(kinds)), "quote": text.replace("\n", " ")[:240]})

    shipped = any(
        _MERGE_RX.search(json.dumps(c.get("input", {})))
        for c in extract_tool_calls(after)
    )

    if interventions:
        verdict = "contested"
    elif human_after:
        verdict = "human_touched"
    elif shipped:
        verdict = "shipped_clean"
    else:
        verdict = "ran_unattended"

    return {
        "verdict": verdict,
        # WHO to hand this verdict to. '' for a brief stamped before senders were carried, or by
        # a dispatcher that does not know its own slug — reported as unattributed rather than
        # guessed, since the whole point is to grade a specific sender.
        "dispatched_by": dispatched_by(brief),
        "n_human_turns_after": len(human_after),
        "interventions": interventions,
        "shipped": shipped,
        "brief_excerpt": _human_text_without_marker(brief)[:240].replace("\n", " "),
    }


def _human_text_without_marker(raw: str) -> str:
    """The brief's own words. `_human_text` deliberately returns '' for anything marked (that is
    its whole job in the corrections lens), so quoting the brief needs the marker stripped
    instead of the turn dropped."""
    stripped = _HARNESS_BLOCK_RX.sub(" ", raw or "").replace(DISPATCH_MARKER, "")
    stripped = SENDER_MARKER_RX.sub("", stripped)
    return HUMAN_REPLY_RX.sub(lambda m: m.group(1), stripped).strip()


# Over-claim mining — a completion verb in the agent's OWN text ("verified", "shipped", "done", …)
# asserted with no tool_use in the SAME assistant message to back it. Mirrors human_corrections:
# walk the RAW transcript entries (not the flattened extract_assistant_text/extract_tool_calls
# corpus friction_signals otherwise uses), because "same turn" is only visible at the per-entry
# granularity — an assistant entry's message.content list interleaves its text and tool_use blocks.
_CLAIM_RX = re.compile(r"\b(shipped|merged|verified|done|fixed|applied|confirmed)\b", re.IGNORECASE)


def _is_genuine_human_message(entry: dict) -> bool:
    """True iff `entry` is a human-authored user message, not a tool_result. Mirrors
    extract_user_messages: a genuine human message has message.content as a plain str;
    a tool_result comes back as a `user`-role entry whose content is a list of blocks."""
    if entry.get("type") != "user":
        return False
    msg = entry.get("message", {})
    if not isinstance(msg, dict):
        return False
    content = msg.get("content", "")
    return isinstance(content, str) and bool(content)


def overclaim_signals(entries: list[dict]) -> list[dict]:
    """Flag assistant completion-claims not substantiated by a tool_use block ANYWHERE in the
    current TURN. Returns [{type: 'over_claim', evidence, turn}].

    A "turn" = the run of entries since the last GENUINE human message. Claude Code routinely
    splits work across entries — an assistant tool_use entry, then a user tool_result entry,
    then a SEPARATE assistant entry with the wrap-up text ("Done and verified...") — so the
    substantiation window must span entries, not stop at the one carrying the claim text.
    Tool RESULTS come back as `user`-role entries (content = tool_result blocks); those are NOT
    genuine human messages and must NOT reset the turn — only a real human text message does
    (see _is_genuine_human_message, which mirrors extract_user_messages/human_corrections).

    `verify_late` (a completion claim whose substantiating tool_use appears only in a LATER
    turn) stays registered in FRICTION_TYPES but is NOT detected here yet: reliably linking a
    specific claim to a specific later tool call needs more than this deterministic regex pass
    can promise without false positives, so it's a follow-up enrichment rather than something to
    ship half-right. `over_claim` — current-turn, unambiguous — ships solidly now.
    """
    out: list[dict] = []
    tool_use_seen_this_turn = False
    for i, entry in enumerate(entries):
        if _is_genuine_human_message(entry):
            tool_use_seen_this_turn = False
            continue
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        has_tool_use = any(
            isinstance(block, dict) and block.get("type") == "tool_use" for block in content
        )
        text = " ".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if text and _CLAIM_RX.search(text) and not (tool_use_seen_this_turn or has_tool_use):
            out.append({"type": "over_claim", "evidence": text[:200], "turn": i})
        if has_tool_use:
            tool_use_seen_this_turn = True
    return out


# Expected turn steps for an operating-model agent, with markers that evidence each ran.
# A step with no marker present in a turn is a candidate `checklist_gap`.
# A marker set must cover how the step is performed TODAY, not only how it was performed
# when the marker was written. The haystack is assistant text + user text + tool NAMES and
# INPUTS — it does NOT include tool RESULTS. So a step whose mechanics moved inside a
# script becomes invisible: the command string lives in the file, and only the script's
# own name reaches the haystack.
#
# That is not hypothetical. `workspace-refresh` matched only `agent-publish` / `/agents/`,
# but every agent now performs it via its close-out script (`bin/<slug>-turn-close`, which
# shells `canopy agent-publish`). Measured on hal over 12 turn-sessions, 2026-08-19:
# the close-out ran in 10, and the OLD markers detected 1. The metric was reporting an
# 11/12 failure rate for a step that was running 10/12 — so it penalized the agent for
# adopting the rail, and the better hal behaved the worse it scored. A finding was already
# being raised off that false signal, which is the expensive part: a wrong metric doesn't
# just mislead, it dispatches work.
#
# `self-review` had the same shape from vocabulary drift — hal and ace renamed the step to
# `agent-turn-review` (eva/echo still use the old name), so the marker missed the renamed
# one entirely: 6 detected -> 8 with the alias.
#
# `preflight` was checked the same way and needed NO widening (4/12 both before and after
# adding `canopy-update-check`/`UP_TO_DATE`/`UPGRADE_AVAILABLE`), which is the control that
# says this is a marker-staleness bug and not a blanket "loosen everything".
#
# Rule of thumb when adding a step: include the CANONICAL command, the SCRIPT that wraps it,
# and any renamed alias still in fleet use.
DEFAULT_TURN_STEPS = (
    ("preflight", (r"preflight", r"readiness", r"canopy-update-check",
                   r"up_to_date", r"upgrade_available")),
    # `agent-turn-review`: hal/ace's name for the step eva/echo call `self-review`.
    ("self-review", (r"self-review", r"self review", r"agent-turn-review")),
    ("skill-self-check", (r"skill.?self.?check", r"did i (create|improve) a skill",
                          r"should be a skill")),
    # `turn-close`: the close-out script that PERFORMS the publish; `agent turn` packages it.
    ("workspace-refresh", (r"agent-publish", r"/agents/", r"turn-close", r"agent turn\b")),
)

# NOTE: no bare "blocked" here — PR status output ("mergeable: MERGEABLE/BLOCKED") and prose
# ("blocked only on required review") made every PR-triage turn look like a failure storm.
# Gating friction is detected separately via _GATING_MARKERS.
_ERROR_MARKERS = re.compile(
    r"(?:^|\b)(?:error|errno|traceback|exception|failed|not found|not been used|"
    r"permission denied|fatal|✗|exit code [1-9]|"
    r"4(?:00|01|03|04|09|22|29)|5(?:00|02|03))\b",
    re.I,
)
# A gating block is a PreToolUse hook outcome — only tools the guard actually gates can
# produce one (a Read of the hook's own source contains "permissionDecision" but is not a block).
# When a hook fires, its message IS the whole tool result, so the marker sits at the head —
# a `cat config/gating.json` carries the same strings, but buried past the file preamble.
_GATABLE_TOOLS = {"Bash", "Edit", "Write", "NotebookEdit"}
_GATING_HEAD = 300
# No bare "PreToolUse" here — gating-policy prose mentions it constantly; hook RESULTS
# always carry one of these instead.
_GATING_MARKERS = re.compile(
    r"hookSpecificOutput|permissionDecision|BLOCKED:|hook (?:denied|blocked)|blocked by .{0,20}hook",
    re.I,
)
_AUTH_MARKERS = re.compile(
    r"(?:not logged in|no token|invalid token|unauthorized|401|403|api .*not enabled|"
    r"credentials?|oauth|1password|op read|op inject)",
    re.I,
)
# A completed file write is never runtime friction — but its success result (or a path/filename
# that happens to contain "oauth", "error", …) would otherwise match the error/auth markers.
# hal's 2026-07 review flagged a successful Write of `email-oauth-not-minted.md` as auth_friction.
_EDITOR_TOOLS = {"Edit", "Write", "NotebookEdit"}
_WRITE_OK = re.compile(
    r"file (?:created|updated|written) successfully|successfully (?:created|wrote|updated|saved)|"
    r"file state is current",
    re.I,
)
# Markers strong enough to still mean friction on a call the harness recorded as
# SUCCEEDING. A command wrapped in `|| true`, or one whose failure the agent
# captured deliberately, exits 0 while its output holds a real stack trace — that
# is worth surfacing. The weak markers above are not: on a succeeding call, a bare
# "error"/"credentials"/"404" is almost always the *subject* of the output rather
# than its outcome (a file being read, a URL slug, a doctor line saying PASS).
_STRONG_ERROR_MARKERS = re.compile(
    r"traceback \(most recent call last\)|\bexit code [1-9]|\bfatal:|\bsegmentation fault\b|"
    r"unhandled exception|panic:",
    re.I,
)
# The turn-step checklist only means something for a TURN. Applying it to an `architect ddd` /
# harvest session flagged every one as a 4-gap "failure storm" (hal's 2026-07 review). A session
# counts as a turn only if it actually engaged the turn loop.
_TURN_MARKERS = re.compile(
    r"skills/turn|/turn/skill|[\w-]*turn-close|\bdo a turn\b|\btake a turn\b", re.I
)


def resolve_agent_repo(slug_or_path: str) -> Path | None:
    """Resolve an agent slug or path to its repo root (must hold .claude-plugin/plugin.json)."""
    p = Path(slug_or_path).expanduser()
    if "/" in str(slug_or_path) and p.exists():
        return p if (p / ".claude-plugin" / "plugin.json").exists() else p
    rp = resolve_repo_path(slug_or_path)
    return rp


def _result_text(call: dict) -> str:
    r = call.get("result")
    if isinstance(r, list):
        return " ".join(str(b.get("text", b) if isinstance(b, dict) else b) for b in r)
    return str(r or "")


def _call_subject(call: dict) -> str:
    """The most comparable piece of a call's input — command / path, else the whole input."""
    inp = call.get("input")
    if not isinstance(inp, dict):
        return str(inp or "")
    return str(
        inp.get("command")
        or inp.get("file_path")
        or inp.get("notebook_path")
        or json.dumps(inp, sort_keys=True)
    )


def _retried_after(calls: list[dict], i: int, window: int = 8) -> bool:
    """True if the tool that failed at index i re-ran shortly after on a near-identical
    subject. (The old check — same tool name appearing anywhere else in the turn — flagged
    every Bash-heavy turn as a retry loop.)"""
    tool = calls[i].get("name", "")
    prefix = _call_subject(calls[i]).strip()[:30]
    if not prefix:
        return False
    for later in calls[i + 1 : i + 1 + window]:
        if later.get("name") != tool:
            continue
        other = _call_subject(later).strip()[:30]
        if other and (other.startswith(prefix) or prefix.startswith(other)):
            return True
    return False


def _transcript_cwd(path: Path) -> str:
    """The cwd a transcript ran in (Claude records it per entry). '' if unknown."""
    for entry in read_transcript(path):
        cwd = entry.get("cwd")
        if cwd:
            return cwd
    return ""


def _belongs_to_agent(cwd: str, repo: Path, slug: str) -> bool:
    if not cwd:
        return False
    cwd = str(cwd)
    return (
        cwd == str(repo)
        or cwd.startswith(str(repo) + "/")
        or f"/worktrees/{slug}/" in cwd
        or cwd.rstrip("/").endswith(f"/repositories/{slug}")
    )


def find_turn_transcripts(
    repo: Path, hours: int = 168, projects_dir: Path = CLAUDE_PROJECTS
) -> list[Path]:
    """Recent transcripts whose cwd is within the agent's repo (or one of its worktrees)."""
    slug = repo.name
    if not projects_dir.exists():
        return []
    cutoff = time.time() - hours * 3600
    # Pre-filter project dirs by name (the encoded cwd contains the slug) to bound the scan.
    name_re = re.compile(rf"-{re.escape(slug)}(?:-|$)")
    out: list[tuple[float, Path]] = []
    for d in projects_dir.iterdir():
        if not d.is_dir() or not name_re.search(d.name):
            continue
        for f in d.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            if _belongs_to_agent(_transcript_cwd(f), repo, slug):
                out.append((mtime, f))
    return [f for _, f in sorted(out, reverse=True)]


def friction_signals(
    transcript_path: Path,
    steps=DEFAULT_TURN_STEPS,
    own_skills: frozenset[str] = frozenset(),
) -> dict:
    """Deterministic per-turn friction signals. No LLM — pure structural extraction.

    `own_skills` is the set of skill dir-names the agent owns (repo/skills/*); it powers
    `skill_collisions` — loading another plugin's same-named skill (e.g. `ace:turn`) over its own.
    """
    entries = read_transcript(transcript_path)
    calls = extract_tool_calls(entries)
    asst_text = "\n".join(extract_assistant_text(entries)).lower()

    failures, gating_blocks, auth_hits, skill_collisions = [], [], [], []
    failed_idx: list[int] = []
    for i, c in enumerate(calls):
        tool = c.get("name", "")
        # Skill collision: the agent loaded a namespaced skill (`plugin:name`) whose bare name is
        # one of ITS OWN skills — i.e. another plugin's version shadowed the agent's. Silent (the
        # skill loads fine), so no error/auth marker ever fires; only this cross-ref catches it.
        if tool == "Skill":
            sk = str(c.get("input", {}).get("skill", "")).strip()
            if ":" in sk and sk.rsplit(":", 1)[-1] in own_skills:
                skill_collisions.append({"invoked": sk, "own_skill": sk.rsplit(":", 1)[-1]})
        res = _result_text(c)
        if not res:
            continue
        head = res[:600]
        if tool in _GATABLE_TOOLS and _GATING_MARKERS.search(res[:_GATING_HEAD]):
            gating_blocks.append({"tool": tool, "evidence": head[:200]})
            continue
        # A completed file write is not runtime friction — skip error/auth scanning on its
        # success result so an "oauth"/"error" in the path/name can't masquerade as a failure.
        if tool in _EDITOR_TOOLS and _WRITE_OK.search(head):
            continue

        # Trust the harness's own verdict over a grep of the result text.
        #
        # Every tool_result block carries `is_error`; this extractor used to drop it
        # and re-derive failure from the prose, which made the SUBJECT of a result
        # indistinguishable from its OUTCOME. Measured on 36 ACE turns: a doctor line
        # reading `PASS cchq_connect_features: … OAuth connection … configured` was
        # counted as BOTH a failure and auth friction; so were `--- tsc exit: 0 ---`
        # (the "404" lived in a release URL), a 200 response, and reading the source
        # of `apps/api/auth.py`. See dimagi-internal/canopy#416.
        #
        # `is_error is None` means the transcript predates the flag — fall through to
        # the markers, so old sessions review exactly as they did before.
        is_error = c.get("is_error")
        if is_error is False:
            # Succeeded. Only a marker that survives a zero exit still counts —
            # a `|| true`-wrapped command can hold a real traceback.
            if _STRONG_ERROR_MARKERS.search(head):
                failed_idx.append(i)
                failures.append({
                    "tool": tool,
                    "input": json.dumps(c.get("input", {}))[:160],
                    "evidence": head[:200],
                })
            continue

        if is_error or _ERROR_MARKERS.search(head):
            failed_idx.append(i)
            failures.append({
                "tool": tool,
                "input": json.dumps(c.get("input", {}))[:160],
                "evidence": head[:200],
            })
        if _AUTH_MARKERS.search(head):
            auth_hits.append({"tool": tool, "evidence": head[:200]})

    # Retry loops: the same tool re-run on a near-identical subject shortly after failing.
    retries = sorted({calls[i].get("name", "") for i in failed_idx if _retried_after(calls, i)})

    # Checklist gaps: expected TURN steps with no marker anywhere in tool inputs/assistant text.
    # Only graded on sessions that actually engaged the turn loop — an architect/harvest run is
    # not a turn and grading it against turn steps is pure noise.
    user_text = "\n".join(extract_user_messages(entries))
    haystack = asst_text + "\n" + user_text.lower() + "\n" + "\n".join(
        f"{c.get('name','')} {json.dumps(c.get('input',{}))}" for c in calls
    ).lower()
    is_turn = bool(_TURN_MARKERS.search(haystack))
    missing_steps = [
        label for label, markers in steps
        if not any(re.search(m, haystack) for m in markers)
    ] if is_turn else []

    return {
        "session_id": transcript_path.stem,
        "path": str(transcript_path),
        "n_tool_calls": len(calls),
        "human_corrections": human_corrections(entries),   # HIGHEST-signal — read first
        "dispatch_outcome": dispatch_outcomes(entries),    # None unless a machine started it
        "overclaims": overclaim_signals(entries),
        "failures": failures,
        "gating_blocks": gating_blocks,
        "auth_friction": auth_hits,
        "retry_loops": retries,
        "checklist_gaps": missing_steps,
        "skill_collisions": skill_collisions,
    }


def build_review_prompt(repo: Path, corpus: list[dict]) -> str:
    """Assemble the friction corpus + agent identity into an evaluator–optimizer prompt.

    Assembled INLINE by the framework-tier convention (#352): framework logic-prompts
    stay inline — static, co-located with their logic, and immune to the #351 packaging
    class (a Python string literal always ships, unlike an external `.md`) — while
    PRODUCT, user-editable templates go external via `prompts/load_prompt`. Loading from
    the PRODUCT `prompts/` package here would also break the framework→product boundary
    (`tests/test_plugin_boundary.py`). Sibling site: `fleet_align.build_judgment_prompt`."""
    persona = ""
    pp = repo / "persona.md"
    if pp.exists():
        persona = pp.read_text()[:1500]
    return (
        "You are canopy's agent self-improvement reviewer. Below is structural friction extracted "
        f"from recent TURNS of the agent at {repo} (its own git repo).\n\n"
        f"AGENT PERSONA (excerpt):\n{persona}\n\n"
        f"FRICTION CORPUS (deterministic signals per turn):\n{json.dumps(corpus, indent=2)}\n\n"
        "Produce a YAML list of findings. Each item:\n"
        "  - title: short imperative\n"
        f"  - friction_type: one of {list(FRICTION_TYPES)}\n"
        "  - evidence: a RECORD (not free text) proving the finding is grounded:\n"
        "      source_ref: the file:line / PR / session you actually consulted\n"
        "      was_read: true    # you OPENED it — not grepped a proxy for it\n"
        "      already_fixed_check: {ran: true, result: '<not-fixed on origin/main @sha | fixed by ...>'}\n"
        "      confidence: high|medium|low\n"
        "      confidence_basis: one sentence justifying the level from the evidence above\n"
        "  - A finding whose evidence is not a complete record WILL BE DROPPED. Do not emit it.\n"
        "  - fix_kind: one of [skill_edit, hook_rule, schema_validator, claude_update, channel_fix, new_skill]\n"
        "  - target: the file/path in the agent repo the fix touches\n"
        "  - recommendation: the concrete change to make\n"
        "  - confidence: high|medium|low  # SAME value as evidence.confidence above; "
        "fill BOTH, they are one judgment (this one is what the findings table prints)\n"
        "Rules:\n"
        "- `human_corrections` are the HIGHEST-signal items in the corpus — a human overriding a "
        "safety behavior (e.g. 'NEVER submit without review') or expressing confusion ('I'm lost') "
        "matters MORE than any tool failure. Surface those findings FIRST, mark them high confidence, "
        "and turn a `safety_override` into a hard invariant (hook_rule), never just prose guidance.\n"
        "- A `confusion` correction means the agent's turn structure/communication failed — recommend "
        "a skill_edit that fixes how it presents (e.g. decide-then-show, not ask-then-show-something-else).\n"
        "- `dispatch_outcome` is present only when a MACHINE started the session, and it grades the "
        "agent that SENT the brief, not the one being reviewed here. A `contested` verdict is the "
        "strongest signal in the corpus: work dispatched to run unattended that a human had to come "
        "back and argue with. Report it against the DISPATCHER — name it in the finding — and treat "
        "the intervention quotes as evidence about the BRIEF (wrong, stale, over-scoped, or sent "
        "without the receiving agent having what it needed), not about this agent's turn loop. "
        "`shipped_clean` is the opposite and is worth saying out loud: a dispatch that ran "
        "unattended and landed is evidence the sender's judgment was good, and a fleet that only "
        "ever surfaces failures teaches its conductor nothing about what to keep doing.\n"
        "- a `skill_collisions` entry means a generic skill NAME (turn/architect/…) resolved to "
        "ANOTHER plugin's skill (e.g. `ace:turn`) instead of the agent's own — recommend a "
        "skill_edit/claude_update that namespaces the agent's skill or forces reading it from disk, "
        "so the agent never silently runs a sibling's procedure.\n"
        "- prefer hook_rule for any 'never do X' invariant; prefer new_skill/skill_edit when a manual "
        "multi-step pattern repeats; only include findings with real evidence in the corpus.\n"
        "- an invariant ('never/always do X') finding MUST use hook_rule or schema_validator — a "
        "skill_edit/claude_update for an invariant WILL BE DROPPED.\n"
        "Output ONLY the YAML list.\n"
    )


def parse_findings(output: str) -> list[dict]:
    import yaml
    text = output.strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```"))
    try:
        result = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    return result if isinstance(result, list) else []


_CONF_LEVELS = {"high", "medium", "low"}

# Same class of defect as the fix_kind rail below, same remedy: `confidence` is a
# ROUTING/METADATA label, and when the model writes it wrong the evidence underneath
# is still sound. Dropping the finding throws away the expensive half (a read source,
# a ran already_fixed_check, a written basis) to punish the cheap half (one enum word).
#
# Measured on the 2026-08-18 cycle: `canopy agent-review ace --hours 24` over 13 turns
# synthesized FIVE findings and dropped ALL FIVE on this gate, so the run reported
# "No findings synthesized" — a clean bill of health on the agent with the loudest
# human-correction signal in the fleet that day. The titles matched Jonathan's own
# verbatim corrections in the same window. A validator that turns 5 findings into 0
# is not enforcing rigor, it is manufacturing silence.
#
# Two failure modes hide behind the ONE message the old gate emitted:
#   (a) the value is a near-miss of the enum ("very high", "Medium", 0.8, 85)
#   (b) the key is ABSENT from `evidence` because the model satisfied the OTHER
#       `confidence` the synthesis prompt asks for — the finding-level one. The prompt
#       requests `confidence: high|medium|low` in BOTH places, so filling exactly one
#       is the predictable half-compliance, not missing evidence.
# (a) is repaired by mapping; (b) is repaired by reading the sibling the model DID
# fill. Only when NEITHER level carries a usable value is the finding dropped —
# that is genuinely absent evidence, and `confidence_basis` stays hard-gated
# regardless, so an unjustified level can never be laundered into a level.
#
# The drop reasons are also split apart, because the single old message made (a) and
# (b) indistinguishable in the log: the 2026-08-18 stderr printed the reason but never
# the offending VALUE, so nobody could tell a mislabel from an omission without
# re-running. `_confidence_coerced` records `from` verbatim to close that for good.
_CONF_ALIASES = {
    # near-misses of "high"
    "very high": "high", "veryhigh": "high", "vhigh": "high", "highest": "high",
    "certain": "high", "strong": "high", "confident": "high", "h": "high",
    # near-misses of "medium"
    "med": "medium", "moderate": "medium", "mid": "medium", "middle": "medium",
    "average": "medium", "m": "medium", "medium-high": "medium", "medium-low": "medium",
    # near-misses of "low"
    "weak": "low", "tentative": "low", "speculative": "low", "uncertain": "low",
    "lowest": "low", "l": "low", "very low": "low", "unsure": "low",
}


def normalize_confidence(raw: object) -> tuple[str | None, str]:
    """Map a model-written confidence label onto _CONF_LEVELS.

    Returns (level, reason). `level` is None when nothing usable was written —
    the caller then falls back to the sibling field, and finally drops.
    `reason` describes the mapping for the coercion record / drop log.

    Deliberately conservative: an unrecognized-but-present label bands to "low"
    rather than "high", so a repair can never INFLATE how much a finding is
    trusted. The fix_kind rail makes the same trade — repair the label, keep the
    evidence, and annotate loudly enough that a human can audit the repair.
    """
    if isinstance(raw, bool):  # bool is an int; catch it before the numeric branch
        return None, "confidence was a boolean, not a level or a score"
    if isinstance(raw, (int, float)):
        # Two scales appear in practice: 0-1 probabilities and 0-100 percentages.
        v = float(raw)
        pct = v * 100 if 0.0 <= v <= 1.0 else v
        if not 0.0 <= pct <= 100.0:
            return None, f"numeric confidence {raw!r} outside 0-1 and 0-100 scales"
        band = "high" if pct >= 80 else "medium" if pct >= 50 else "low"
        return band, f"numeric confidence {raw!r} banded to {band!r}"
    if not isinstance(raw, str):
        return None, f"confidence must be a string level or a score, got {type(raw).__name__}"
    # Normalize case, surrounding whitespace/punctuation, and inner separators, so
    # "Very_High", " HIGH. ", and "very-high" all land on the same key.
    k = re.sub(r"[\s_-]+", " ", raw.strip().strip(".,;:!\"'").lower()).strip()
    if not k:
        return None, "confidence is empty"
    if k in _CONF_LEVELS:
        return k, ""  # already valid — no coercion
    if k in _CONF_ALIASES:
        return _CONF_ALIASES[k], f"{raw!r} is a near-miss of the enum"
    # A bare number that arrived as a string ("0.9", "85%").
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%?", k)
    if m:
        return normalize_confidence(float(m.group(1)) / (100 if "%" in k else 1))[0], (
            f"numeric-string confidence {raw!r} banded"
        )
    # Present but unrecognized: band DOWN, never up.
    return "low", f"unrecognized confidence {raw!r} banded down to 'low' (conservative default)"


# Structural-fix-only rail: an "invariant" finding (a hard never/always rule, or one
# born from a human safety_override correction) must ship as a structural fix
# (hook_rule / schema_validator) — a skill_edit/claude_update is prose, and prose
# relies on the model choosing to comply, which is exactly what an invariant can't
# rely on (see CLAUDE.md's "Invariants are hooks, not memory").
#
# This rail used to DROP such a finding. That threw away the expensive half (the
# evidence) to punish the cheap half (a label). The synthesis prompt already tells
# the model "an invariant finding MUST use hook_rule or schema_validator" — so when
# it writes `skill_edit` anyway, the finding isn't wrong, its ROUTING is, and the
# routing is the one part we can fix ourselves. Deterministically. Measured on the
# 2026-08-11 cycle: eva twice, hal once, each losing exactly one evidence-valid
# finding per run this way — including eva's recurring Goals-sheet permission
# failure and hal's global-CLI resolution bug.
#
# So the fix-kind branch REPAIRS instead of discarding: coerce to the structural kind
# and annotate it (`_fix_kind_coerced`) so the triager sees both the correction and
# what the model originally proposed. `fix_kind` is a triage BIAS hint consumed by a
# human/agent in the agent-review skill's Step 2 table — not a machine-executed
# dispatch — so steering it is exactly the nudge the rail wanted, and the preserved
# `recommendation` keeps the model's own intent readable next to it.
#
# The EVIDENCE gate above stays a hard drop: unevidenced is unfixable, wrongly-routed
# is not.
_STRUCTURAL_FIX_KINDS = {"hook_rule", "schema_validator"}
_INVARIANT_RX = re.compile(r"\b(never|always|must not|do not)\b", re.IGNORECASE)
# A schema-shaped target gets schema_validator; everything else gets hook_rule, which
# is what the prompt itself prefers ("prefer hook_rule for any 'never do X' invariant").
# Deliberately narrow — matching on file EXTENSION would misroute `config/gating.json`,
# which is a hook-rule config, not a schema.
_SCHEMA_TARGET_RX = re.compile(r"schema", re.IGNORECASE)


def _structural_fix_kind_for(finding: dict) -> str:
    """The structural fix_kind an invariant finding should ship as, given its target."""
    target = finding.get("target")
    if isinstance(target, str) and _SCHEMA_TARGET_RX.search(target):
        return "schema_validator"
    return "hook_rule"


def _is_invariant(finding: dict) -> bool:
    """True if a finding describes a hard invariant rather than an ordinary
    improvement — either it was mined from a human safety_override correction, or
    its title/recommendation uses never/always/must-not/do-not phrasing."""
    if finding.get("friction_type") == "safety_override":
        return True
    blob = f"{finding.get('title') or ''} {finding.get('recommendation') or ''}"
    return bool(_INVARIANT_RX.search(blob))


def _valid_evidence(ev: object) -> tuple[bool, str]:
    """A finding's evidence must be a machine-checkable record, not free text.
    Returns (ok, reason). Reason is '' when ok."""
    if not isinstance(ev, dict):
        return False, "evidence must be a record (dict), not free text"
    if not str(ev.get("source_ref") or "").strip():
        return False, "evidence.source_ref missing/empty (what source was consulted)"
    if ev.get("was_read") is not True:
        return False, "evidence.was_read must be true (the source was opened, not proxied)"
    afc = ev.get("already_fixed_check")
    if (
        not isinstance(afc, dict)
        or not isinstance(afc.get("ran"), bool)
        or not str(afc.get("result") or "").strip()
    ):
        return False, "evidence.already_fixed_check must be {ran: bool, result: <non-empty>}"
    # NOTE: `evidence.confidence` is deliberately NOT gated here. It is a label, not
    # evidence, and qualify_findings repairs it (see normalize_confidence). The basis
    # below IS gated — that is the justification, and it cannot be synthesized.
    if not str(ev.get("confidence_basis") or "").strip():
        return False, "evidence.confidence_basis missing/empty (justify the confidence)"
    return True, ""


def qualify_findings(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split findings into (qualified, dropped). A finding with no valid evidence
    record is DROPPED, annotated with `_drop_reason`. Fail-loud: the caller logs
    each drop. This is the enforcement — a finding without verified evidence cannot
    survive to be published or dispatched.

    Two labels are REPAIRED rather than dropped, because each is a routing/metadata
    word sitting on top of otherwise-sound evidence: `evidence.confidence`
    (`_confidence_coerced`) and `fix_kind` (`_fix_kind_coerced`). Both repairs are
    annotated on the finding and logged by `_qualify_and_log`, so a silent fix stays
    visible and auditable."""
    qualified: list[dict] = []
    dropped: list[dict] = []
    for f in findings:
        if not isinstance(f, dict):
            dropped.append({"_drop_reason": "finding is not a record (dict)", "_raw": f})
            continue
        ok, reason = _valid_evidence(f.get("evidence"))
        if not ok:
            f["_drop_reason"] = reason
            dropped.append(f)
            continue
        # Confidence rail: repair the label, keep the evidence. Try the nested value
        # first, then the finding-level sibling the synthesis prompt also asks for —
        # the model routinely fills exactly one of the two. See normalize_confidence.
        ev = f["evidence"]
        raw = ev.get("confidence")
        level, why = normalize_confidence(raw)
        source = "evidence.confidence"
        if level is None:
            level, why = normalize_confidence(f.get("confidence"))
            source = "finding.confidence"
            if level is None:
                f["_drop_reason"] = (
                    "no usable confidence at evidence.confidence or finding.confidence "
                    f"(evidence.confidence={raw!r}, finding.confidence="
                    f"{f.get('confidence')!r}) — expected one of "
                    f"{sorted(_CONF_LEVELS)}, an alias, or a 0-1/0-100 score"
                )
                dropped.append(f)
                continue
        # Record a coercion whenever the stored value is not literally what the model
        # wrote in `evidence.confidence` — that covers a mapped alias, a banded score,
        # a bare case/whitespace normalization ("HIGH" -> "high"), and a value lifted
        # from the finding-level sibling. Keying off `why` alone would let those last
        # two through silently, which is the failure this rail exists to stop.
        changed = level != raw or source != "evidence.confidence"
        ev["confidence"] = level
        # The finding-level `confidence` is a SECOND consumer of the same judgment: the
        # gate reads `evidence.confidence`, but the agent-review display reads
        # `finding.confidence` (cli.py's findings table). Nothing normalized that one, so
        # a repaired finding could still PRINT the raw label it was repaired away from —
        # or print `banana`. One resolved level, written to both, keeps the table honest
        # and removes the divergence the duplicated prompt field invites.
        f["confidence"] = level
        if changed:
            f["_confidence_coerced"] = {
                "from": raw,
                "to": level,
                "source": source,
                "reason": (
                    (why or f"normalized to the {level!r} enum member")
                    + "; coerced rather than dropped so the evidence survives — "
                    "confidence_basis was still required and passed."
                ),
            }
        # Structural-fix-only rail: an evidence-valid invariant finding whose fix isn't
        # structural (hook_rule/schema_validator) is REPAIRED, not discarded — the
        # evidence is sound, only the routing label is wrong, and the label is the part
        # we can fix deterministically. See the _STRUCTURAL_FIX_KINDS note above.
        fk = f.get("fix_kind")
        if _is_invariant(f) and (not isinstance(fk, str) or fk not in _STRUCTURAL_FIX_KINDS):
            coerced_to = _structural_fix_kind_for(f)
            f["fix_kind"] = coerced_to
            f["_fix_kind_coerced"] = {
                "from": fk,
                "to": coerced_to,
                "reason": (
                    "invariant finding must ship as a structural fix "
                    f"(hook_rule/schema_validator), not {fk!r} — coerced rather than "
                    "dropped so the evidence survives; confirm the target fits."
                ),
            }
        qualified.append(f)
    return qualified, dropped


def _qualify_and_log(findings: list[dict], label: str) -> list[dict]:
    """Run findings through qualify_findings and log every drop to stderr (fail-loud,
    not fail-silent) before returning only the qualified ones. A fix-kind coercion is
    logged the same way — a silent repair is just a different kind of quiet."""
    qualified, dropped = qualify_findings(findings)
    for d in dropped:
        print(f"[agent-review:{label}] dropped finding "
              f"{d.get('title')!r}: {d.get('_drop_reason')}", file=sys.stderr)
    for q in qualified:
        cc = q.get("_confidence_coerced")
        if cc:
            print(f"[agent-review:{label}] coerced confidence on finding "
                  f"{q.get('title')!r}: {cc.get('from')!r} \u2192 {cc.get('to')!r} "
                  f"(via {cc.get('source')}; kept, not dropped)", file=sys.stderr)
        c = q.get("_fix_kind_coerced")
        if c:
            print(f"[agent-review:{label}] coerced fix_kind on finding "
                  f"{q.get('title')!r}: {c.get('from')!r} → {c.get('to')!r} "
                  "(invariant must ship structurally; kept, not dropped)",
                  file=sys.stderr)
    return qualified


# --- Source-verification gate (enforced) -------------------------------------
# The recurring failure mode this closes: agent-review reads STALE transcripts, so
# a finding can describe friction that a LATER commit already fixed — the review
# window overlaps the very cycle that shipped the fix. Surfacing (or dispatching)
# such a finding wastes a turn and erodes trust (it happened two days running:
# eva's chrome-sales-SA fix was already in `gsp-daily-briefing` as `as:eva@…`, yet
# got re-surfaced). So every finding is re-checked against the agent repo's CURRENT
# origin/main BEFORE run_review returns it, and the already-shipped ones are dropped.
# This runs by DEFAULT — the operator can't forget it (enforcement, not a checklist
# step the model has to remember under load).
#
# Reuses the FRAMEWORK-tier repo-evidence helpers so the "is it in main?" evidence
# gathering stays one implementation (shared with the proposals verify path) WITHOUT
# a framework→product import (agent_review is framework; verify_findings is product).
from orchestrator.repo_evidence import (  # noqa: E402
    SYMBOL_RX as _SYMBOL_RX,
    changelog_head as _changelog_head,
    git_log_recent as _git_log_recent,
    grep_repo as _grep_repo,
)


def _finding_symbols(findings: list[dict]) -> list[str]:
    """Concrete tokens to grep the current tree for: backtick-quoted identifiers in
    a finding's text PLUS bare file paths named in `target` (which are usually not
    backticked). These are what the verdict LLM checks presence of."""
    out: set[str] = set()
    for f in findings:
        if not isinstance(f, dict):
            continue
        for field in ("title", "recommendation", "evidence", "target"):
            for m in _SYMBOL_RX.finditer(str(f.get(field) or "")):
                sym = m.group(1).strip()
                if 2 <= len(sym) <= 80:
                    out.add(sym)
        for tok in re.split(r"[,\s]+", str(f.get("target") or "")):
            tok = tok.strip().strip("`()")
            if tok and "/" in tok and len(tok) <= 80:
                out.add(tok)
    return sorted(out)[:30]


def build_verify_corpus(repo: Path, findings: list[dict], since: str = "21 days ago") -> dict:
    """Current-source evidence for the verdict pass: recent origin/main commits, the
    CHANGELOG head, and a grep of the tree for each finding's symbols/targets."""
    return {
        "commits": _git_log_recent(repo, since=since) or "(no commits in window)",
        "changelog": _changelog_head(repo) or "(no CHANGELOG.md)",
        "grep_results": _grep_repo(repo, _finding_symbols(findings)) or "(no symbols extracted)",
    }


def build_verify_prompt(repo: Path, findings: list[dict], corpus: dict) -> str:
    """Ask the model, per finding, whether the CURRENT source already does what the
    recommendation asks. Conservative by construction: `shipped` only on concrete
    evidence; otherwise `unverifiable` (which is KEPT)."""
    minimal = [
        {
            "index": i,
            "title": f.get("title"),
            "target": f.get("target"),
            "recommendation": (str(f.get("recommendation") or ""))[:400],
            "evidence": (str(f.get("evidence") or ""))[:300],
        }
        for i, f in enumerate(findings) if isinstance(f, dict)
    ]
    return (
        "You are canopy's SOURCE-VERIFICATION GATE for agent self-improvement findings.\n"
        f"Each finding below was synthesized from STALE turn transcripts of the agent at {repo}; "
        "its review window overlaps the cycle that may already have shipped the fix. For EACH "
        "finding, decide whether the friction it describes is ALREADY FIXED in the agent repo's "
        "CURRENT origin/main. Read the recommendation, then weigh the evidence below.\n\n"
        f"RECENT COMMITS (origin/main):\n{corpus['commits']}\n\n"
        f"CHANGELOG head:\n{corpus['changelog']}\n\n"
        f"GREP of the current tree for the findings' symbols/targets:\n{corpus['grep_results']}\n\n"
        f"FINDINGS:\n{json.dumps(minimal, indent=2)}\n\n"
        "Output a YAML list, one item per finding:\n"
        "  - index: <the finding's index>\n"
        "  - verdict: one of [shipped, live, unverifiable]\n"
        "      shipped = current source ALREADY does what the recommendation asks (it will be DROPPED)\n"
        "      live = the friction still exists in current source (KEPT)\n"
        "      unverifiable = target isn't in this repo, or evidence is insufficient (KEPT)\n"
        "  - evidence: ONE sentence citing the commit / file / grep line that decides it\n"
        "Be CONSERVATIVE: say `shipped` ONLY when the evidence concretely shows the fix is present. "
        "The fix can take any form — a config rail, a skill line, a shared-engine flag — so absence "
        "of one specific mechanism is NOT proof it's unfixed; judge whether the RECOMMENDATION's "
        "intent is already satisfied. When unsure, say `unverifiable`. Output ONLY the YAML list.\n"
    )


def _call_verify_llm(prompt: str, model: str, max_budget_usd: float,
                     timeout: int | None = VERIFY_TIMEOUT) -> tuple[list[dict] | None, str | None]:
    """Run the verdict pass. Returns (verdicts, error). `error` is None on success and
    a human-readable reason on any failure — a silent None-on-fail gate is worse than
    no gate (it presents UNVERIFIED findings as if they'd been checked), so every
    failure path names itself and the caller surfaces it LOUDLY."""
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", model,
             "--max-budget-usd", str(max_budget_usd), "--no-session-persistence"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"verify pass timed out after {timeout}s"
    except (subprocess.SubprocessError, OSError) as exc:
        return None, f"verify pass subprocess error: {exc}"
    if proc.returncode != 0:
        # claude -p prints some errors (e.g. budget) to STDOUT with empty stderr.
        detail = (proc.stderr.strip() or proc.stdout.strip())[:200]
        return None, f"verify pass claude -p exited {proc.returncode}: {detail}"
    verdicts = parse_findings(proc.stdout)  # same tolerant YAML-list parser
    if not verdicts:
        return None, f"verify pass output did not parse to a YAML list (head: {proc.stdout[:120]!r})"
    return verdicts, None


def verify_findings_against_source(
    repo: Path,
    findings: list[dict],
    *,
    model: str = "sonnet",
    max_budget_usd: float = 2.0,
    since: str = "21 days ago",
    verdict_fn=None,
) -> tuple[list, list, str | None]:
    """Drop findings whose fix is ALREADY in the agent repo's current origin/main.

    Returns (kept, dropped, error). Every finding — kept or dropped — is annotated with
    a `verification` block ({verdict, evidence}) so the judgment is auditable. FAIL-OPEN
    BUT LOUD: on any verification failure (LLM error, empty/parse-miss output) ALL
    findings are KEPT unchanged AND `error` is set to the reason — the caller must
    surface it so nobody mistakes "gate couldn't run" for "gate passed everything".
    """
    real = [f for f in findings if isinstance(f, dict)]
    if not real:
        return list(findings), [], None
    # Fetch so the corpus reflects the true current main, not a stale local ref.
    try:
        subprocess.run(["git", "-C", str(repo), "fetch", "origin", "main"],
                       capture_output=True, timeout=15)
    except (subprocess.SubprocessError, OSError):
        pass
    corpus = build_verify_corpus(repo, real, since=since)
    prompt = build_verify_prompt(repo, real, corpus)
    if verdict_fn is not None:
        verdicts = verdict_fn(prompt)
        error = None if verdicts else "verdict_fn returned no verdicts"
    else:
        verdicts, error = _call_verify_llm(prompt, model, max_budget_usd)
    if not verdicts:
        return list(findings), [], error  # fail-open, but WITH a reason

    by_index: dict[int, dict] = {}
    for v in verdicts:
        if isinstance(v, dict) and v.get("index") is not None:
            try:
                by_index[int(v["index"])] = v
            except (TypeError, ValueError):
                continue

    kept: list = [f for f in findings if not isinstance(f, dict)]  # preserve any junk entries
    dropped: list = []
    for i, f in enumerate(real):
        v = by_index.get(i) or {}
        verdict = v.get("verdict", "unverifiable")
        annotated = {**f, "verification": {"verdict": verdict, "evidence": v.get("evidence")}}
        (dropped if verdict == "shipped" else kept).append(annotated)
    return kept, dropped, None


def run_review(
    slug_or_path: str,
    *,
    hours: int = 168,
    use_llm: bool = True,
    verify: bool = True,
    model: str = "sonnet",
    max_budget_usd: float = 2.0,
    projects_dir: Path | None = None,
    timeout: int | None = SYNTHESIS_TIMEOUT,
) -> dict:
    """Review an agent's recent turns. Returns {agent, repo, turns, signals, corpus,
    findings, dropped_findings, error?}. `verify` (default on) runs the
    source-verification gate over the synthesized findings and drops the ones already
    shipped to origin/main. `timeout` caps the claude -p synthesis pass (seconds).

    Corpus: with no `projects_dir`, scans EVERY readable source `session_sources()`
    returns and merges them — the cross-user fix `agent_coverage.coverage_report`
    already made (see `session_sources`'s module docstring). Passing `projects_dir`
    scans only that dir, keeping the seam injectable for tests and single-root
    callers. `corpus.confidence` is `half-blind` whenever a source is unreadable, so
    a partial review says so instead of passing for a complete one.

    CONTRACT — the result is ALWAYS well-formed, on every path including failure:
    `findings` and `dropped_findings` are lists (empty on failure, never absent),
    `turns` is an int, and `error`, when present, is a non-empty descriptive STRING.
    Consumers may `len()` / substring-match `error` without type-checking it first."""
    repo = resolve_agent_repo(slug_or_path)
    if not repo or not repo.exists():
        # Well-formed even here: a bare {"error": ...} makes consumers KeyError on findings.
        return {
            "agent": str(slug_or_path),
            "repo": "",
            "turns": 0,
            "signals": [],
            "corpus": {"confidence": "half-blind", "sources": [], "unreadable": []},
            "findings": [],
            "dropped_findings": [],
            "error": f"could not resolve agent repo for {slug_or_path!r}",
        }

    if projects_dir is not None:
        # An explicitly named dir scans ONLY that dir — a caller who says where to
        # look must not get a silent fan-out across every account on the machine.
        transcripts = find_turn_transcripts(repo, hours=hours, projects_dir=projects_dir)
        corpus_meta = {
            "confidence": "whole-corpus",
            "sources": [str(projects_dir)],
            "unreadable": [],
        }
    else:
        sources = session_sources()
        seen: set[str] = set()
        transcripts = []
        for d in local_transcript_dirs(sources):
            for t in find_turn_transcripts(repo, hours=hours, projects_dir=d):
                # Sources can overlap (a symlinked or duplicated root); dedupe on the
                # resolved path so one turn isn't counted — or reviewed — twice.
                key = str(Path(t).resolve())
                if key not in seen:
                    seen.add(key)
                    transcripts.append(t)
        corpus_meta = {
            "confidence": corpus_confidence(sources),
            "sources": [s.name for s in sources if s.readable],
            "unreadable": [s.name for s in sources if not s.readable],
        }

    skills_dir = repo / "skills"
    own_skills = frozenset(
        p.name for p in skills_dir.iterdir() if p.is_dir()
    ) if skills_dir.is_dir() else frozenset()
    corpus = [friction_signals(t, own_skills=own_skills) for t in transcripts]
    result = {
        "agent": repo.name,
        "repo": str(repo),
        "turns": len(corpus),
        "signals": corpus,
        "corpus": corpus_meta,
        "findings": [],
        "dropped_findings": [],
    }
    if not corpus or not use_llm:
        return result

    prompt = build_review_prompt(repo, corpus)
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", model,
             "--max-budget-usd", str(max_budget_usd), "--no-session-persistence"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result["error"] = _error_text(
            f"agent-review synthesis pass timed out after {timeout}s",
            "agent-review synthesis pass timed out",
        )
        result["findings"] = []
        result["dropped_findings"] = []
        return result
    except (subprocess.SubprocessError, OSError) as exc:
        # e.g. `claude` not on PATH under cron. Previously this propagated out of
        # run_review and took the whole caller down instead of reporting a finding-less run.
        result["error"] = _error_text(
            f"agent-review synthesis pass subprocess error: {exc}",
            "agent-review synthesis pass subprocess error",
        )
        result["findings"] = []
        result["dropped_findings"] = []
        return result
    if proc.returncode == 0:
        findings = parse_findings(proc.stdout)
        # ENFORCED evidence gate: drop findings whose evidence isn't a complete, valid
        # record (source_ref/was_read/already_fixed_check/confidence/confidence_basis)
        # BEFORE the source-verification gate below ever sees them.
        findings = _qualify_and_log(findings, label=repo.name)
        # ENFORCED source gate: drop findings a later commit already shipped, BEFORE
        # they're returned. Fails open (keeps everything) if it can't verify.
        if verify and findings:
            kept, dropped, verify_error = verify_findings_against_source(
                repo, findings, model=model, max_budget_usd=max(max_budget_usd, 1.5),
            )
            result["findings"] = kept
            result["dropped_findings"] = dropped
            if verify_error:
                result["verification_error"] = _error_text(
                    verify_error, "source-verification gate failed for an unstated reason")
        else:
            result["findings"] = findings
    else:
        # claude -p prints some errors (e.g. "Exceeded USD budget") to STDOUT with an
        # empty stderr — capture whichever stream has the message so failures stay
        # diagnosable. Both streams can be empty (e.g. SIGKILL), so the exit code is
        # always named: a bare returncode is not an error MESSAGE.
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        result["error"] = _error_text(
            f"agent-review synthesis pass: claude -p exited {proc.returncode}"
            + (f": {detail[:200]}" if detail else " with no output"),
            f"agent-review synthesis pass: claude -p exited {proc.returncode}",
        )
        result["findings"] = []
        result["dropped_findings"] = []
    return result
