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
from orchestrator.transcripts import (
    extract_assistant_text,
    extract_tool_calls,
    extract_user_messages,
    read_transcript,
)

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Wall-clock budgets for the two claude -p passes. Named constants, not magic numbers
# buried at the call site, so the CLI can expose them and tests can assert the default.
SYNTHESIS_TIMEOUT = 180   # `run_review`'s synthesis pass
VERIFY_TIMEOUT = 300      # `_call_verify_llm`'s source-verification pass


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
    # confusion — the agent's output didn't make sense to the human
    ("confusion", re.compile(
        r"\bi['’ ]?m lost\b|\bi am lost\b|\bconfus|why are you|why did you|what are you doing|"
        r"that['’]s not what i|does ?n['’]t make sense|makes no sense|i don['’]t (?:get|follow|understand)", re.I)),
    # strong correction — a forceful "no, do it differently"
    ("strong_correction", re.compile(
        r"\bstop\b|\byou['’]re wrong\b|that['’]s wrong|that is wrong|^\s*no[,.! ]|"
        r"\binstead of\b|not what i (?:asked|wanted|meant)|\bredo\b|\bundo\b", re.I)),
)


def human_corrections(entries: list[dict]) -> list[dict]:
    """Mine the HUMAN side of a turn for corrections/overrides/confusion — the highest-signal
    friction. A forceful safety correction ("NEVER submit without review") matters more than ten
    git errors, but the mechanical signals miss it entirely. Returns [{kinds, quote}]."""
    out: list[dict] = []
    for m in extract_user_messages(entries):
        s = (m or "").strip()
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
DEFAULT_TURN_STEPS = (
    ("preflight", (r"preflight", r"readiness")),
    ("self-review", (r"self-review", r"self review")),
    ("skill-self-check", (r"skill.?self.?check", r"did i (create|improve) a skill",
                          r"should be a skill")),
    ("workspace-refresh", (r"agent-publish", r"/agents/")),
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
        if _ERROR_MARKERS.search(head):
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
        "  - confidence: high|medium|low\n"
        "Rules:\n"
        "- `human_corrections` are the HIGHEST-signal items in the corpus — a human overriding a "
        "safety behavior (e.g. 'NEVER submit without review') or expressing confusion ('I'm lost') "
        "matters MORE than any tool failure. Surface those findings FIRST, mark them high confidence, "
        "and turn a `safety_override` into a hard invariant (hook_rule), never just prose guidance.\n"
        "- A `confusion` correction means the agent's turn structure/communication failed — recommend "
        "a skill_edit that fixes how it presents (e.g. decide-then-show, not ask-then-show-something-else).\n"
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

# Structural-fix-only rail: an "invariant" finding (a hard never/always rule, or one
# born from a human safety_override correction) must ship as a structural fix
# (hook_rule / schema_validator) — a skill_edit/claude_update is prose, and prose
# relies on the model choosing to comply, which is exactly what an invariant can't
# rely on (see CLAUDE.md's "Invariants are hooks, not memory"). qualify_findings
# drops any invariant finding that isn't structural, right after the evidence check.
_STRUCTURAL_FIX_KINDS = {"hook_rule", "schema_validator"}
_INVARIANT_RX = re.compile(r"\b(never|always|must not|do not)\b", re.IGNORECASE)


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
    conf = ev.get("confidence")
    if not isinstance(conf, str) or conf not in _CONF_LEVELS:
        return False, f"evidence.confidence must be one of {sorted(_CONF_LEVELS)}"
    if not str(ev.get("confidence_basis") or "").strip():
        return False, "evidence.confidence_basis missing/empty (justify the confidence)"
    return True, ""


def qualify_findings(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split findings into (qualified, dropped). A finding with no valid evidence
    record is DROPPED, annotated with `_drop_reason`. Fail-loud: the caller logs
    each drop. This is the enforcement — a finding without verified evidence cannot
    survive to be published or dispatched."""
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
        # Structural-fix-only rail: an evidence-valid invariant finding still gets
        # dropped if its fix isn't structural (hook_rule/schema_validator) — a
        # skill_edit/claude_update can't enforce a "never/always" rule reliably.
        fk = f.get("fix_kind")
        if _is_invariant(f) and (not isinstance(fk, str) or fk not in _STRUCTURAL_FIX_KINDS):
            f["_drop_reason"] = (
                "invariant finding must ship as a structural fix "
                f"(hook_rule/schema_validator), not {f.get('fix_kind')}"
            )
            dropped.append(f)
            continue
        qualified.append(f)
    return qualified, dropped


def _qualify_and_log(findings: list[dict], label: str) -> list[dict]:
    """Run findings through qualify_findings and log every drop to stderr (fail-loud,
    not fail-silent) before returning only the qualified ones."""
    qualified, dropped = qualify_findings(findings)
    for d in dropped:
        print(f"[agent-review:{label}] dropped finding "
              f"{d.get('title')!r}: {d.get('_drop_reason')}", file=sys.stderr)
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
                     timeout: int = VERIFY_TIMEOUT) -> tuple[list[dict] | None, str | None]:
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
    projects_dir: Path = CLAUDE_PROJECTS,
    timeout: int = SYNTHESIS_TIMEOUT,
) -> dict:
    """Review an agent's recent turns. Returns {agent, repo, turns, signals, findings,
    dropped_findings, error?}. `verify` (default on) runs the source-verification gate
    over the synthesized findings and drops the ones already shipped to origin/main.
    `timeout` caps the claude -p synthesis pass (seconds).

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
            "findings": [],
            "dropped_findings": [],
            "error": f"could not resolve agent repo for {slug_or_path!r}",
        }

    transcripts = find_turn_transcripts(repo, hours=hours, projects_dir=projects_dir)
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
