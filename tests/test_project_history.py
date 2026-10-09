"""project_history: deterministic project selection, collection and rendering.

Offline: canopy-web is a fake caller, local transcripts are tmp files laid out
like /Users/<account>/.claude/projects/<slug>/<uuid>.jsonl.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import harvest
from orchestrator import project_history as ph


# ── helpers ──────────────────────────────────────────────────────────────────

def _spec(**kw) -> ph.ProjectSpec:
    base = dict(name="Supply", since="2026-09-10", repo="connect-labs",
                paths=["connect_labs/supply_chain"], exclude_paths=["connect_labs/supply"],
                mcp_prefixes=["supply_chain_"], name_terms=["supply"], person="Jonathan",
                agent_project="hal/P5")
    base.update(kw)
    return ph.ProjectSpec.from_dict(base)


def _user(text, ts, **extra):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}, **extra}


def _asst(ts, text="", tools=()):
    content = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "name": n, "input": i} for n, i in tools]
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": content}}


def _write(path: Path, events: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


def _transcript(users_root: Path, account: str, cwd: str, uuid: str, events) -> Path:
    slug = ph.claude_project_slug(cwd)
    return _write(users_root / account / ".claude" / "projects" / slug / f"{uuid}.jsonl", events)


class FakeCanopy:
    """Routes canopy-web GETs to canned data."""

    base_url = "https://canopy.example"

    def __init__(self, sessions=(), inputs=None, artifacts=None, reviews=None, runs=None):
        self.sessions = list(sessions)
        self.inputs = inputs or {}
        self.artifacts = artifacts or {}
        self.reviews = reviews or {}
        self.runs = runs or {}
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append(path)
        if path.startswith("/api/canopy-sessions/search"):
            return {"sessions": self.sessions, "next_cursor": None}
        if "/human-inputs" in path:
            sid = path.split("/")[3]
            return {"messages": self.inputs.get(sid, []), "next_cursor": None, "source": "transcript"}
        if "/messages" in path:
            return {"messages": [], "has_more_before": False}
        for kind in ("walkthroughs", "reviews", "storyboards", "ddd/narratives"):
            if path.rstrip("/").endswith("/api/" + kind):
                return self.artifacts.get(kind, [])
        if path.startswith("/api/reviews/"):
            return self.reviews.get(path.split("/")[3], {})
        if path.startswith("/api/ddd/runs/"):
            return self.runs.get(path.split("/")[4], {})
        raise AssertionError(f"unexpected call {path}")


# ── spec + matching ──────────────────────────────────────────────────────────

def test_spec_requires_name_and_since_and_rejects_unknown_fields():
    with pytest.raises(ValueError):
        ph.ProjectSpec.from_dict({"name": "x"})
    with pytest.raises(ValueError, match="unknown"):
        ph.ProjectSpec.from_dict({"name": "x", "since": "2026-01-01", "pathz": []})
    assert _spec().since == "2026-09-10T00:00:00Z"


@pytest.mark.parametrize("path,hit", [
    ("/Users/a/wt/connect_labs/supply_chain/models.py", True),
    ("connect_labs/supply_chain", True),
    ("supply_chain/stock", True),                    # canopy records some dirs relative
    ("/Users/a/wt/connect_labs/supply/views.py", False),
    ("connect_labs/supply", False),                  # sibling dir, not a prefix match
    ("/Users/a/wt/other/supply_chain/x.py", False),  # absolute paths need the whole spec
])
def test_path_matches_on_whole_segments(path, hit):
    assert ph.path_matches(path, ["connect_labs/supply_chain"]) is hit


def test_branch_mentions_respect_token_boundaries():
    heads = ph.branch_index(["supply-x", "supply-x-v2", "main"])
    raw = "pushed supply-x and supply-x-v2; origin/supply-x again; main"
    assert ph.count_branch_mentions(raw, heads) == {"supply-x": 2, "supply-x-v2": 1}


# ── transcript scanning ──────────────────────────────────────────────────────

def test_scan_transcript_reads_prompts_signals_and_subagents(tmp_path):
    t = _write(tmp_path / "p" / "u1.jsonl", [
        _user("build the supply tender view", "2026-09-12T10:00:00Z", cwd="/w/emdash-supply-ab12c",
              gitBranch="supply/tenders"),
        _user("skill body", "2026-09-12T10:00:01Z", isMeta=True),
        _user([{"type": "tool_result", "content": "ok"}], "2026-09-12T10:00:02Z"),
        _asst("2026-09-12T10:01:00Z", "done", tools=[
            ("Edit", {"file_path": "/w/connect_labs/supply_chain/views.py"}),
            ("Edit", {"file_path": "/w/connect_labs/supply/old.py"}),
            ("mcp__connect_labs__supply_chain_tender_list", {})]),
        _user([{"type": "text", "text": "looks good, see screenshot"}, {"type": "image"}],
              "2026-09-12T11:00:00Z"),
    ])
    _write(tmp_path / "p" / "u1" / "subagents" / "a.jsonl", [
        _asst("2026-09-12T10:30:00Z", tools=[
            ("Write", {"file_path": "/w/connect_labs/supply_chain/models.py"})]),
        {"type": "user", "message": {"content": "supply/tenders supply/tenders"}},
    ])
    sc = ph.scan_transcript(t, _spec(), ph.branch_index(["supply/tenders"]))
    assert [p["text"] for p in sc.prompts] == ["build the supply tender view",
                                               "looks good, see screenshot"]
    assert sc.edits == 2 and sc.excluded_edits == 1 and sc.mcp_calls == 1
    assert sc.branch_mentions == {"supply/tenders": 3}   # gitBranch + 2 in the subagent
    assert sc.start == "2026-09-12T10:00:00Z" and sc.git_branches == ["supply/tenders"]
    assert sc.turns[0]["reply"] == "done"


# ── classification ───────────────────────────────────────────────────────────

def test_tiers():
    spec = _spec()
    prs = {1: {"cross_cutting": False}, 2: {"cross_cutting": True}}
    assert ph._tier([1], 3, 0, 0, [], spec, prs)[0] == "core"
    assert ph._tier([1], 0, 0, 0, [], spec, prs)[0] == "adjacent"          # a lone PR
    assert ph._tier([2], 0, 0, 0, [], spec, prs) == ("candidate", ["owns 1 cross-cutting PR(s)"])
    assert ph._tier([], 0, 2, 0, ["supply"], spec, prs)[0] == "candidate"  # MCP under min
    assert ph._tier([], 0, 0, 4, [], spec, prs)[0] == "excluded"
    assert ph._tier([], 0, 0, 0, [], spec, prs) == ("", [])


def test_prompt_origin():
    assert ph.prompt_origin("We got rate limited on the jj account…", 0, "supply") == "handoff"
    assert ph.prompt_origin("do X\n<!-- canopy:dispatched-prompt -->", 3, "supply") == "dispatch"
    assert ph.prompt_origin("# Run the loop", 0, "c-run-loop-1234") == "dispatch"
    assert ph.prompt_origin("where are we?", 1, "c-run-loop-1234") == "person"


def test_decision_side_separates_agent_self_approval_from_the_person():
    assert ph.decision_side("Approved under the standing mandate", "Jonathan") == "agent"
    assert ph.decision_side("Autonomous restatement, not a human approval", "Jonathan") == "agent"
    assert ph.decision_side("Owner ruling, Jonathan: move scene 3", "Jonathan Jackson") == "human"
    assert ph.decision_side("recorded by the orchestrator", "Jonathan") == "agent"


# ── selection ────────────────────────────────────────────────────────────────

def _session(sid, key, created, last, runner="jj-mbp", project="connect-labs", activity=None):
    return {"id": sid, "title": key, "session_key": key, "project": project, "workspace": "w",
            "runner_name": runner, "created_at": created, "last_activity_at": last,
            "activity": activity or {}}


def test_select_uses_canopy_activity_and_splits_a_merged_session(tmp_path):
    users = tmp_path / "Users"
    root = "/Users/{acct}/emdash/worktrees/connect-labs-1234abcd/emdash-{name}"
    # A merged canopy record ("supply", created in July) holding two conversations:
    # a pre-pivot one in worktree ivyep, and the real v1 build in 8tbye.
    _transcript(users, "jjackson", root.format(acct="jjackson", name="supply-ivyep"), "old", [
        _user("login for the oes supply app", "2026-09-09T10:00:00Z")])
    _transcript(users, "jjackson", root.format(acct="jjackson", name="supply-8tbye"), "v1", [
        _user("build supply from sophie's sheet", "2026-09-11T21:00:00Z"),
        _asst("2026-09-11T22:00:00Z", "ok", tools=[
            ("Edit", {"file_path": "/x/connect_labs/supply_chain/models.py"})])])
    # Same task key on ANOTHER runner/account: must not steal jjackson's transcripts.
    _transcript(users, "acedimagi", root.format(acct="acedimagi", name="supply-piux3"), "uc", [
        _user("build the use cases", "2026-09-23T21:00:00Z"),
        _asst("2026-09-23T22:00:00Z", "", tools=[
            ("Edit", {"file_path": "/x/connect_labs/supply_chain/use.py"})])])
    sessions = [
        _session("s-jj", "supply", "2026-07-28T00:00:00Z", "2026-10-02T00:00:00Z"),
        _session("s-ace", "supply", "2026-07-26T00:00:00Z", "2026-09-24T00:00:00Z",
                 runner="acedimagi-mbp"),
        # A canopy-only review session: MCP calls, no local transcript.
        _session("s-review", "c-first-use-review-ab12", "2026-10-08T00:00:00Z",
                 "2026-10-09T00:00:00Z", project="", activity={
                     "mcp_tools": {"mcp__connect_labs__supply_chain_worker_stock": 5}}),
        # Noise: names nothing, does nothing.
        _session("s-noise", "morning-briefing", "2026-09-20T00:00:00Z", "2026-09-20T01:00:00Z"),
        # Name only: a candidate the agent must decide.
        _session("s-name", "supply-functionality", "2026-09-10T05:00:00Z",
                 "2026-09-10T06:00:00Z", project="nowhere"),
    ]
    fake = FakeCanopy(sessions, inputs={"s-review": [
        {"created_at": "2026-10-08T00:00:01Z", "plaintext": "review the screens"}]})
    sel = ph.select(_spec(), call=fake, prs={}, users_root=str(users), now="2026-10-10T00:00:00Z")
    by_title = {(c["title"], c.get("user")): c for c in sel["conversations"]}
    tiers = {c["id"]: c["tier"] for c in sel["conversations"]}

    assert tiers["s-jj:v1"] == "core" and "s-jj:old" not in tiers   # pre-pivot dropped
    assert tiers["s-ace:uc"] == "core"
    assert not any(k.startswith("s-ace:v1") or k.startswith("s-jj:uc") for k in tiers)
    assert tiers["s-review"] == "core" and by_title[("c-first-use-review-ab12", "")]["source"] == "canopy"
    assert tiers["s-name"] == "candidate"
    assert "s-noise" not in tiers
    assert "s-jj" in sel["coverage"]["split_sessions"]
    starts = [c["start"] for c in sel["conversations"]]
    assert starts == sorted(starts)

    # The agent's calls persist in the spec and make the re-run deterministic.
    spec = _spec(include_sessions=["s-name"], exclude_sessions=["v1"])
    sel2 = ph.select(spec, call=FakeCanopy(sessions), prs={}, users_root=str(users),
                     now="2026-10-10T00:00:00Z", fetch_inputs=False)
    tiers2 = {c["id"]: c["tier"] for c in sel2["conversations"]}
    assert tiers2["s-name"] == "core" and tiers2["s-jj:v1"] == "excluded"

    # Validation against a hand-checked set.
    cmp = ph.compare_selection(sel, [["v1", "s-jj"], ["uc"], ["s-review"], ["s-missing"]])
    assert cmp["hits"] == 3 and cmp["truth"] == 4 and cmp["missed"] == [["s-missing"]]


# ── collection + artifacts ───────────────────────────────────────────────────

def test_collect_prefers_transcript_text_and_gathers_artifact_evidence(tmp_path):
    t = _write(tmp_path / "Users" / "jj" / ".claude" / "projects" / "p" / "c1.jsonl", [
        _user("<!-- canopy:dispatched-prompt --> brief naming supply-x-2026-10-01-001",
              "2026-10-01T09:00:00Z"),
        _user("show me https://docs.google.com/document/d/abc/edit please", "2026-10-01T10:00:00Z"),
        _asst("2026-10-01T10:05:00Z", "Video ready: https://c/w/x/walkthrough/"
              "aaaaaaaa-1111-2222-3333-444444444444"),
        _user("okay this is good", "2026-10-01T10:10:00Z"),
    ])
    selection = {"spec": vars(_spec()), "until": "2026-10-10T00:00:00Z", "prs": {
        "7": {"number": 7, "title": "tenders", "url": "u7", "head": "supply/t"}},
        "conversations": [{"id": "s1", "canopy_session_id": "s1", "title": "supply",
                           "source": "canopy", "start": "2026-10-01T09:00:00Z",
                           "end": "2026-10-01T10:10:00Z", "tier": "core", "transcript": str(t),
                           "owned_prs": [7], "workspace": "w"}]}
    fake = FakeCanopy(artifacts={
        "walkthroughs": [
            {"id": "aaaaaaaa-1111-2222-3333-444444444444", "title": "supply-x hero",
             "created_at": "2026-10-01T10:04:00Z", "run_id": "supply-x-2026-10-01-001"},
            {"id": "bbbbbbbb-1111-2222-3333-444444444444", "title": "unrelated",
             "created_at": "2026-10-01T10:04:00Z"},
            {"id": "cccccccc-1111-2222-3333-444444444444", "title": "supply OES July",
             "created_at": "2026-07-30T00:00:00Z"}],
        "reviews": [{"id": "r1", "title": "Other", "status": "resolved", "created_at": "2026-10-01",
                     "agent_project": {"id": 21, "agent": "hal", "ext_id": "P5"}}],
    }, reviews={"r1": {"response_json": {"note": "Owner ruling, Jonathan: approve"}}},
       runs={"supply-x-2026-10-01-001": {"phase": "converged"}})
    b = ph.collect(selection, call=fake)
    c = b["conversations"][0]
    assert c["text_source"] == "local"
    assert [p["origin"] for p in c["prompts"]] == ["dispatch", "person", "person"]
    assert b["input_documents"][0]["url"].startswith("https://docs.google.com/document/d/abc")
    assert c["prs"][0]["number"] == 7
    arts = {a["id"]: a for a in b["artifacts"]}
    assert set(arts) == {"aaaaaaaa-1111-2222-3333-444444444444", "r1"}   # July + unrelated out
    hero = arts["aaaaaaaa-1111-2222-3333-444444444444"]
    sources = [(e["side"], e["source"]) for e in hero["evidence"]]
    assert ("agent", "run_phase") in sources and ("agent", "agent_mention") in sources
    reply = [e for e in hero["evidence"] if e["source"] == "reply_after_share"]
    assert reply and reply[0]["detail"] == "okay this is good"
    # A dispatch brief naming the run is not the person responding to it.
    assert not any(e["source"] == "prompt_mention" for e in hero["evidence"])
    r1 = arts["r1"]["evidence"][0]
    assert r1["side"] == "human" and r1["detail"] == "Owner ruling, Jonathan: approve"


# ── render ───────────────────────────────────────────────────────────────────

def _bundle():
    return {"spec": vars(_spec()), "until": "2026-10-10T00:00:00Z",
            "base_url": "https://canopy.example", "coverage": {},
            "input_documents": [], "conversations": [{
                "id": "s1", "canopy_session_id": "s1", "workspace": "w", "title": "supply",
                "tier": "core", "start": "2026-09-11T00:00:00Z", "end": "2026-09-12T00:00:00Z",
                "user": "jj", "text_source": "local", "prs": [],
                "prompts": [{"at": "2026-09-11T00:00:00Z", "text": "build it   for sophie",
                             "origin": "person"},
                            {"at": "2026-09-11T01:00:00Z", "text": "agent brief",
                             "origin": "dispatch"}], "turns": []}],
            "artifacts": [
                {"id": "a1", "kind": "walkthrough", "title": "hero", "url": "u1",
                 "created_at": "2026-09-12", "evidence": [
                     {"side": "human", "source": "reply_after_share", "detail": "this is good"}]},
                {"id": "a2", "kind": "walkthrough", "title": "bare", "url": "u2",
                 "created_at": "2026-09-12", "evidence": []}]}


def test_render_refuses_useful_without_evidence():
    with pytest.raises(ph.JudgmentError, match="NO evidence"):
        ph.render_package(_bundle(), {"artifacts": {"a2": {"useful": True, "evidence": []}}},
                          "/nonexistent")
    probs = ph.validate_judgments(_bundle(), {"artifacts": {"a1": {"useful": True}}})
    assert probs and "cite evidence" in probs[0]


def test_render_package_outputs(tmp_path):
    j = {"person": "Jonathan",
         "conversations": {"s1": {"title": "v1 build", "summary": "Built v1."}},
         "artifacts": {"a1": {"useful": True, "evidence": [0], "why": "He liked it."}}}
    files = ph.render_package(_bundle(), j, tmp_path, reading="He wants **visibility**.")
    tl = Path(files["timeline.md"]).read_text()
    assert "v1 build" in tl and "Built v1." in tl and "https://canopy.example/w/w/chat/s1" in tl
    assert "1 prompts" in tl                         # the dispatch brief is not his prompt
    arts = Path(files["artifacts.md"]).read_text()
    assert "hero" in arts and "bare" not in arts and "“this is good”" in arts
    ai = Path(files["ai-package.md"]).read_text()
    order = [ai.index(h) for h in ("Part 1", "Part 2", "Part 3", "Part 5", "Part 6 (optional)")]
    assert order == sorted(order)                    # evidence first, the reading last
    assert "- 2026-09-11: build it for sophie" in ai  # verbatim, whitespace folded
    assert "not typed by Jonathan" in ai
    assert ai.rstrip().endswith("He wants **visibility**.")


# ── harvest: "when" is the start, not the mtime ──────────────────────────────

def test_harvest_dates_sessions_by_first_event(tmp_path):
    early = _write(tmp_path / "proj-supply" / "a.jsonl", [_user("supply start", "2026-09-01T00:00:00Z")])
    late = _write(tmp_path / "proj-supply" / "b.jsonl", [_user("supply later", "2026-09-05T00:00:00Z")])
    import os
    os.utime(early, (2_000_000_000, 2_000_000_000))   # touched last, started first
    refs = harvest.find_initiative_sessions("supply", ["supply"],
                                            roots=[{"user": "u", "path": str(tmp_path), "readable": True}])
    assert [Path(r.path).name for r in refs] == ["a.jsonl", "b.jsonl"]
    assert refs[0].when.startswith("2026-0")
    assert harvest.first_timestamp(str(late)) == "2026-09-05T00:00:00Z"
