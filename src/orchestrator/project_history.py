"""Project history — reconstruct everything a person and canopy agents did on ONE
project, as one deterministic analysis that several outputs render from.

Why this exists (hal T66, 2026-10-09): Jonathan wanted "the Supply story" told
back to him and to the Connect team — every session in order, the artifacts
that actually helped, and how the agent reads his thinking — and wanted it to
work on ANY project, not as a one-off. `canopy harvest map` answered the
adjacent question badly: it selects sessions by KEYWORD ("supply" hit 842
sessions, mostly morning briefings and unrelated talks) and dates them by file
mtime (the last write, not the start). A hand-built Supply corpus showed what
does work, and this module encodes it:

* **A session belongs to a project by what it DID, not what it said.** It
  owns a project PR, edits project paths, or calls the project's MCP tools.
  A name/branch match is a *weak* signal: it nominates a candidate, it does not
  include one. Keywords alone are noise.
* **Canopy first, local transcripts only where canopy falls short.** The
  session index, per-session activity (repos, branches, PRs, edited dirs, MCP
  tools), human inputs and artifacts all come from canopy-web. A canopy session
  that merged several conversations under one task name (sessions created
  before canopy-web T74) has lost the earlier conversations' messages
  server-side; for those, and only those, the local Claude transcripts (every
  readable `/Users/*/.claude/projects`) split it back into conversations and
  supply the prompts. Every conversation records which source it came from.
* **A useful artifact needs evidence.** This module gathers the evidence
  (comments, owner views, resolved-review decisions, the human quoting the
  artifact back, the agent's own verdict at the time); whether it adds up to
  "useful" is the agent's judgment, recorded in a judgments file. `render`
  refuses to call an artifact useful when no evidence exists.

Deterministic only — no LLM here. The judgment (per-conversation summaries,
which artifacts were useful, the "how the agent reads <person>" reading) lives
in the `project-history` skill, which writes a judgments file this module
renders from. One analysis (`bundle.json`), several outputs.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
import subprocess
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from orchestrator import turn_synthesis

#: canopy-web reader: ``(method, path, body) -> dict``. Injected so tests run offline.
Caller = Callable[..., dict]

DEFAULT_BASE_URL = "https://canopy.dimagi.com"
_INPUT_DOC_RX = re.compile(
    r"https://(?:docs|drive|sheets|slides)\.google\.com/[^\s)\]>\"'`,]+")


# ─────────────────────────────────────────────────────────────────── spec ──

@dataclass
class ProjectSpec:
    """What a project IS, in terms a selector can test. Parameters, not judgment.

    ``paths`` / ``exclude_paths`` are repo-relative directories
    (``connect_labs/supply_chain``). ``mcp_prefixes`` match the tool name after
    ``mcp__<server>__`` (``supply_chain_``). ``name_terms`` are the WEAK signal
    (title / task key / branch / cwd). ``include_sessions`` /
    ``adjacent_sessions`` / ``exclude_sessions`` persist the agent's calls on candidates so a re-run is
    deterministic; they take canopy session ids, local transcript stems, or
    conversation ids.
    """

    name: str
    since: str
    repo: str = ""
    remote: str = ""
    paths: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)
    mcp_prefixes: list[str] = field(default_factory=list)
    name_terms: list[str] = field(default_factory=list)
    artifact_terms: list[str] = field(default_factory=list)
    agent_project: str = ""
    person: str = ""
    until: str = ""
    mcp_min: int = 3
    branch_mentions_min: int = 3
    include_sessions: list[str] = field(default_factory=list)
    adjacent_sessions: list[str] = field(default_factory=list)
    exclude_sessions: list[str] = field(default_factory=list)
    workspaces: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "ProjectSpec":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = sorted(set(d) - known)
        if unknown:
            raise ValueError(f"unknown project spec field(s): {', '.join(unknown)}")
        if not d.get("name") or not d.get("since"):
            raise ValueError("a project spec needs at least `name` and `since`")
        spec = cls(**d)
        spec.since = _iso(spec.since)
        spec.until = _iso(spec.until) if spec.until else ""
        return spec

    @classmethod
    def from_file(cls, path: str | Path) -> "ProjectSpec":
        text = Path(path).read_text(encoding="utf-8")
        if str(path).endswith((".yaml", ".yml")):
            import yaml
            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text)
        return cls.from_dict(data)

    @property
    def terms(self) -> list[str]:
        return [t.lower() for t in (self.name_terms or [self.name])]


def _iso(value: str) -> str:
    """Normalize a date or datetime to a UTC ISO-8601 string (``Z``)."""
    v = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        v += "T00:00:00+00:00"
    dt = _parse_ts(v)
    if dt is None:
        raise ValueError(f"not a date: {value!r}")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(ts: str | None) -> Optional[_dt.datetime]:
    if not ts:
        return None
    try:
        dt = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


# ────────────────────────────────────────────────────────── path matching ──

def _strip_repo(key: str, repo: str) -> Optional[str]:
    """canopy's activity path keys look like ``connect-labs:connect_labs/x``;
    a key for ANOTHER repo is not a project path. Bare keys pass through."""
    if ":" in key and not key.startswith("/"):
        r, rest = key.split(":", 1)
        if repo and r and r != repo:
            return None
        return rest
    return key


def path_matches(path: str, specs: Iterable[str]) -> bool:
    """True when ``path`` lies inside one of ``specs`` (repo-relative dirs).

    Accepts an absolute file path (``/…/worktree/connect_labs/supply_chain/x.py``),
    a repo-relative path, or a path relative to a parent of the spec dir
    (canopy records some dirs relative to where the edit ran:
    ``supply_chain/stock`` for ``connect_labs/supply_chain``). Matching is on
    whole path segments, so ``connect_labs/supply`` never matches
    ``connect_labs/supply_chain``.
    """
    p = path.strip().lstrip("./")
    if not p:
        return False
    padded = "/" + p.strip("/") + "/"
    for spec in specs:
        s = spec.strip("/")
        if not s:
            continue
        if "/" + s + "/" in padded:
            return True
        if path.startswith("/"):
            continue  # an absolute path must contain the whole spec
        segs = s.split("/")
        for k in range(1, len(segs)):
            suffix = "/".join(segs[k:])
            if p == suffix or p.startswith(suffix + "/"):
                return True
    return False


# ───────────────────────────────────────────────────────── canopy reading ──

def default_caller(base_url: Optional[str] = None, token: Optional[str] = None) -> Caller:
    from orchestrator import canopy_web

    base = base_url or os.environ.get("CANOPY_WEB_API_URL", "").strip() or _runner_base_url() \
        or DEFAULT_BASE_URL

    def _call(method: str, path: str, body=None) -> dict:
        return canopy_web.call(method, path, body, base_url=base, token=token)

    _call.base_url = base.rstrip("/")  # type: ignore[attr-defined]
    return _call


def _runner_base_url() -> str:
    try:
        d = json.loads((Path.home() / ".canopy" / "runner.json").read_text(encoding="utf-8"))
        return str(d.get("base_url") or "").rstrip("/")
    except (OSError, ValueError):
        return ""


def walk_sessions(call: Caller, *, since: str, until: str, q: str = "",
                  repo: str = "", limit: int = 500) -> list[dict]:
    """Every canopy session with last activity in [since, until), via the
    cursor walk. ``until`` freezes the set (a session active mid-walk would
    otherwise jump to the front and be skipped)."""
    out: list[dict] = []
    cursor = ""
    while True:
        params = {"limit": str(limit), "state": "all", "since": since, "until": until}
        if q:
            params["q"] = q
        if repo:
            params["repo"] = repo
        if cursor:
            params["cursor"] = cursor
        page = call("GET", "/api/canopy-sessions/search?" + urllib.parse.urlencode(params))
        out.extend(page.get("sessions") or [])
        cursor = page.get("next_cursor") or ""
        if not cursor:
            return out


def human_inputs(call: Caller, session_id: str) -> tuple[list[dict], str]:
    """All human inputs canopy holds for a session, oldest first, + their source
    (``transcript`` = durable; ``tail`` = only the runner's recent tail)."""
    out: list[dict] = []
    after = None
    source = ""
    while True:
        qs = {"limit": "500"}
        if after is not None:
            qs["after"] = str(after)
        page = call("GET", f"/api/canopy-sessions/{session_id}/human-inputs?"
                    + urllib.parse.urlencode(qs))
        source = page.get("source") or source
        out.extend(page.get("messages") or [])
        after = page.get("next_cursor")
        if after in (None, ""):
            return out, source


# ─────────────────────────────────────────────────────── project PR index ──

def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True,
                          text=True, check=True).stdout


def _remote_slug(checkout: Path) -> tuple[str, str]:
    """(remote name, owner/repo) of the checkout's first GitHub remote."""
    for name in _git(checkout, "remote").split():
        url = _git(checkout, "remote", "get-url", name).strip()
        m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", url)
        if m:
            return name, m.group(1)
    return "", ""


def project_prs(spec: ProjectSpec, checkout: Optional[Path] = None,
                runner: Callable[..., str] | None = None) -> dict[int, dict]:
    """``{number: {number, head, title, url, merged_at}}`` for every PR merged
    into the default branch since ``spec.since`` that touched a project path.

    The PR list comes from the squash-merge subjects (``… (#1234)``) of
    ``git log <remote>/main -- <paths>`` in a local checkout — exact and cheap —
    and head branches from one ``gh pr list`` call."""
    if not spec.paths:
        return {}
    if checkout is None:
        from orchestrator.repo_paths import resolve_repo_path
        checkout = resolve_repo_path(spec.repo) if spec.repo else None
    if checkout is None:
        return {}
    remote_name, slug = _remote_slug(checkout)
    slug = spec.remote or slug
    ref = f"{remote_name}/main" if remote_name else "main"
    try:
        subprocess.run(["git", "-C", str(checkout), "fetch", "-q", remote_name or "origin"],
                       capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError):
        pass
    log = _git(checkout, "log", ref, f"--since={spec.since}", "--format=%H%x09%cI%x09%s", "--",
               *spec.paths)
    prs: dict[int, dict] = {}
    for line in log.splitlines():
        sha, when, subj = (line.split("\t", 2) + ["", ""])[:3]
        m = re.search(r"\(#(\d+)\)\s*$", subj)
        if m:
            n = int(m.group(1))
            if n in prs:
                continue
            files = [f for f in _git(checkout, "show", "--name-only", "--format=", sha).splitlines()
                     if f.strip()]
            inside = sum(1 for f in files if path_matches(f, spec.paths))
            prs[n] = {"number": n, "title": re.sub(r"\s*\(#\d+\)\s*$", "", subj),
                      "merged_at": when, "head": "", "files": len(files), "files_in": inside,
                      # Mostly outside the project: it touched the project on its way
                      # to something else (a cross-cutting refactor, a shared helper).
                      "cross_cutting": bool(files) and inside * 2 < len(files),
                      "url": f"https://github.com/{slug}/pull/{n}" if slug else ""}
    if prs and slug:
        run = runner or (lambda *a: subprocess.run(a, capture_output=True, text=True,
                                                   check=True).stdout)
        try:
            raw = run("gh", "pr", "list", "-R", slug, "--state", "merged", "--limit", "3000",
                      "--search", f"merged:>={spec.since[:10]}",
                      "--json", "number,headRefName,url")
            for row in json.loads(raw or "[]"):
                if row.get("number") in prs:
                    prs[row["number"]]["head"] = row.get("headRefName") or ""
                    prs[row["number"]]["url"] = row.get("url") or prs[row["number"]]["url"]
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
    return prs


# ───────────────────────────────────────────────────── local transcripts ──

def claude_project_slug(cwd: str) -> str:
    """Claude Code's ~/.claude/projects directory name for a cwd."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def local_transcripts_for(cwds: Iterable[str], users_root: str = "/Users") -> list[Path]:
    """Every readable top-level transcript recorded under any of ``cwds``, for
    any macOS user on the machine (a rate-limit move continues the same
    worktree under another account's ~/.claude)."""
    found: dict[str, Path] = {}
    for cwd in cwds:
        if not cwd:
            continue
        pattern = os.path.join(users_root, "*", ".claude", "projects",
                               claude_project_slug(cwd), "*.jsonl")
        for f in glob.glob(pattern):
            if os.access(f, os.R_OK):
                found[f] = Path(f)
    return sorted(found.values())


def local_transcripts_by_task(project: str, session_key: str,
                              users_root: str = "/Users") -> list[Path]:
    """Transcripts of an emdash task, found by name when canopy recorded no cwd
    (activity on sessions folded before canopy-web T75 is thin). emdash names a
    task's worktree ``<root>/worktrees/<project>-<hash>/emdash-<key>-<5 chars>``,
    so the key plus the exact 5-character suffix pins it without catching a
    longer sibling key (``supply`` never matches ``supply-demo-ybqkk``)."""
    if not session_key:
        return []
    # canopy leaves `project` empty on some runner sessions; any repo then.
    proj = f"*-worktrees-{claude_project_slug(project)}-" if project else ""
    key = claude_project_slug(session_key)
    pattern = os.path.join(users_root, "*", ".claude", "projects",
                           f"{proj}*-emdash-{key}-[a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9]",
                           "*.jsonl")
    return sorted(Path(f) for f in glob.glob(pattern) if os.access(f, os.R_OK))


def _prompt_text(content: object) -> Optional[str]:
    """A human prompt from a user event's content. Strings go through the shared
    turn_synthesis filter; list content counts only when it carries text and no
    tool_result (a prompt with a pasted image is a list)."""
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text").strip()
        return turn_synthesis.clean_prompt(text) if text else None
    return turn_synthesis.clean_prompt(content)


@dataclass
class LocalScan:
    path: str
    user: str
    session_uuid: str
    start: str = ""
    end: str = ""
    cwd: str = ""
    git_branches: list[str] = field(default_factory=list)
    prompts: list[dict] = field(default_factory=list)   # [{at, text}]
    turns: list[dict] = field(default_factory=list)     # [{at, prompt, reply}]
    edits: int = 0
    edited_dirs: dict[str, int] = field(default_factory=dict)
    excluded_edits: int = 0
    mcp_calls: int = 0
    branch_mentions: dict[str, int] = field(default_factory=dict)
    assistant_texts: list[dict] = field(default_factory=list)  # [{at, text}] for artifact mentions


_TOKEN_RX = re.compile(r"[A-Za-z0-9][\w./-]*\w")


def branch_index(branches: Iterable[str]) -> frozenset[str]:
    """The project's PR head branches, as a set to count mentions against."""
    return frozenset(b for b in branches if b and b not in ("main", "master"))


def count_branch_mentions(raw: str, heads: frozenset[str]) -> dict[str, int]:
    """How often each head branch is named in ``raw``, on whole-token boundaries
    (``supply-x`` is not counted inside ``supply-x-v2``; ``origin/supply-x``
    counts). Tokenize-and-lookup: a 177-branch regex alternation was ~5x slower
    on a 95 MB transcript set."""
    if not heads:
        return {}
    out: dict[str, int] = {}
    for tok, n in _counter(_TOKEN_RX.findall(raw)).items():
        hit = tok if tok in heads else None
        if hit is None and "/" in tok:
            parts = tok.split("/")
            for i in range(1, len(parts)):
                cand = "/".join(parts[i:])
                if cand in heads:
                    hit = cand
                    break
        if hit:
            out[hit] = out.get(hit, 0) + n
    return out


def _counter(tokens: list[str]) -> dict[str, int]:
    from collections import Counter
    return Counter(tokens)


def scan_transcript(path: str | Path, spec: ProjectSpec,
                    heads: frozenset[str] = frozenset()) -> LocalScan:
    """One conversation's signals and human side, from its jsonl + its
    ``<uuid>/subagents/*.jsonl`` (most edits happen in subagents)."""
    p = Path(path)
    user = p.parts[2] if len(p.parts) > 2 and p.parts[1] == "Users" else ""
    scan = LocalScan(path=str(p), user=user, session_uuid=p.stem)
    branches: dict[str, int] = {}
    pending_prompt: Optional[dict] = None
    pending_reply = ""

    def flush():
        if pending_prompt is not None:
            scan.turns.append({"at": pending_prompt["at"], "prompt": pending_prompt["text"],
                               "reply": pending_reply})

    files = [p] + sorted(p.parent.glob(f"{p.stem}/subagents/*.jsonl"))
    for i, f in enumerate(files):
        top = i == 0
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for b, n in count_branch_mentions(raw, heads).items():
            scan.branch_mentions[b] = scan.branch_mentions.get(b, 0) + n
        for line in raw.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ts = e.get("timestamp") or ""
            if top and ts:
                scan.start = scan.start or ts
                scan.end = ts
            if top and e.get("cwd") and not scan.cwd:
                scan.cwd = e["cwd"]
            if top and e.get("gitBranch"):
                branches[e["gitBranch"]] = branches.get(e["gitBranch"], 0) + 1
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            kind = e.get("type")
            if kind == "user" and top and not e.get("isSidechain") and not e.get("isCompactSummary") \
                    and not e.get("isMeta"):
                text = _prompt_text(msg.get("content"))
                if text:
                    flush()
                    pending_prompt = {"at": ts, "text": text}
                    pending_reply = ""
                    scan.prompts.append({"at": ts, "text": text})
            elif kind == "assistant":
                if top:
                    t = turn_synthesis.assistant_text(msg)
                    if t:
                        pending_reply = t
                        scan.assistant_texts.append({"at": ts, "text": t})
                for b in msg.get("content") or []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name = b.get("name") or ""
                    inp = b.get("input") or {}
                    if name.startswith("mcp__") and spec.mcp_prefixes:
                        tool = name.split("__", 2)[-1]
                        if any(tool.startswith(px) or px in name for px in spec.mcp_prefixes):
                            scan.mcp_calls += 1
                    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                        fp = str(inp.get("file_path") or inp.get("notebook_path") or "")
                        if path_matches(fp, spec.paths) and not path_matches(fp, spec.exclude_paths):
                            scan.edits += 1
                            d = _repo_rel_dir(fp, spec.paths)
                            scan.edited_dirs[d] = scan.edited_dirs.get(d, 0) + 1
                        elif spec.exclude_paths and path_matches(fp, spec.exclude_paths):
                            scan.excluded_edits += 1
    flush()
    scan.git_branches = [b for b, _ in sorted(branches.items(), key=lambda kv: -kv[1])]
    return scan


def _repo_rel_dir(fp: str, specs: list[str]) -> str:
    for s in specs:
        i = fp.find("/" + s.strip("/") + "/")
        if i >= 0:
            return os.path.dirname(fp[i + 1:])
    return os.path.dirname(fp)


# ─────────────────────────────────────────────────────────────── selection ──

@dataclass
class Conversation:
    """The unit of the timeline: one conversation (a canopy session, or one
    local transcript split out of a merged canopy session)."""

    id: str
    canopy_session_id: str
    title: str
    source: str                  # canopy | local | local-only
    start: str
    end: str
    user: str = ""
    runner: str = ""
    workspace: str = ""
    cwd: str = ""
    transcript: str = ""
    tier: str = ""               # core | adjacent | candidate | excluded
    reasons: list[str] = field(default_factory=list)
    owned_prs: list[int] = field(default_factory=list)
    edits: int = 0
    mcp_calls: int = 0
    branches: list[str] = field(default_factory=list)
    canopy_inputs: int = 0
    local_inputs: int = 0
    canopy_input_source: str = ""
    merged_session: bool = False
    overridden: str = ""


def _weak_hits(spec: ProjectSpec, *texts: str) -> list[str]:
    hay = " ".join(t.lower() for t in texts if t)
    return [t for t in spec.terms if t and t in hay]


def classify(strong: list[str], weak: list[str], *, owned: int, edits: int, mcp: int,
             excluded_edits: int, spec: ProjectSpec) -> tuple[str, list[str]]:
    """Tier from signals. Strong (did project work) → core; a lone owned PR with
    nothing else → adjacent (a session that touched the project once on its way
    to something else); weak-only → candidate (the agent decides); edits only
    in excluded paths → excluded."""
    if strong:
        lone_pr = owned == 1 and edits == 0 and mcp < spec.mcp_min
        return ("adjacent" if lone_pr else "core"), strong + weak
    if excluded_edits:
        return "excluded", [f"edits only excluded paths ({excluded_edits})"] + weak
    if weak:
        return "candidate", weak
    return "", []


def _canopy_signals(s: dict, spec: ProjectSpec, prs: dict[int, dict], slug: str) -> dict:
    act = s.get("activity") or {}
    owned, merged = [], []
    for pr in act.get("prs") or []:
        repo = str(pr.get("repo") or "")
        if slug and repo and repo != slug:
            continue
        n = pr.get("number")
        if n in prs:
            (owned if "created" in (pr.get("actions") or []) else merged).append(n)
    edits = excl = 0
    for key, n in (act.get("paths") or {}).items():
        rel = _strip_repo(key, spec.repo)
        if rel is None:
            continue
        if path_matches(rel, spec.paths) and not path_matches(rel, spec.exclude_paths):
            edits += int(n or 0)
        elif spec.exclude_paths and path_matches(rel, spec.exclude_paths):
            excl += int(n or 0)
    mcp = sum(int(n or 0) for t, n in (act.get("mcp_tools") or {}).items()
              if any(t.split("__", 2)[-1].startswith(px) for px in spec.mcp_prefixes))
    cwds = list(act.get("cwds") or []) or ([act["cwd"]] if act.get("cwd") else [])
    return {"owned": sorted(set(owned)), "merged": sorted(set(merged)), "edits": edits,
            "excluded_edits": excl, "mcp": mcp, "cwds": cwds,
            "branches": list(act.get("branches") or [])}


def _strong(owned: list[int], edits: int, mcp: int, spec: ProjectSpec) -> list[str]:
    out = []
    if owned:
        out.append(f"owns {len(owned)} project PR(s)")
    if edits:
        out.append(f"edits project paths ({edits})")
    if mcp >= spec.mcp_min:
        out.append(f"calls project MCP tools ({mcp})")
    return out


def _tier(owned: list[int], edits: int, mcp: int, excluded_edits: int, weak: list[str],
          spec: ProjectSpec, prs: dict[int, dict]) -> tuple[str, list[str]]:
    """classify(), with owned PRs split: a cross-cutting PR (most of its files
    outside the project) is only a weak signal — it touched the project on its
    way somewhere else."""
    own = [n for n in owned if not (prs.get(n) or {}).get("cross_cutting")]
    cross = [n for n in owned if n not in own]
    weak = list(weak) + ([f"owns {len(cross)} cross-cutting PR(s)"] if cross else [])
    return classify(_strong(own, edits, mcp, spec), weak, owned=len(own), edits=edits, mcp=mcp,
                    excluded_edits=excluded_edits, spec=spec)


def select(spec: ProjectSpec, *, call: Caller, prs: dict[int, dict], slug: str = "",
           users_root: str = "/Users", use_local: bool = True, local_only_scan: bool = False,
           now: Optional[str] = None, fetch_inputs: bool = True) -> dict:
    """Choose the project's conversations. Returns
    ``{spec, generated_at, until, prs, conversations: [...], coverage: {...}}``.

    Canopy is the index. A canopy session whose activity shows project work —
    or that merely names the project — has its local transcripts (if any)
    looked up; when canopy merged several conversations into it, or holds fewer
    human inputs than the transcripts do, the local transcripts are split out
    and each re-scored on its own."""
    until = spec.until or now or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sessions = walk_sessions(call, since=spec.since, until=until)
    heads = branch_index(pr.get("head", "") for pr in prs.values())
    head_to_pr = {pr["head"]: n for n, pr in prs.items() if pr.get("head")}
    since_dt = _parse_ts(spec.since)
    until_dt = _parse_ts(until)
    convs: list[Conversation] = []
    seen_transcripts: set[str] = set()
    coverage = {"canopy_sessions_walked": len(sessions), "split_sessions": [],
                "local_short_sessions": [], "tail_only_sessions": []}

    picked = []
    for s in sessions:
        sig = _canopy_signals(s, spec, prs, slug)
        weak = _weak_hits(spec, s.get("title", ""), s.get("session_key", ""),
                          " ".join(sig["cwds"]), " ".join(sig["branches"]))
        strong = _strong(sig["owned"], sig["edits"], sig["mcp"], spec)
        forced = _override(spec, s["id"])
        if strong or weak or sig["excluded_edits"] or forced:
            picked.append((s, sig, weak))
    claims = _claim_transcripts(picked, users_root) if use_local else {}

    for s, sig, weak in picked:
        created = _parse_ts(s.get("created_at"))
        straddles = bool(created and since_dt and created < since_dt)
        scans = []
        for f in claims.get(s["id"], []):
            sc = scan_transcript(f, spec, heads)
            st = _parse_ts(sc.start)
            en = _parse_ts(sc.end)
            if not sc.prompts or (en and since_dt and en < since_dt) or \
                    (st and until_dt and st >= until_dt):
                continue
            scans.append(sc)
        canopy_msgs, canopy_src = ([], "")
        if fetch_inputs:
            try:
                canopy_msgs, canopy_src = human_inputs(call, s["id"])
            except Exception:  # noqa: BLE001 — coverage is reported, never fatal
                canopy_msgs, canopy_src = [], "error"
        if canopy_src == "tail":
            coverage["tail_only_sessions"].append(s["id"])
        local_n = sum(len(sc.prompts) for sc in scans)
        merged = len(scans) > 1 or straddles
        use_split = bool(scans) and (merged or local_n > len(canopy_msgs) or canopy_src == "tail")
        base = dict(canopy_session_id=s["id"], title=s.get("title") or s.get("session_key") or "",
                    runner=s.get("runner_name") or "", workspace=s.get("workspace") or "",
                    canopy_inputs=len(canopy_msgs),
                    canopy_input_source=canopy_src, merged_session=merged)
        if not use_split:
            c = Conversation(id=s["id"], source="canopy",
                             start=_first_at(canopy_msgs) or s.get("created_at") or "",
                             end=s.get("last_activity_at") or "", cwd=(sig["cwds"] or [""])[0],
                             local_inputs=local_n, **base)
            owned = sig["owned"]
            edits, mcp = sig["edits"], sig["mcp"]
            if scans:  # one conversation, canopy complete: union the local signals in
                sc = scans[0]
                owned = sorted(set(owned) | _owned_by_mentions(sc, head_to_pr, spec))
                edits, mcp = max(edits, sc.edits), max(mcp, sc.mcp_calls)
                c.transcript, c.user = sc.path, sc.user
                seen_transcripts.add(sc.path)
            c.owned_prs, c.edits, c.mcp_calls = owned, edits, mcp
            c.branches = sig["branches"]
            c.tier, c.reasons = _tier(owned, edits, mcp, sig["excluded_edits"], weak, spec, prs)
            _apply_override(c, spec)
            if c.tier:
                convs.append(c)
            continue
        coverage["split_sessions" if merged else "local_short_sessions"].append(s["id"])
        for sc in scans:
            seen_transcripts.add(sc.path)
            convs.append(_conv_from_scan(sc, spec, head_to_pr, prs, source="local", **base))
    if local_only_scan and use_local:
        convs.extend(_local_only(spec, head_to_pr, prs, heads, seen_transcripts, users_root,
                                 since_dt, until_dt))
    convs = [c for c in convs if c.tier]
    convs.sort(key=lambda c: c.start or "")
    return {"spec": asdict(spec), "until": until,
            "prs": {str(k): v for k, v in sorted(prs.items())},
            "coverage": coverage,
            "conversations": [asdict(c) for c in convs]}


def _worktree_owner(transcript: Path) -> str:
    """The account whose worktree a transcript ran in, from Claude's project dir
    name (``-Users-<account>-…``) — not the account whose ~/.claude holds it."""
    m = re.match(r"-Users-([^-]+)-", transcript.parent.name)
    return m.group(1) if m else ""


def _prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _claim_transcripts(picked: list, users_root: str) -> dict[str, list[Path]]:
    """Give every local transcript to exactly ONE canopy session.

    An exact cwd match wins. A transcript found only by task name (canopy kept
    no cwd) can be named by several sessions — emdash task keys repeat across
    runners (three different ``supply`` sessions, one per account) — so it goes
    to the session whose runner name best matches the worktree's account
    (``jj-mbp-cdp`` ↔ ``jjackson``), earliest-created on a tie."""
    claims: dict[str, list[Path]] = {}
    owner: dict[Path, tuple] = {}
    for s, sig, _weak in picked:
        for f in local_transcripts_for(sig["cwds"], users_root):
            owner[f] = (2, 0, 0.0, s["id"])  # exact cwd: strongest claim
    for s, sig, _weak in picked:
        if sig["cwds"]:
            continue
        for f in local_transcripts_by_task(s.get("project") or "", s.get("session_key") or "",
                                           users_root):
            score = (1, _prefix_len(s.get("runner_name") or "", _worktree_owner(f)),
                     _neg_ts(s.get("created_at")), s["id"])
            if f not in owner or score > owner[f]:
                owner[f] = score
    for f, score in owner.items():
        claims.setdefault(score[3], []).append(f)
    return claims


def _neg_ts(ts: Optional[str]) -> float:
    """Sort key that ranks an EARLIER timestamp higher under max()."""
    dt = _parse_ts(ts)
    return -dt.timestamp() if dt else float("-inf")


def _first_at(msgs: list[dict]) -> str:
    return (msgs[0].get("created_at") or "") if msgs else ""


def _owned_by_mentions(sc: LocalScan, head_to_pr: dict[str, int], spec: ProjectSpec) -> set[int]:
    return {head_to_pr[b] for b, n in sc.branch_mentions.items()
            if n >= spec.branch_mentions_min and b in head_to_pr}


def _conv_from_scan(sc: LocalScan, spec: ProjectSpec, head_to_pr: dict[str, int],
                    prs: dict[int, dict], *, source: str, **base) -> Conversation:
    owned = sorted(_owned_by_mentions(sc, head_to_pr, spec))
    # Per-transcript name signal: the worktree/branch of THIS conversation, not
    # the merged canopy record's title (which names every conversation in it).
    weak = _weak_hits(spec, sc.cwd, " ".join(sc.git_branches))
    c = Conversation(id=(f"{base.get('canopy_session_id')}:{sc.session_uuid}"
                         if base.get("canopy_session_id") else f"local:{sc.session_uuid}"),
                     source=source, start=sc.start, end=sc.end, user=sc.user, cwd=sc.cwd,
                     transcript=sc.path, local_inputs=len(sc.prompts),
                     **{k: v for k, v in base.items()})
    c.owned_prs, c.edits, c.mcp_calls = owned, sc.edits, sc.mcp_calls
    c.branches = sc.git_branches
    st = _parse_ts(sc.start)
    since_dt = _parse_ts(spec.since)
    if st and since_dt and st < since_dt:
        c.tier, c.reasons = "excluded", [f"starts before {spec.since[:10]}"]
    else:
        c.tier, c.reasons = _tier(owned, sc.edits, sc.mcp_calls, sc.excluded_edits, weak, spec,
                                  prs)
    _apply_override(c, spec)
    return c


def _local_only(spec, head_to_pr, prs, heads, seen, users_root, since_dt, until_dt):
    """Fallback for work canopy never indexed: scan every readable local
    transcript in the window that canopy did not already account for. Only a
    STRONG signal admits one (weak-only local hits are too noisy to list)."""
    out = []
    for f in glob.glob(os.path.join(users_root, "*", ".claude", "projects", "*", "*.jsonl")):
        if f in seen or not os.access(f, os.R_OK):
            continue
        try:
            mt = _dt.datetime.fromtimestamp(os.path.getmtime(f), _dt.timezone.utc)
        except OSError:
            continue
        if since_dt and mt < since_dt:
            continue
        sc = scan_transcript(f, spec, heads)
        st = _parse_ts(sc.start)
        if not sc.prompts or (st and until_dt and st >= until_dt):
            continue
        c = _conv_from_scan(sc, spec, head_to_pr, prs, source="local-only", canopy_session_id="",
                            title=os.path.basename(sc.cwd or os.path.dirname(f)))
        if c.tier in ("core", "adjacent") or c.overridden:
            out.append(c)
    return out


def _override(spec: ProjectSpec, *ids: str) -> str:
    for i in ids:
        if not i:
            continue
        if i in spec.exclude_sessions:
            return "exclude"
        if i in spec.include_sessions:
            return "include"
        if i in spec.adjacent_sessions:
            return "adjacent"
    return ""


def _apply_override(c: Conversation, spec: ProjectSpec) -> None:
    stem = c.id.split(":", 1)[1] if ":" in c.id else ""
    ids = [c.id, stem] + ([c.canopy_session_id] if c.source == "canopy" else [])
    o = _override(spec, *ids)
    if o == "exclude":
        c.tier, c.overridden = "excluded", "exclude"
        c.reasons = ["excluded by spec"] + c.reasons
    elif o == "include" and c.tier != "core":
        c.tier, c.overridden = "core", "include"
        c.reasons = ["included by spec"] + c.reasons
    elif o == "adjacent":
        c.tier, c.overridden = "adjacent", "adjacent"
        c.reasons = ["adjacent by spec"] + c.reasons


# ───────────────────────────────────────────────────────────── collection ──

def _canopy_turns(call: Caller, session_id: str, since: str, max_rows: int = 4000) -> list[dict]:
    """Condensed turns for a canopy-sourced conversation: each human prompt with
    the last assistant text before the next prompt (scroll-back read)."""
    rows: list[dict] = []
    before = 10 ** 9
    while len(rows) < max_rows:
        page = call("GET", f"/api/canopy-sessions/{session_id}/messages?before={before}&limit=500")
        msgs = page.get("messages") or []
        if not msgs:
            break
        rows = msgs + rows
        before = min(m.get("turn_index", before) for m in msgs)
        if not page.get("has_more_before"):
            break
    turns: list[dict] = []
    for m in rows:
        if m.get("created_at", "") < since:
            continue
        role, text = m.get("role"), (m.get("plaintext") or "").strip()
        if role == "user" and text and not turn_synthesis.is_noise(text):
            turns.append({"at": m.get("created_at", ""), "prompt": text, "reply": ""})
        elif role == "assistant" and text and turns:
            turns[-1]["reply"] = text
    return turns


def collect(selection: dict, *, call: Caller, tiers: Iterable[str] = ("core", "adjacent"),
            reply_chars: int = 600) -> dict:
    """The ONE analysis substrate every output renders from: per conversation the
    human's prompts verbatim + condensed turns + owned PRs, the input documents
    the human pointed at, and every candidate artifact with its evidence."""
    spec = ProjectSpec.from_dict(selection["spec"])
    prs = {int(k): v for k, v in selection.get("prs", {}).items()}
    tiers = set(tiers)
    out_convs = []
    assistant_by_conv: dict[str, list[dict]] = {}
    for c in selection["conversations"]:
        if c["tier"] not in tiers:
            continue
        # Canopy indexes; for the TEXT, a claimed local transcript wins when there
        # is one. canopy-web's human-inputs read (2026-10-09) still returns
        # subagent dispatch prompts, injected skill bodies and compaction summaries
        # as human input — 51 "human" rows for 22 real prompts on one DDD session —
        # because the flags that tell them apart (isSidechain / isMeta /
        # isCompactSummary) live only in the transcript. Canopy alone is used
        # where no transcript is on this machine (cloud runners, other laptops).
        if c.get("transcript") and os.access(c["transcript"], os.R_OK):
            sc = scan_transcript(c["transcript"], spec)
            prompts = [p for p in sc.prompts if (p["at"] or "") >= spec.since[:19]]
            turns = sc.turns
            assistant_by_conv[c["id"]] = sc.assistant_texts
            text_source = "local"
        else:
            msgs, _src = human_inputs(call, c["canopy_session_id"])
            prompts = [{"at": m.get("created_at", ""), "text": (m.get("plaintext") or "").strip()}
                       for m in msgs if (m.get("created_at") or "") >= spec.since[:19]
                       and (m.get("plaintext") or "").strip()
                       and not turn_synthesis.is_noise(m.get("plaintext") or "")]
            try:
                turns = _canopy_turns(call, c["canopy_session_id"], spec.since)
            except Exception:  # noqa: BLE001
                turns = [{"at": p["at"], "prompt": p["text"], "reply": ""} for p in prompts]
            assistant_by_conv[c["id"]] = [{"at": t["at"], "text": t["reply"]} for t in turns
                                          if t["reply"]]
            text_source = "canopy"
        for i, p in enumerate(prompts):
            p["origin"] = prompt_origin(p["text"], i, c.get("title") or "")
        condensed = [{"at": t["at"], "prompt": _clip(t["prompt"], 500),
                      "reply": _clip(t["reply"], reply_chars)} for t in turns]
        out_convs.append({**c, "text_source": text_source, "prompts": prompts,
                          "turns": condensed,
                          "prs": [prs[n] for n in c.get("owned_prs", []) if n in prs]})
    base = getattr(call, "base_url", DEFAULT_BASE_URL)
    artifacts = collect_artifacts(spec, call=call, conversations=out_convs,
                                  assistant_by_conv=assistant_by_conv, base_url=base)
    return {"spec": selection["spec"], "until": selection.get("until"), "base_url": base,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "coverage": selection.get("coverage", {}),
            "conversations": out_convs,
            "input_documents": input_documents(out_convs),
            "artifacts": artifacts}


_HANDOFF_PREFIX = "We got rate limited on the "
#: canopy stamps every agent-dispatched prompt with this (agent_dispatch).
_DISPATCH_MARKER = "<!-- canopy:dispatched-prompt -->"


def prompt_origin(text: str, index: int, title: str) -> str:
    """Who wrote a "user" prompt: ``person``, ``handoff`` (canopy's rate-limit
    move template — it carries the previous log, not new words) or ``dispatch``
    (the brief that opens a canopy-dispatched ``c-…`` session, written by an
    agent or by canopy, not typed by the person). Only ``person`` prompts are the
    person's own words — quoted verbatim, and counted as their responses."""
    if text.startswith(_HANDOFF_PREFIX):
        return "handoff"
    if _DISPATCH_MARKER in text or (index == 0 and title.startswith("c-")):
        return "dispatch"
    return "person"


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def input_documents(convs: list[dict]) -> list[dict]:
    """Google Docs/Drive/Sheets/Slides links the HUMAN pasted, first mention first —
    the inputs the project started from."""
    seen: dict[str, dict] = {}
    for c in convs:
        for p in c.get("prompts", []):
            for url in _INPUT_DOC_RX.findall(p.get("text", "")):
                key = re.sub(r"[?#].*$", "", url).rstrip("/")
                key = re.sub(r"/(edit|view|preview|copy)$", "", key)
                if key not in seen:
                    seen[key] = {"url": url, "first_at": p.get("at", ""), "conversation": c["id"],
                                 "context": _clip(p.get("text", ""), 300), "mentions": 0}
                seen[key]["mentions"] += 1
    return sorted(seen.values(), key=lambda d: d["first_at"])


# ──────────────────────────────────────────────────────────────── artifacts ──

_ARTIFACT_KINDS = (
    ("walkthrough", "/api/walkthroughs/"),
    ("review", "/api/reviews/"),
    ("storyboard", "/api/storyboards/"),
    ("narrative", "/api/ddd/narratives/"),
)


def _ws_path(path: str, ws: str) -> str:
    return path.replace("/api/", f"/api/w/{ws}/", 1) if ws else path


def _list(call: Caller, path: str) -> list[dict]:
    d = call("GET", path)
    if isinstance(d, dict):
        return list(d.get("items") or d.get("results") or [])
    return list(d or [])


def collect_artifacts(spec: ProjectSpec, *, call: Caller, conversations: list[dict],
                      assistant_by_conv: dict[str, list[dict]], base_url: str) -> list[dict]:
    """Every canopy artifact that belongs to the project — stamped with its board
    project, made in one of its sessions, or named for it after ``since`` — each
    carrying the evidence that bears on whether it was useful."""
    session_ids = {c["canopy_session_id"] for c in conversations if c.get("canopy_session_id")}
    workspaces = list(spec.workspaces) or [""]
    found: dict[tuple[str, str], dict] = {}
    for ws in workspaces:
        for kind, path in _ARTIFACT_KINDS:
            try:
                rows = _list(call, _ws_path(path, ws))
            except Exception:  # noqa: BLE001 — a missing app is reported as none
                continue
            for r in rows:
                key = r.get("id") or r.get("slug")
                if not key or (kind, str(key)) in found:
                    continue
                why = _artifact_belongs(r, spec, session_ids)
                if why:
                    found[(kind, str(key))] = _artifact_row(kind, r, ws, why, base_url)
    _add_review_decisions(call, found, spec.person)
    _add_run_verdicts(call, found)
    _add_mentions(found, conversations, assistant_by_conv)
    return sorted(found.values(), key=lambda a: a.get("created_at") or "")


def _artifact_belongs(r: dict, spec: ProjectSpec, session_ids: set[str]) -> str:
    ap = r.get("agent_project") or {}
    if spec.agent_project and ap:
        agent, _, ext = spec.agent_project.replace(":", "/").partition("/")
        if (ap.get("agent"), ap.get("ext_id")) == (agent, ext) or str(ap.get("id")) == spec.agent_project:
            return "board project"
    if r.get("session_id") and r["session_id"] in session_ids:
        return "made in a project session"
    created = r.get("created_at") or r.get("latest_at") or ""
    if created and created < spec.since:
        return ""
    terms = [t.lower() for t in (spec.artifact_terms or spec.name_terms or [spec.name])]
    hay = " ".join(str(r.get(k) or "") for k in ("narrative_slug", "slug", "run_id", "title")).lower()
    if any(t in hay for t in terms):
        return "named for the project"
    return ""


def _artifact_row(kind: str, r: dict, ws: str, why: str, base_url: str) -> dict:
    ident = str(r.get("id") or r.get("slug"))
    from orchestrator import canopy_web

    page = {
        "walkthrough": f"/walkthrough/{ident}",
        "review": f"/review/{ident}",
        "storyboard": f"/storyboard/{r.get('slug') or ident}",
        "narrative": f"/ddd/{ident}",
    }[kind]
    try:  # artifact pages live only under /w/<workspace>/ (canopy-web#1337)
        url = canopy_web.app_url(page, ws, base_url)
    except canopy_web.WorkspaceRequiredError:
        url = ""  # listed without a workspace: name `workspaces` in the spec to get links
    evidence = []
    if r.get("comment_count"):
        evidence.append({"side": "human", "source": "comments",
                         "detail": f"{r['comment_count']} comment(s) from "
                                   f"{r.get('commenter_count') or '?'} person(s)"})
    if r.get("owner_viewed_at"):
        evidence.append({"side": "human", "source": "owner_viewed", "at": r["owner_viewed_at"],
                         "detail": "the owner opened it"})
    if r.get("viewer_count"):
        evidence.append({"side": "human", "source": "viewers",
                         "detail": f"{r['viewer_count']} signed-in human viewer(s)"})
    if kind == "narrative" and r.get("phase"):
        evidence.append({"side": "agent", "source": "narrative_phase", "detail": r["phase"]})
    return {"kind": kind, "id": ident, "title": r.get("title") or ident, "url": url,
            "workspace": ws, "created_at": r.get("created_at") or r.get("latest_at") or "",
            "run_id": r.get("run_id") or "", "narrative_slug": r.get("narrative_slug") or
            (ident if kind == "narrative" else ""), "role": r.get("role") or "",
            "status": r.get("status") or "", "gate": r.get("gate") or "",
            "description": _clip(r.get("description") or "", 300),
            "session_id": r.get("session_id") or "", "why_in_scope": why,
            "evidence": evidence}


def _flatten(value) -> str:
    """A review's response JSON as readable text: its string leaves, keyed."""
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            t = _flatten(v)
            if t:
                parts.append(t if k.lstrip("_") in ("note", "comment", "overall_feedback")
                             else f"{k}: {t}")
        return "; ".join(parts)
    if isinstance(value, list):
        return "; ".join(t for t in (_flatten(v) for v in value) if t)
    return str(value).strip() if value not in (None, "") else ""


def decision_side(text: str, person: str = "") -> str:
    """Who actually made a resolved review's decision. A DDD orchestrator often
    resolves its own gate ("approved by the agent under the standing mandate");
    that is the AGENT's judgment, however human the field it lands in looks.
    A decision that names the person or "in chat" relays theirs. The skill
    still reads the text — this only keeps an agent's self-approval from being
    counted as the person's reaction by default."""
    low = (text or "").lower()
    if any(k in low for k in ("not a human", "standing mandate", "approved by the agent",
                              "on the merits")):
        return "agent"
    names = [w.lower() for w in (person or "").split() if len(w) > 2]
    if any(k in low for k in names + ["owner", "in chat"]):
        return "human"
    if "orchestrator" in low:
        return "agent"
    return "human"


def _add_review_decisions(call: Caller, found: dict, person: str = "") -> None:
    """A RESOLVED review gate carries the human's decision verbatim — the
    strongest "the human responded to it" evidence canopy holds."""
    for (kind, ident), a in found.items():
        if kind != "review" or a["status"] != "resolved":
            continue
        try:
            d = call("GET", _ws_path(f"/api/reviews/{ident}/", a["workspace"]))
        except Exception:  # noqa: BLE001
            continue
        resp = d.get("response_json")
        if resp:
            a["evidence"].append({"side": decision_side(_flatten(resp), person),
                                  "source": "review_decision",
                                  "at": d.get("resolved_at") or "",
                                  "detail": _clip(_flatten(resp), 600)})


def _add_run_verdicts(call: Caller, found: dict) -> None:
    """The DDD run phase (converged / stopped_not_converged / …) is the agent's
    own verdict at the time on everything that run produced."""
    runs: dict[str, str] = {}
    for a in found.values():
        rid = a.get("run_id")
        if not rid or rid in runs:
            continue
        try:
            d = call("GET", _ws_path(f"/api/ddd/runs/{rid}/", a["workspace"]))
            runs[rid] = str(d.get("phase") or "")
        except Exception:  # noqa: BLE001
            runs[rid] = ""
    for a in found.values():
        ph = runs.get(a.get("run_id") or "")
        if ph:
            a["run_phase"] = ph
            a["evidence"].append({"side": "agent", "source": "run_phase", "detail": ph})


def _add_mentions(found: dict, convs: list[dict], assistant_by_conv: dict[str, list[dict]]) -> None:
    """The human quoting an artifact back (its id, run, or narrative) is a direct
    response to it; the agent naming it in its own reply is its judgment at the
    time. Both are captured verbatim for the skill to read — sentiment is a
    judgment, not a regex."""
    needles: dict[str, list[str]] = {}
    for key, a in found.items():
        n = [a["id"]] if len(a["id"]) >= 12 else []
        if a.get("run_id"):
            n.append(a["run_id"])
        elif a["kind"] in ("narrative", "storyboard") and len(a["id"]) >= 8:
            n.append(a["id"])
        needles[key] = n
    for key, a in found.items():
        if not needles[key]:
            continue
        human, agent = [], []
        for c in convs:
            for p in c.get("prompts", []):
                if p.get("origin", "person") != "person":
                    continue  # a dispatch brief naming an artifact is not a response to it
                if any(x in p["text"] for x in needles[key]):
                    human.append({"side": "human", "source": "prompt_mention", "at": p["at"],
                                  "conversation": c["id"], "detail": _clip(p["text"], 600)})
            person = [p for p in c.get("prompts", []) if p.get("origin", "person") == "person"]
            for t in assistant_by_conv.get(c["id"], []):
                if any(x in t["text"] for x in needles[key]):
                    agent.append({"side": "agent", "source": "agent_mention", "at": t["at"],
                                  "conversation": c["id"], "detail": _clip(t["text"], 600)})
                    # The person's NEXT prompt after the agent shared it is how they
                    # actually respond — nobody retypes a uuid. Whether it reads as
                    # "this is good" is the skill's call.
                    nxt = next((p for p in person if (p["at"] or "") > (t["at"] or "")), None)
                    if nxt and not any(h.get("at") == nxt["at"] for h in human):
                        human.append({"side": "human", "source": "reply_after_share",
                                      "at": nxt["at"], "conversation": c["id"],
                                      "detail": _clip(nxt["text"], 600)})
        a["evidence"].extend(human[-5:])
        a["evidence"].extend(agent[-3:])


# ───────────────────────────────────────────────────────────────── render ──

class JudgmentError(ValueError):
    """The judgments file claims something the evidence does not support."""


def validate_judgments(bundle: dict, judgments: dict) -> list[str]:
    """Problems that make the judgments unrenderable. Rail: an artifact judged
    useful must have at least one evidence item in the bundle, and must cite
    which (by index) — "useful" without evidence is exactly the failure the
    package exists to prevent."""
    problems = []
    arts = {a["id"]: a for a in bundle.get("artifacts", [])}
    for aid, j in (judgments.get("artifacts") or {}).items():
        if not j.get("useful"):
            continue
        a = arts.get(aid)
        if a is None:
            problems.append(f"artifact {aid}: judged useful but not in the bundle")
            continue
        cited = j.get("evidence") or []
        if not a.get("evidence"):
            problems.append(f"artifact {aid} ({a['title']}): judged useful with NO evidence")
        elif not cited or any(not isinstance(i, int) or i >= len(a["evidence"]) for i in cited):
            problems.append(f"artifact {aid} ({a['title']}): cite evidence by index "
                            f"(0..{len(a['evidence']) - 1})")
    convs = {c["id"] for c in bundle.get("conversations", [])}
    for cid in (judgments.get("conversations") or {}):
        if cid not in convs:
            problems.append(f"conversation {cid}: not in the bundle")
    return problems


def _byline(judgments: dict) -> list[str]:
    """Optional header line ("Prepared by … · date · for …") so a doc found later
    explains itself."""
    b = (judgments.get("byline") or "").strip()
    return [b, ""] if b else []


def _person(c: dict) -> list[dict]:
    return [p for p in c.get("prompts", []) if p.get("origin", "person") == "person"]


def _day(ts: str) -> str:
    dt = _parse_ts(ts)
    return dt.strftime("%Y-%m-%d") if dt else "?"


def _span(c: dict) -> str:
    a, b = _day(c.get("start", "")), _day(c.get("end", ""))
    return a if a == b else f"{a} to {b}"


def _phases(bundle: dict, judgments: dict) -> list[tuple[dict, list[dict]]]:
    convs = [c for c in bundle["conversations"]
             if (judgments.get("conversations") or {}).get(c["id"], {}).get("include", True)]
    phases = judgments.get("phases") or []
    if not phases:
        return [({"name": "", "summary": ""}, convs)]
    out, placed = [], set()
    for ph in phases:
        members = [c for c in convs if c["id"] in set(ph.get("conversations") or [])]
        placed.update(c["id"] for c in members)
        out.append((ph, members))
    rest = [c for c in convs if c["id"] not in placed]
    if rest:
        out.append(({"name": "Other sessions", "summary": ""}, rest))
    return out


def render_timeline(bundle: dict, judgments: dict) -> str:
    spec = bundle["spec"]
    who = judgments.get("person") or spec.get("person") or "the user"
    convs = [c for _, cs in _phases(bundle, judgments) for c in cs]
    users = sorted({c.get("user") or c.get("runner") or "?" for c in convs})
    lines = [f"# {spec['name']}: session timeline", ""] + _byline(judgments) + [
             f"Every session on {spec['name']} since {spec['since'][:10]}, in order. "
             f"{len(convs)} sessions, {sum(len(_person(c)) for c in convs)} prompts from "
             f"{who}, {len({p['number'] for c in convs for p in c['prs']})} pull requests. "
             f"Accounts: {', '.join(users)}.", ""]
    jc = judgments.get("conversations") or {}
    for ph, members in _phases(bundle, judgments):
        if ph.get("name"):
            lines += [f"## {ph['name']}", ""]
            if ph.get("summary"):
                lines += [ph["summary"], ""]
        for c in members:
            j = jc.get(c["id"], {})
            title = j.get("title") or c["title"]
            lines.append(f"**{_span(c)} · {title}**")
            lines.append("")
            first = (_person(c) or c["prompts"] or [{"text": ""}])[0]["text"]
            summary = j.get("summary") or _clip(first, 240)
            lines.append(summary)
            lines.append("")
            meta = [f"{len(_person(c))} prompts", f"account {c.get('user') or c.get('runner')}"]
            if c["prs"]:
                meta.append(f"{len(c['prs'])} PRs")
            if c["tier"] == "adjacent":
                meta.append("adjacent")
            lines.append("- " + ", ".join(meta))
            url = session_url(bundle, c)
            if url:
                lines.append(f"- Canopy session: {url}"
                             + (" (one record holding several conversations)"
                                if c.get("merged_session") else ""))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def session_url(bundle: dict, c: dict) -> str:
    """canopy-web's page for a session: ``/w/<workspace>/chat/<id>``."""
    if not c.get("canopy_session_id") or not c.get("workspace"):
        return ""
    from orchestrator import canopy_web

    return canopy_web.app_url(f"/chat/{c['canopy_session_id']}", c["workspace"],
                              bundle.get("base_url") or DEFAULT_BASE_URL)


def useful_artifacts(bundle: dict, judgments: dict) -> list[tuple[dict, dict]]:
    ja = judgments.get("artifacts") or {}
    out = []
    for a in bundle.get("artifacts", []):
        j = ja.get(a["id"]) or {}
        if j.get("useful") and a.get("evidence"):
            out.append((a, j))
    return out


def render_artifacts(bundle: dict, judgments: dict) -> str:
    spec = bundle["spec"]
    rows = useful_artifacts(bundle, judgments)
    total = len(bundle.get("artifacts", []))
    lines = [f"# {spec['name']}: useful artifacts", ""] + _byline(judgments) + [
             f"{len(rows)} of {total} {spec['name']} artifacts in Canopy. An artifact is listed "
             "only with evidence that it was useful: the agent judged it good at the time, or "
             "the person responded to it and implied it was good.", ""]
    for a, j in rows:
        lines.append(f"**[{j.get('title') or a['title']}]({a['url']})** ({a['kind']}, "
                     f"{_day(a['created_at'])})")
        lines.append("")
        if j.get("why"):
            lines.append(j["why"])
            lines.append("")
        for i in j.get("evidence") or []:
            ev = a["evidence"][i]
            who = "Agent" if ev["side"] == "agent" else "Human"
            lines.append(f"- {who}, {ev['source'].replace('_', ' ')}"
                         + (f" ({_day(ev['at'])})" if ev.get("at") else "")
                         + f": {_quote(ev['detail'], 280)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _quote(s: str, n: int) -> str:
    return "“" + _clip(" ".join((s or "").split()), n) + "”"


def render_ai_package(bundle: dict, judgments: dict, reading: str = "") -> str:
    """The AI-facing package: evidence FIRST (scope, timeline, useful artifacts,
    input documents, the person's prompts verbatim), and the agent's reading of
    the person LAST, in its own clearly labeled, optional section — so another
    person's AI reasons from what happened, not from one agent's summary."""
    spec = bundle["spec"]
    who = judgments.get("person") or spec.get("person") or "the user"
    jc = judgments.get("conversations") or {}
    cov = bundle.get("coverage") or {}
    convs = [c for _, cs in _phases(bundle, judgments) for c in cs]
    lines = [f"# {spec['name']}: project package for an AI", ""] + _byline(judgments) + [
             "How to use this: it is the evidence of how this project was built, for an AI "
             "helping you think about what comes next. Parts 1 to 5 are evidence: what "
             f"happened, in {who}'s own words. Part 6 is one agent's reading of {who}'s "
             "thinking. It is optional. Leave it out if you want your AI to form its own view.",
             "",
             "## Part 1: Scope and method", ""]
    lines += [f"- Project: {spec['name']}. Window: {spec['since'][:10]} to "
              f"{(bundle.get('until') or '')[:10]}.",
              f"- A session is in scope when it did project work: owned a pull request that "
              f"touched {', '.join(spec.get('paths') or []) or 'the project paths'}, edited those "
              f"paths, or called the {', '.join(spec.get('mcp_prefixes') or []) or 'project'} "
              "tools. A matching name alone only nominates a candidate.",
              ]
    if spec.get("exclude_paths"):
        lines.append(f"- Excluded: sessions whose only project work was in "
                     f"{', '.join(spec['exclude_paths'])}.")
    if judgments.get("scope_notes"):
        lines += [f"- {n}" for n in judgments["scope_notes"]]
    n_local = sum(1 for c in convs if c["text_source"] == "local")
    lines.append("- Sources: Canopy is the index (which sessions, their PRs and edits, and the "
                 f"artifacts). Prompt text came from local transcripts for {n_local} of "
                 f"{len(convs)} sessions, and from Canopy for the rest. Where both exist, the "
                 "transcript can tell what the person typed apart from subagent prompts and "
                 "injected skill text, so it is used.")
    if cov.get("split_sessions"):
        lines.append(f"- {len(cov['split_sessions'])} Canopy session record(s) held several "
                     "conversations under one task name; they were split back apart.")
    lines.append("")
    lines += ["## Part 2: Session timeline", ""]
    for c in convs:
        j = jc.get(c["id"], {})
        lines.append(f"**{_span(c)} · {j.get('title') or c['title']}** "
                     f"({len(_person(c))} prompts, {len(c['prs'])} PRs, {c['tier']})")
        lines.append("")
        if j.get("summary"):
            lines += [j["summary"], ""]
        if c["prs"]:
            lines.append("PRs: " + ", ".join(f"#{p['number']} {p['title']}" for p in c["prs"]))
            lines.append("")
    lines += ["## Part 3: Useful artifacts", ""]
    rows = useful_artifacts(bundle, judgments)
    if not rows:
        lines += ["None met the evidence bar.", ""]
    for a, j in rows:
        lines.append(f"**{j.get('title') or a['title']}** ({a['kind']}, {_day(a['created_at'])}): "
                     f"{a['url']}")
        lines.append("")
        if j.get("why"):
            lines += [j["why"], ""]
        for i in j.get("evidence") or []:
            ev = a["evidence"][i]
            lines.append(f"- {ev['side']}, {ev['source'].replace('_', ' ')}: "
                         f"{_quote(ev['detail'], 600)}")
        lines.append("")
    lines += ["## Part 4: Input documents", "",
              f"Documents {who} pointed the agents at, first mention first.", ""]
    for d in bundle.get("input_documents") or []:
        lines.append(f"- {d['url']} (first {_day(d['first_at'])}, {d['mentions']} mention(s))")
    lines.append("")
    lines += [f"## Part 5: {who}'s prompts, verbatim", "",
              f"Every prompt {who} typed in these sessions, in order, unedited.", ""]
    for c in convs:
        j = jc.get(c["id"], {})
        lines += [f"### {_span(c)} · {j.get('title') or c['title']}", ""]
        for p in c["prompts"]:
            origin = p.get("origin", "person")
            if origin == "person":
                lines.append(f"- {_day(p['at'])}: {' '.join(p['text'].split())}")
            else:
                label = ("canopy's rate-limit handoff (carries the previous log)"
                         if origin == "handoff" else "dispatch brief, written by an agent")
                lines.append(f"- {_day(p['at'])}: [{label}, not typed by {who}] "
                             f"{_clip(' '.join(p['text'].split()), 300)}")
        lines.append("")
    lines += [f"## Part 6 (optional): How the agent reads {who}", "",
              f"This is an interpretation, not evidence. Skip it if you want your AI to reason "
              f"from Parts 1 to 5 alone.", ""]
    lines.append(reading.strip() if reading.strip() else "(not written)")
    return "\n".join(lines).rstrip() + "\n"


def render_package(bundle: dict, judgments: dict, out_dir: str | Path,
                   reading: str = "") -> dict[str, str]:
    problems = validate_judgments(bundle, judgments)
    if problems:
        raise JudgmentError("; ".join(problems))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "timeline.md": render_timeline(bundle, judgments),
        "artifacts.md": render_artifacts(bundle, judgments),
        "ai-package.md": render_ai_package(bundle, judgments, reading),
    }
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
    return {name: str(out / name) for name in files}


def compare_selection(selection: dict, truth: Iterable,
                      tiers: Iterable[str] = ("core", "adjacent")) -> dict:
    """Recall/precision of a selection against a hand-checked set — how a new
    project spec is validated. Each truth item is one conversation, given as an
    id or a collection of equivalent ids (transcript stem, canopy session id)."""
    rows = [frozenset([t]) if isinstance(t, str) else frozenset(t) for t in truth]
    tiers = set(tiers)
    hit_rows: set[int] = set()
    extra: list[str] = []
    selected = 0
    for c in selection["conversations"]:
        if c["tier"] not in tiers:
            continue
        selected += 1
        keys = {k for k in (Path(c["transcript"]).stem if c.get("transcript") else "",
                            c["id"].split(":", 1)[1] if ":" in c["id"] else "",
                            c.get("canopy_session_id") or "", c["id"]) if k}
        match = next((i for i, r in enumerate(rows) if r & keys and i not in hit_rows), None)
        if match is None:
            extra.append(c["id"])
        else:
            hit_rows.add(match)
    return {"selected": selected, "truth": len(rows), "hits": len(hit_rows),
            "recall": round(len(hit_rows) / len(rows), 3) if rows else None,
            "precision": round(len(hit_rows) / selected, 3) if selected else None,
            "missed": [sorted(rows[i]) for i in range(len(rows)) if i not in hit_rows],
            "extra": extra}
