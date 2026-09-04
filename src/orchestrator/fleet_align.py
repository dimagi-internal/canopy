"""fleet_align — cross-agent improvement spread (the *spread* verb of the operating model).

The factory (`canopy create-agent`) stamps every agent from a shared set of templates — a `turn`
checklist, a `self-review` skill, a `config/gating.json` policy. After stamping each agent evolves
independently, so good ideas (and stale drift) don't propagate. This module compares a KNOWN
taxonomy of shared artifacts across the whole fleet, anchored on the current factory template as
ground truth, and emits typed findings:

  - DISTRIBUTE  — a better/newer version of a shared artifact exists (in the template, or a peer);
                  the laggards should adopt it. Subsumes "agent is stale vs. a newer template."
  - PROMOTE     — an artifact evolved beyond the template in >=2 agents (they converged); lift it
                  back into canopy's factory template so everyone inherits it.
  - RECONCILE   — divergence with no clear winner / a deprecated pattern to clean up.

Deterministic and offline: it reads files, extracts structural *markers*, and set-diffs them.
The optional LLM judgment layer (which decides best-of-fleet on ties and writes PR rationale)
lives elsewhere; this core is what the unit tests pin. Sibling to `agent_review` (which measures
ONE agent's friction); this spreads improvements ACROSS the fleet. FRAMEWORK tier — imports
`agent_factory` for the template baseline, never product code. See
docs/superpowers/specs/2026-07-03-fleet-align-design.md.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from orchestrator import agent_factory

# An agent repo is any repo carrying the operating model's defining primitive: the turn orchestrator.
# `config/agent.json` is only a *secondary* signal (factory-marked vs. legacy) — echo is a real
# agent but predates the factory and has no agent.json, so keying on it alone would miss echo, the
# very agent whose improvements we most want to spread.
AGENT_MARKER = Path("skills") / "turn" / "SKILL.md"

DEFAULT_BASES = (
    Path.home() / "emdash" / "repositories",
    Path.home() / "emdash-projects",
)

_SKILL_RELPATH = re.compile(r"skills/([^/]+)/SKILL\.md")


def _artifacts() -> tuple:
    """Shared-artifact taxonomy: `(name, relpath, kind)`.

    Skill artifacts are DERIVED from the factory's stamp table (`agent_factory._TEMPLATES`) rather
    than hardcoded — so a newly-stamped shared skill (e.g. `task-tracker`, added 2026-07-09) is
    auto-covered and can never silently go un-checked. Gating is added explicitly (different
    extractor). The template TEXT is looked up at call time (`_template_text`) so it always reflects
    the current factory. `kind` picks the extractor at compare time.
    """
    arts = [(m.group(1), Path(relpath), "skill")
            for relpath in getattr(agent_factory, "_TEMPLATES", {})
            if (m := _SKILL_RELPATH.fullmatch(relpath))]
    arts.append(("gating", Path("config") / "gating.json", "gating"))
    return tuple(arts)


def _template_text(relpath, kind):
    """The current factory template text for an artifact (call-time, so it tracks the factory)."""
    if kind == "gating":
        return getattr(agent_factory, "_GATING_JSON", None)
    return getattr(agent_factory, "_TEMPLATES", {}).get(str(relpath))


ARTIFACTS = _artifacts()

_NUM_STEP = re.compile(r"^\s*\d+\.\s+\*\*(.+?)\*\*", re.M)  # "1. **Re-read the request.** ..."
_HEADING = re.compile(r"^\s*#{2,}\s+(.+?)\s*$", re.M)        # "## Step 2 — process inbound"
# Step headings carry a NUMBER ("Step 4 — close the turn") that differs across agents whenever one
# inserts an extra step — so "close the turn" at Step 4 vs Step 5 must NOT read as a diff. Strip the
# "Step <N> —/:/." lead-in and compare the semantic remainder.
_STEP_PREFIX = re.compile(r"^step\s+\d+\s*[—:.\-]*\s*", re.I)
_PLACEHOLDER = re.compile(r"\{\{\s*AGENT[_A-Z]*\s*\}\}", re.I)
_WS = re.compile(r"\s+")
_DIVERGENT_OVERLAP = 0.40  # below this fraction of template markers shared → different lineage


@dataclass
class Agent:
    slug: str
    path: Path
    factory_marked: bool  # has config/agent.json


@dataclass
class Evidence:
    """A moment in a laggard's recent turn where this improvement would have helped."""
    agent: str
    session_id: str
    when: Optional[str]   # transcript date (YYYY-MM-DD), best-effort
    signal: str           # the probe that matched (e.g. "recipient/cc handling")
    excerpt: str          # short quote from the transcript


@dataclass
class Finding:
    kind: str            # distribute | promote | reconcile
    artifact: str        # taxonomy class
    reference: str       # slug that owns the better version, or "canopy-template"
    laggards: list[str]  # slugs that should adopt / clean up
    summary: str         # one line
    detail: list[str] = field(default_factory=list)  # the specific markers involved
    recency: Optional[str] = None  # last-touched date (YYYY-MM-DD) of the reference artifact
    note: Optional[str] = None     # caveat for the human/LLM gate (applicability, judgment needed)
    evidence: list = field(default_factory=list)  # list[Evidence] — sessions the change would help
    rationale: Optional[str] = None  # LLM judgment (direction confirmed + why)
    action: Optional[str] = None     # LLM's recommended concrete action

    def as_dict(self) -> dict:
        return asdict(self)


# ── discovery ─────────────────────────────────────────────────────────────────

def discover_agents(bases=DEFAULT_BASES, extra_repos=()) -> list[Agent]:
    """Find agent repos (marker = skills/turn/SKILL.md) across `bases`, plus any explicit repos."""
    seen: dict[str, Agent] = {}
    candidates: list[Path] = []
    for base in bases:
        base = Path(base)
        if base.is_dir():
            candidates.extend(sorted(p for p in base.iterdir() if p.is_dir()))
    candidates.extend(Path(r) for r in extra_repos)
    for path in candidates:
        if not (path / AGENT_MARKER).is_file():
            continue
        slug = path.name
        if slug in seen:
            continue
        seen[slug] = Agent(slug=slug, path=path, factory_marked=(path / "config" / "agent.json").is_file())
    return list(seen.values())


def _git(path, *args) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=10)
        return r.returncode, r.stdout.strip()
    except Exception:
        return 1, ""


def checkout_warnings(bases=DEFAULT_BASES, extra_repos=()) -> list[str]:
    """Offline checkout-drift check (local refs only — never fetches): a repo whose origin
    default branch carries the agent marker but whose WORKING TREE is on another branch or
    behind it is stale — or entirely invisible — to discovery, with no error. This is the
    failure mode that hid ACE (2026-07-13: primary checkout parked on a feature branch 621
    commits behind main → "Fleet (3)")."""
    warnings: list[str] = []
    candidates: list[Path] = []
    for base in bases:
        base = Path(base)
        if base.is_dir():
            candidates.extend(sorted(p for p in base.iterdir() if p.is_dir()))
    candidates.extend(Path(r) for r in extra_repos)
    seen: set[str] = set()
    for path in candidates:
        if path.name in seen or not (path / ".git").exists():
            continue
        seen.add(path.name)
        default = next((ref for ref in ("origin/main", "origin/master")
                        if _git(path, "rev-parse", "--verify", "--quiet", ref)[0] == 0), None)
        if default is None:
            continue
        if _git(path, "cat-file", "-e", f"{default}:{AGENT_MARKER.as_posix()}")[0] != 0:
            continue  # not an agent on the origin default branch — irrelevant
        _, branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        rc, behind = _git(path, "rev-list", "--count", f"HEAD..{default}")
        behind_n = int(behind) if rc == 0 and behind.isdigit() else 0
        default_name = default.split("/", 1)[1]
        if branch == default_name and not behind_n:
            continue
        state = f"on branch {branch!r}" if branch != default_name else f"on {default_name}"
        if behind_n:
            state += f", {behind_n} commit(s) behind {default}"
        warnings.append(
            f"{path.name}: agent exists on {default} but the checkout is {state} — fleet-align "
            f"reads the working tree, so this agent is stale or invisible here. Fix: "
            f"git -C {path} checkout {default_name} && git -C {path} pull --ff-only"
        )
    return warnings


# ── marker extraction ───────────────────────────────────────────────────────

def _identity_tokens(agent: Agent) -> list[str]:
    """Lowercased identity strings to blank out before comparing, so 'echo' vs 'ace' isn't a diff."""
    toks = {agent.slug.lower()}
    aj = agent.path / "config" / "agent.json"
    if aj.is_file():
        try:
            data = json.loads(aj.read_text(encoding="utf-8"))
            for key in ("name", "email"):
                v = str(data.get(key, "")).strip().lower()
                if v:
                    toks.add(v.split("@")[0])  # local-part of the mailbox, and the display name
        except (ValueError, OSError):
            pass
    return [t for t in toks if len(t) >= 2]


def _norm(text: str, tokens: list[str]) -> str:
    s = _PLACEHOLDER.sub("<agent>", text).lower()
    for t in tokens:
        s = s.replace(t, "<agent>")
    s = _WS.sub(" ", s).strip()
    return s.rstrip(".!:—- ")


def extract_skill_markers(text: str, tokens: list[str]) -> set[str]:
    """Structural markers of a skill: bolded numbered-step lead-ins + section headings."""
    out = set()
    for m in _NUM_STEP.findall(text):
        out.add(_norm(m, tokens))
    for h in _HEADING.findall(text):
        n = _STEP_PREFIX.sub("", _norm(h, tokens))  # number-agnostic: "step 4 — close" -> "close"
        if n and n != "<agent>":
            out.add("§" + n)
    return {m for m in out if m}


def extract_gating(text: str) -> dict:
    """Deny-pattern signatures + whether the deprecated `approve` list is populated."""
    try:
        cfg = json.loads(text)
    except ValueError:
        return {"deny": set(), "approve_count": 0, "parse_error": True}
    deny = {str(r.get("pattern", "")) for r in cfg.get("deny", []) if r.get("pattern")}
    return {"deny": deny, "approve_count": len(cfg.get("approve", [])), "parse_error": False}


# ── template baseline ─────────────────────────────────────────────────────────

def load_template_baseline() -> dict:
    """The current factory templates, extracted straight from agent_factory (ground truth)."""
    base = {}
    for name, relpath, kind in ARTIFACTS:
        text = _template_text(relpath, kind)
        if text is None:
            continue
        if kind == "skill":
            base[name] = extract_skill_markers(text, [])
        elif kind == "gating":
            base[name] = extract_gating(text)
    return base


# ── comparison ────────────────────────────────────────────────────────────────

def _recency(path: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(path.parent), "log", "-1", "--format=%ad", "--date=short", "--", str(path)],
            capture_output=True, text=True, timeout=8,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _compare_skill(name, agents, template_markers, per_agent) -> list[Finding]:
    findings: list[Finding] = []
    # 1. behind-template: group agents by the identical set of template markers they're MISSING.
    groups: dict[frozenset, list[str]] = {}
    divergent: list[str] = []
    for a in agents:
        markers = per_agent[a.slug]
        overlap = len(markers & template_markers) / max(1, len(template_markers))
        missing = template_markers - markers
        # A legacy agent (no config/agent.json — e.g. echo, the ANCESTOR the template was derived
        # from) is a different lineage by definition. It is never "stale vs. the template" — treat
        # it only as a PROMOTE source / harvest target, never a distribute laggard.
        if not a.factory_marked or overlap < _DIVERGENT_OVERLAP:
            divergent.append(a.slug)
        elif missing:
            groups.setdefault(frozenset(missing), []).append(a.slug)
    for missing, slugs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        findings.append(Finding(
            kind="distribute", artifact=name, reference="canopy-template", laggards=sorted(slugs),
            summary=f"{name}: {len(slugs)} agent(s) stale vs. the factory template — missing {len(missing)} step(s)",
            detail=sorted(missing),
        ))
    for slug in divergent:
        n = len(per_agent[slug])
        findings.append(Finding(
            kind="reconcile", artifact=name, reference=slug, laggards=[],
            summary=f"{name}: {slug} is a divergent lineage (low template overlap) — review by hand for ideas to PROMOTE",
            detail=sorted(per_agent[slug])[:12],
            note="Structurally unlike the template; likely predates it. Harvest good markers manually / via LLM.",
        ))
    # 2. ahead-of-template: markers >=2 non-divergent agents share that the template lacks → PROMOTE.
    counts: dict[str, list[str]] = {}
    for a in agents:
        if a.slug in divergent:
            continue
        for mk in per_agent[a.slug] - template_markers:
            counts.setdefault(mk, []).append(a.slug)
    for mk, slugs in counts.items():
        if len(slugs) >= 2:
            findings.append(Finding(
                kind="promote", artifact=name, reference=", ".join(sorted(slugs)), laggards=["canopy-template"],
                summary=f"{name}: {len(slugs)} agents converged on a step the template lacks → PROMOTE",
                detail=[mk],
            ))
    return findings


_RAIL_TOKEN = re.compile(r"[a-z][a-z0-9_-]+")


def _rail_tokens(pattern: str) -> frozenset:
    """A deny pattern's command signature: the literal word tokens, regex syntax stripped."""
    return frozenset(_RAIL_TOKEN.findall(pattern.lower()))


def _rail_covered(tmpl_pattern: str, agent_patterns) -> bool:
    """A template deny rail counts as PRESENT when some agent rail targets the same command —
    exact match, or the template's token signature is a subset of an agent pattern's. A
    deliberately SCOPED variant (e.g. ACE narrowing the raw-gog-send rail to its own identity
    because its hook fires machine-wide) is coverage, not absence — flagging it every cycle is
    the re-flag noise fleet-align's own discipline says to drop."""
    want = _rail_tokens(tmpl_pattern)
    if not want:
        return tmpl_pattern in set(agent_patterns)
    return any(want <= _rail_tokens(p) for p in agent_patterns)


def _compare_gating(name, agents, template_g, per_agent) -> list[Finding]:
    findings: list[Finding] = []
    tmpl_deny = template_g.get("deny", set())
    # deprecated approve-rules cleanup (the "rails, not gates" revision retired `approve`)
    approve_users = sorted(a.slug for a in agents if per_agent[a.slug].get("approve_count", 0) > 0)
    if approve_users:
        findings.append(Finding(
            kind="distribute", artifact=name, reference="canopy-template", laggards=approve_users,
            summary=f"gating: {len(approve_users)} agent(s) still carry deprecated `approve` rules (current standard is deny-rails-only)",
            detail=[f"{s}: {per_agent[s]['approve_count']} approve rule(s)" for s in approve_users],
            note="Per the 2026-07-01 'rails, not gates' revision, approval belongs in the turn checklist, not a hook modal.",
        ))
    # missing template deny rails
    missing_groups: dict[frozenset, list[str]] = {}
    for a in agents:
        agent_deny = per_agent[a.slug].get("deny", set())
        missing = {t for t in tmpl_deny if not _rail_covered(t, agent_deny)}
        if missing:
            missing_groups.setdefault(frozenset(missing), []).append(a.slug)
    for missing, slugs in missing_groups.items():
        findings.append(Finding(
            kind="distribute", artifact=name, reference="canopy-template", laggards=sorted(slugs),
            summary=f"gating: {len(slugs)} agent(s) missing {len(missing)} template deny rail(s)",
            detail=sorted(missing),
            note="Verify applicability — an agent without the matching channel (e.g. no email adapter) may not need the rail yet.",
        ))
    return findings


def analyze(agents: list[Agent], baseline: Optional[dict] = None) -> list[Finding]:
    """Deterministic cross-agent comparison → typed findings. No network, no LLM."""
    if baseline is None:
        baseline = load_template_baseline()
    findings: list[Finding] = []
    for name, relpath, kind in ARTIFACTS:
        present = [a for a in agents if (a.path / relpath).is_file()]
        # A factory-marked agent LACKING a template-stamped skill entirely (not just drifted) —
        # e.g. eva predating task-tracker — is a distribute laggard the marker-diff can't see (it
        # only compares agents that HAVE the file). Flag the whole-skill gap explicitly.
        if kind == "skill" and _template_text(relpath, kind):
            absent = sorted(a.slug for a in agents
                            if a.factory_marked and not (a.path / relpath).is_file())
            if absent:
                findings.append(Finding(
                    kind="distribute", artifact=name, reference="canopy-template", laggards=absent,
                    summary=f"{name}: {len(absent)} agent(s) missing the whole skill (stamped by the factory)",
                    detail=[f"add skills/{name}/SKILL.md (factory template)"],
                    note="The agent predates this shared skill being added to the factory; stamp it in.",
                ))
        if not present:
            continue
        if kind == "skill":
            per_agent = {
                a.slug: extract_skill_markers((a.path / relpath).read_text(encoding="utf-8"), _identity_tokens(a))
                for a in present
            }
            findings.extend(_compare_skill(name, present, baseline.get(name, set()), per_agent))
        elif kind == "gating":
            per_agent = {a.slug: extract_gating((a.path / relpath).read_text(encoding="utf-8")) for a in present}
            findings.extend(_compare_gating(name, present, baseline.get(name, {}), per_agent))
    # rank: promote (convergence is the strongest signal) first, then by breadth of impact
    order = {"promote": 0, "distribute": 1, "reconcile": 2}
    findings.sort(key=lambda f: (order.get(f.kind, 9), -len(f.laggards)))
    return findings


# ── reporting ─────────────────────────────────────────────────────────────────

_KIND_LABEL = {"promote": "PROMOTE ↑", "distribute": "DISTRIBUTE →", "reconcile": "RECONCILE ?"}


# ── evidence: would this improvement have helped in recent sessions? ──────────
# The user's north star: a finding is only useful if we can show recent turns where its absence
# actually cost something. Each probe ties an improvement theme to a regex over the laggards'
# recent turn transcripts (reusing agent_review's transcript discovery). Deterministic and honest:
# zero evidence is a real result (the finding is speculative → ranks low), not a failure.

# (keyword-in-finding, transcript regex, human-readable signal name)
_EVIDENCE_PROBES = (
    ("verify recipients", re.compile(r"(?i)\b(cc'?d?|bcc|reply[- ]?all|recipients?|forgot to (?:cc|copy|add))\b"), "recipient / cc handling"),
    ("recipient", re.compile(r"(?i)\b(cc'?d?|bcc|reply[- ]?all|recipients?|forgot to (?:cc|copy|add))\b"), "recipient / cc handling"),
    ("gdocs", re.compile(r"(?i)(wall of text|paste(?:d)? (?:the |it |in )?(?:draft|body|report|reply)|(?:draft|reply|deliverable) (?:is )?in a (?:local )?(?:file|\.txt))"), "pasted / local-file deliverable"),
    ("inline", re.compile(r"(?i)(wall of text|paste(?:d)? (?:the |it |in )?(?:draft|body|report|reply)|(?:draft|reply|deliverable) (?:is )?in a (?:local )?(?:file|\.txt))"), "pasted / local-file deliverable"),
    ("gmail", re.compile(r"(?i)gog\s+gmail\s+(?:send|reply)"), "raw `gog gmail send` attempt"),
    ("rate it", re.compile(r"(?i)(that'?s not what|that is wrong|not what i (?:asked|wanted|meant)|redo|doesn'?t match|you missed|wrong (?:link|report|doc))"), "faithfulness miss a tough self-review would catch"),
)


# SOURCE-side (positive) probes: evidence that the REFERENCE agent actively USES the pattern —
# "this isn't theoretical, echo runs it constantly" strengthens a PROMOTE far more than the mere
# absence of it elsewhere. Keyed by artifact; searched in the reference/divergent agent's sessions.
_SOURCE_PROBES = {
    "agent-turn-review": (re.compile(r"(?i)\b(?:agent[- ]?turn[- ]?review|self[- ]?review)\b|\bfaithfulness\b|re-?read the (?:original|request)|\brate it\b|grounded commitment|extract each (?:discrete )?ask"), "source actively runs this turn-review discipline"),
    "turn": (re.compile(r"(?i)skill[- ]?(?:development )?self[- ]?check|(?:create or improve|repeat(?:ed)? .* by hand).{0,20}skill"), "source actively runs this turn discipline"),
}


def _probe_for(finding: Finding):
    hay = (finding.summary + " " + " ".join(finding.detail)).lower()
    for key, rx, label in _EVIDENCE_PROBES:
        if key in hay:
            return rx, label
    return None


def _source_agents(finding: Finding, agents: list[Agent]) -> list[Agent]:
    """The reference/divergent agent(s) that OWN the better pattern (for source-side evidence)."""
    by_slug = {a.slug: a for a in agents}
    return [by_slug[s.strip()] for s in finding.reference.split(",") if s.strip() in by_slug]


def _turn_corpus(transcript_path: Path) -> str:
    from orchestrator.transcripts import (
        extract_assistant_text, extract_tool_calls, extract_user_messages, read_transcript,
    )
    entries = read_transcript(transcript_path)
    parts = list(extract_user_messages(entries)) + list(extract_assistant_text(entries))
    for c in extract_tool_calls(entries):
        parts.append(f"{c.get('name','')} {json.dumps(c.get('input', {}))}")
    return "\n".join(str(p) for p in parts)


def _transcript_date(path: Path) -> Optional[str]:
    try:
        import datetime
        return datetime.date.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return None


def _evidence_targets(finding: Finding, agents: list[Agent]) -> list[Agent]:
    """Which agents to search: the ones who'd ADOPT the change (and thus whose recent pain counts)."""
    by_slug = {a.slug: a for a in agents}
    if finding.kind == "promote":  # everyone lacking it benefits (reference agents already have it)
        have = {s.strip() for s in finding.reference.split(",")}
        return [a for a in agents if a.slug not in have]
    if finding.kind == "distribute":
        return [by_slug[s] for s in finding.laggards if s in by_slug]
    return []  # reconcile → harvest by hand, no evidence search


def claude_projects_roots(users_root: str = "/Users") -> tuple[list[Path], int]:
    """Every readable `~/.claude/projects` across ALL mac logins, not just the current one.

    Agents are commonly driven under a different login than the one canopy runs as (e.g. this
    machine has both `acedimagi` and `jjackson`, and eva's sessions live under the latter). Reading
    only `Path.home()` makes the evidence search half-blind. `_belongs_to_agent` already matches
    cross-user cwds via its slug checks, so we just have to point it at every login's projects dir.
    Returns (readable_roots, unreadable_count) — the count feeds the half-blind confidence flag.
    """
    import glob
    roots, unreadable = [], 0
    for home in sorted(glob.glob(str(Path(users_root) / "*"))):
        pd = Path(home) / ".claude" / "projects"
        if not pd.is_dir():
            continue
        if os.access(pd, os.R_OK):
            roots.append(pd)
        else:
            unreadable += 1
    return roots or [Path.home() / ".claude" / "projects"], unreadable


def gather_evidence(findings: list[Finding], agents: list[Agent], *, hours: int = 336,
                    projects_dir=None, per_finding: int = 3) -> int:
    """Attach recent-session evidence to each finding, in place. Searches every readable login's
    sessions (cross-user). Returns the count of unreadable user homes (0 = fully sighted)."""
    from orchestrator import agent_review
    if projects_dir is not None:          # explicit dir (tests) → single root, no cross-user scan
        roots, unreadable = [projects_dir], 0
    else:
        roots, unreadable = claude_projects_roots()
    def _search(targets, rx, label, cap):
        hits: list[Evidence] = []
        for agent in targets:
            transcripts = {}
            for root in roots:
                for tp in agent_review.find_turn_transcripts(agent.path, hours=hours, projects_dir=root):
                    transcripts.setdefault(str(tp), tp)  # dedupe: matched under >1 root counts once
            for tp in transcripts.values():
                if len(hits) >= cap:
                    break
                text = _turn_corpus(tp)
                m = rx.search(text)
                if not m:
                    continue
                s = max(0, m.start() - 60)
                hits.append(Evidence(agent=agent.slug, session_id=tp.stem, when=_transcript_date(tp),
                                     signal=label, excerpt=_WS.sub(" ", text[s:m.end() + 60]).strip()))
        return hits

    for f in findings:
        ev: list[Evidence] = []
        # beneficiary (absence) evidence — the gap plausibly cost a laggard something
        probe = _probe_for(f)
        if probe and f.kind in ("distribute", "promote"):
            ev += _search(_evidence_targets(f, agents), probe[0], probe[1], per_finding)
        # source (adoption) evidence — the reference agent actively USES the pattern (strengthens PROMOTE)
        sp = _SOURCE_PROBES.get(f.artifact)
        if sp and f.kind in ("promote", "reconcile"):
            ev += _search(_source_agents(f, agents), sp[0], sp[1], per_finding)
        f.evidence = ev
    return unreadable


# ── LLM judgment (optional; direction + rationale, evidence-aware) ─────────────

def _run_claude(prompt: str, model: str = "sonnet", timeout: int = 180) -> str:
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", model, "--no-session-persistence"],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200] or "claude -p failed")
    return proc.stdout


def build_judgment_prompt(findings: list[Finding]) -> str:
    """Assemble the fleet-alignment judge prompt.

    Inline by the framework-tier convention (#352 / `TIERS.md`): framework logic-prompts
    stay inline (static, co-located, immune to the #351 packaging class); PRODUCT
    editable templates go external via `prompts/load_prompt`. Sibling site:
    `agent_review.build_review_prompt`."""
    rows = []
    for i, f in enumerate(findings):
        ev = [f"{e.agent}/{e.session_id[:8]} ({e.when}): {e.excerpt[:160]}" for e in f.evidence]
        rows.append({
            "index": i, "kind": f.kind, "artifact": f.artifact, "reference": f.reference,
            "laggards": f.laggards, "summary": f.summary, "detail": f.detail,
            "evidence": ev,
        })
    return (
        "You are canopy's fleet-alignment judge. Each finding below is a DETERMINISTIC divergence "
        "across a fleet of factory-stamped Claude Code agents, anchored on the current factory "
        "template. `evidence` lists real recent-session moments where the change might have helped.\n\n"
        "For EACH finding decide:\n"
        "  - final_kind: distribute | promote | reconcile | drop  (drop = not worth acting on)\n"
        "  - direction_ok: is the deterministic reference/laggard direction correct? (bool)\n"
        "  - rationale: one or two sentences. Weigh the EVIDENCE — a finding with real evidence "
        "that the gap cost something is far stronger than a speculative one. Say so.\n"
        "  - action: the concrete change (e.g. 'backport self-review steps 7-8 into eva & hal').\n\n"
        "Return ONLY a JSON array of {index, final_kind, direction_ok, rationale, action}.\n\n"
        f"FINDINGS:\n{json.dumps(rows, indent=2)}\n"
    )


def judge(findings: list[Finding], *, runner=_run_claude, model: str = "sonnet") -> list[Finding]:
    """Optional LLM pass: refine kind + write rationale/action, evidence-weighted. Mutates in place."""
    if not findings:
        return findings
    try:
        raw = runner(build_judgment_prompt(findings), model)
    except (RuntimeError, subprocess.SubprocessError, OSError):
        return findings  # LLM optional — deterministic findings stand on their own
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return findings
    try:
        verdicts = json.loads(m.group(0))
    except ValueError:
        return findings
    kept: list[Finding] = []
    for f in findings:
        kept.append(f)
    for v in verdicts:
        i = v.get("index")
        if not isinstance(i, int) or not (0 <= i < len(findings)):
            continue
        f = findings[i]
        fk = v.get("final_kind")
        if fk in ("distribute", "promote", "reconcile"):
            f.kind = fk
        elif fk == "drop":
            f.kind = "drop"
        f.rationale = v.get("rationale")
        f.action = v.get("action")
    return [f for f in kept if f.kind != "drop"]


# ── change brief (deterministic spec that seeds the AI apply) ─────────────────
# We do NOT programmatically splice files. Matching canopy's own architecture (the pipeline stops
# at proposals; a Claude Code agent implements), the fleet-align SKILL dispatches an AI to make the
# surgical edit + PR with judgment — placement, identity substitution, applicability, combining
# same-file findings — none of which brittle string/JSON surgery does well. Python's job is only to
# hand that AI a precise brief: which file, what the template has that the agent lacks (the exact
# reference text), and what to remove. The edit itself is the AI's.

def _template_step_blocks(template_text: str) -> dict:
    """Map each normalized numbered-step marker → the full step text (lead line + continuations)."""
    starts = [(m.start(), _norm(m.group(1), [])) for m in _NUM_STEP.finditer(template_text)]
    blocks = {}
    for i, (pos, marker) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(template_text)
        block = template_text[pos:end].rstrip()
        h = re.search(r"\n#{1,6}\s", block)  # stop a trailing block at the next heading
        if h:
            block = block[:h.start()].rstrip()
        blocks[marker] = block
    return blocks


def change_brief(finding: Finding) -> Optional[dict]:
    """A deterministic brief for the AI that will make the edit — NOT a mechanical patch. Returns
    {target_relpath, add_reference[], remove_hint, instruction} for a distribute-from-template
    finding, or None when the change needs full human/LLM judgment (promote/reconcile)."""
    if finding.kind != "distribute" or finding.reference != "canopy-template":
        return None
    art = next((a for a in ARTIFACTS if a[0] == finding.artifact), None)
    if not art:
        return None
    _name, relpath, kind = art
    text = _template_text(relpath, kind)
    if text is None:
        return None
    add_reference, remove_hint = [], None
    if kind == "skill":
        blocks = _template_step_blocks(text)
        add_reference = [blocks[m] for m in finding.detail if m in blocks]  # exact template text of the missing steps
        instruction = ("Splice these template steps into the agent's EXISTING "
                       f"{relpath} — renumber to continue its list, preserve everything else, do NOT "
                       "regenerate the file. If a step names a channel the agent lacks, adapt or skip it.")
    elif kind == "gating":
        if "approve" in finding.summary:
            remove_hint = "the deprecated `approve` array (rails, not gates — approval lives in the turn checklist)"
            instruction = (f"Edit {relpath} to empty the `approve` array, preserving formatting, `_doc`, "
                           "and all `deny` rules. Structured JSON edit, not a rewrite.")
        else:
            add_reference = list(finding.detail)  # the missing deny patterns
            instruction = (f"Add the missing deny rail(s) to {relpath} ONLY IF this agent actually has the "
                           "matching channel (e.g. an email adapter/bin shim); substitute the agent's real "
                           "name/slug for any {{AGENT_NAME}}/{{AGENT_SLUG}} placeholders. Otherwise skip.")
    else:
        return None
    if not add_reference and not remove_hint:
        return None
    return {"target_relpath": str(relpath), "artifact": finding.artifact,
            "add_reference": add_reference, "remove_hint": remove_hint, "instruction": instruction}


def evidence_rank(findings: list[Finding]) -> list[Finding]:
    """Re-rank so findings BACKED BY EVIDENCE float up — the user's whole point."""
    order = {"promote": 0, "distribute": 1, "reconcile": 2, "drop": 9}
    return sorted(findings, key=lambda f: (-len(f.evidence), order.get(f.kind, 5), -len(f.laggards)))


def format_report(agents: list[Agent], findings: list[Finding]) -> str:
    lines = []
    fleet = ", ".join(f"{a.slug}{'' if a.factory_marked else ' (legacy)'}" for a in agents)
    lines.append(f"Fleet ({len(agents)}): {fleet}")
    lines.append("")
    if not findings:
        lines.append("No cross-agent divergence found — the fleet is aligned. ✓")
        return "\n".join(lines)
    backed = sum(1 for f in findings if f.evidence)
    lines.append(f"{len(findings)} finding(s)"
                 + (f" — {backed} backed by recent-session evidence:" if backed else ":"))
    for i, f in enumerate(findings, 1):
        lines.append("")
        ev_tag = f"  ⟨{len(f.evidence)} evidence⟩" if f.evidence else ""
        lines.append(f"{i}. [{_KIND_LABEL.get(f.kind, f.kind)}] {f.summary}{ev_tag}")
        lines.append(f"     artifact: {f.artifact}   reference: {f.reference}   → {', '.join(f.laggards) or '(none)'}")
        for d in f.detail:
            lines.append(f"       · {d}")
        if f.rationale:
            lines.append(f"     judge: {f.rationale}")
        if f.action:
            lines.append(f"     action: {f.action}")
        for e in f.evidence:
            lines.append(f"     ✎ evidence [{e.agent} {e.when or '?'}] {e.signal}: \"{e.excerpt[:140]}\"")
        if f.note:
            lines.append(f"     note: {f.note}")
    return "\n".join(lines)
