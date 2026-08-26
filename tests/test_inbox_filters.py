"""Fleet inbox filters — idempotent apply via gog (framework single-source-of-truth)."""
import json
from types import SimpleNamespace
from orchestrator import inbox_filters


def test_filters_conservative_and_complete():
    for f in inbox_filters.FILTERS:
        assert f["query"] and f["archive"] and f["mark_read"] and f["name"]


def test_auto_reply_ooo_rule_present_and_matches_observed_subjects():
    """Out-of-office / auto-reply guard (added 2026-07-20 after Beth's 'Offline through
    July 26th…' auto-reply spawned a wasted eva turn). Locks the rule in and pins the
    high-precision subject markers it must cover."""
    ooo = next((f for f in inbox_filters.FILTERS if f["name"] == "auto-reply-ooo"), None)
    assert ooo is not None, "auto-reply-ooo filter rule missing"
    q = ooo["query"].lower()
    for marker in ("out of office", "automatic reply", "auto-reply", "offline through"):
        assert marker in q, f"OOO filter should cover {marker!r}"


def test_auto_reply_ooo_body_rule_catches_the_wordings_the_subject_list_misses():
    """The subject list above is an enumeration, so it keeps getting outrun: Beth's responder
    said 'Offline through July 26th' (caught) then 'Offline August 13-14' (missed, 2026-08-13)
    and spawned a wasted eva turn. This rule closes that by requiring a subject marker AND a
    first-person body phrase — the conjunction is what makes a bare `offline` marker safe, so
    it must never be flattened into a subject-only OR (that would archive a real 'let's take
    this offline' thread, the one failure this file exists to prevent)."""
    ooo = next((f for f in inbox_filters.FILTERS if f["name"] == "auto-reply-ooo-body"), None)
    assert ooo is not None, "auto-reply-ooo-body filter rule missing"
    q = ooo["query"].lower()
    # the subject half carries the bare marker the enumeration lacked...
    assert "subject:(offline" in q, "should cover bare 'offline <date>' subjects"
    # ...and is guarded by a body half, outside the subject:() group.
    body = q.split(")", 1)[1]
    assert any(p in body for p in ("i will be offline", "i am out of the office")), \
        "bare subject markers must be ANDed with first-person body phrases, never used alone"


def _runner(list_json, create_rc=0):
    def run(cmd, capture_output, text, timeout):
        if "list" in cmd:
            return SimpleNamespace(returncode=0, stdout=list_json, stderr="")
        return SimpleNamespace(returncode=create_rc, stdout='{"threads": []}', stderr="boom" if create_rc else "")
    return run


def test_apply_creates_when_none_and_is_idempotent():
    r = _runner('{"filters": null}')
    res = inbox_filters.apply_filters("hal@x", "canopy", runner=r)
    assert res["applied"] == [f["name"] for f in inbox_filters.FILTERS]
    existing = json.dumps({"filters": [{"criteria": {"query": f["query"]}} for f in inbox_filters.FILTERS]})
    res2 = inbox_filters.apply_filters("hal@x", "canopy", runner=_runner(existing))
    assert res2["applied"] == []


def test_apply_raises_on_error():
    import pytest
    with pytest.raises(inbox_filters.FilterError):
        inbox_filters.apply_filters("hal@x", "canopy", runner=_runner('{"filters": null}', create_rc=1))


class _SweepRunner:
    """Fake gog: search pages through `pages` per query, records modify calls.
    Ada's 2026-07-14 fleet sweep 'archived' 184 messages that never moved: the real
    sweep passed --remove-label (gog wants --remove=), discarded the modify result,
    and reported search MATCHES as swept. These tests pin the fixed contract."""

    def __init__(self, pages, modify_rc=0):
        self.pages = dict(pages)   # query-substring -> list of page thread-id lists
        self.modify_rc = modify_rc
        self.modified = []         # (thread_id, remove_arg)

    def __call__(self, cmd, **kw):
        if "search" in cmd:
            for key, page_list in self.pages.items():
                if any(key in c for c in cmd):
                    ids = page_list.pop(0) if page_list else []
                    payload = {"threads": [{"id": i} for i in ids]}
                    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
            return SimpleNamespace(returncode=0, stdout='{"threads": []}', stderr="")
        if "modify" in cmd:
            remove = next((c for c in cmd if c.startswith("--remove")), "")
            tid = cmd[cmd.index("modify") + 1]
            self.modified.append((tid, remove))
            return SimpleNamespace(returncode=self.modify_rc, stdout="{}", stderr="boom" if self.modify_rc else "")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")


def test_sweep_uses_correct_flag_and_counts_only_successes():
    r = _SweepRunner({inbox_filters.FILTERS[0]["query"][:20]: [["t1", "t2"]]})
    res = inbox_filters.sweep_existing("hal@x", "canopy", runner=r)
    assert res[inbox_filters.FILTERS[0]["name"]] == 2
    # one modify per thread, with gog's actual flag syntax: --remove=INBOX,UNREAD
    assert ("t1", "--remove=INBOX,UNREAD") in r.modified
    assert ("t2", "--remove=INBOX,UNREAD") in r.modified


def test_sweep_failed_modifies_raise_instead_of_lying():
    import pytest
    r = _SweepRunner({inbox_filters.FILTERS[0]["query"][:20]: [["t1", "t2"]]}, modify_rc=1)
    with pytest.raises(inbox_filters.FilterError, match="modify failed"):
        inbox_filters.sweep_existing("hal@x", "canopy", runner=r)


def test_sweep_pages_past_50_result_cap():
    page1 = [f"t{i}" for i in range(50)]
    page2 = ["t50", "t51"]
    r = _SweepRunner({inbox_filters.FILTERS[0]["query"][:20]: [page1, page2]})
    res = inbox_filters.sweep_existing("hal@x", "canopy", runner=r)
    assert res[inbox_filters.FILTERS[0]["name"]] == 52    # drained, not capped at 50


# --- alarm mail must survive the noreply rule (2026-08-26) ---------------------------------

def test_automated_noreply_does_not_swallow_cloudwatch_alarm_mail():
    """CloudWatch alarms arrive as `Labs Alerts <no-reply@sns.amazonaws.com>`. Without the
    carve-out the noreply rule archives + marks them read on arrival, and since the runner
    polls `in:inbox is:unread`, the alarm never spawns a turn — the agent is not slow to a
    page, it never hears about it. Pins the exclusion and the retirement of the old query."""
    rule = next(f for f in inbox_filters.FILTERS if f["name"] == "automated-noreply")
    assert "-from:sns.amazonaws.com" in rule["query"], \
        "alerting senders must be excluded from the noreply junk rule"
    assert 'from:(noreply' in rule["query"], "the junk-catching half must still be there"
    assert any("sns.amazonaws.com" not in q for q in rule["supersedes"]), \
        "the pre-carve-out query must be listed as superseded, or it stays live beside this one"


def test_sweep_inherits_the_carve_out():
    """sweep_existing builds its search from the SAME query, so the retroactive pass must not
    archive alarm mail either — the live Gmail filter's carve-out does nothing for a sweep."""
    searched = []

    def run(cmd, **kw):
        if "search" in cmd:
            searched.append(next(c for c in cmd if c.startswith("in:inbox")))
            return SimpleNamespace(returncode=0, stdout='{"threads": []}', stderr="")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    inbox_filters.sweep_existing("hal@x", "canopy", runner=run)
    noreply = [q for q in searched if "noreply" in q]
    assert noreply and all("-from:sns.amazonaws.com" in q for q in noreply)


# --- supersedes: editing a query must RETIRE the old filter, not orphan it -----------------

class _SupersedeRunner:
    def __init__(self, live, delete_rc=0):
        self.live = live          # list of (id, query)
        self.delete_rc = delete_rc
        self.deleted, self.created = [], []

    def __call__(self, cmd, **kw):
        if "list" in cmd:
            payload = {"filters": [{"id": i, "criteria": {"query": q}} for i, q in self.live]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if "delete" in cmd:
            self.deleted.append(cmd[cmd.index("delete") + 1])
            return SimpleNamespace(returncode=self.delete_rc, stdout="{}",
                                   stderr="boom" if self.delete_rc else "")
        if "create" in cmd:
            self.created.append(cmd[cmd.index("--query") + 1])
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")


def _stale_query():
    rule = next(f for f in inbox_filters.FILTERS if f["name"] == "automated-noreply")
    return rule["supersedes"][0]


def test_supersedes_deletes_the_stale_filter_and_installs_the_new_one():
    r = _SupersedeRunner(live=[("STALE1", _stale_query())])
    res = inbox_filters.apply_filters("hal@x", "canopy", runner=r)
    assert r.deleted == ["STALE1"], "the superseded filter must be removed, not left beside"
    assert "automated-noreply" in res["applied"]
    assert res["superseded"] == ["automated-noreply:STALE1"]


def test_supersedes_retires_the_stale_filter_even_when_the_new_one_is_already_live():
    """The exact shape found on hal@: a hand-patched filter carrying the carve-out AND the
    original un-carved rule still live beside it. Gmail applies both, so alarm mail is still
    archived — skipping the rule as 'already present' would leave the bug in place."""
    current = next(f for f in inbox_filters.FILTERS if f["name"] == "automated-noreply")["query"]
    r = _SupersedeRunner(live=[("STALE1", _stale_query()), ("GOOD", current)])
    res = inbox_filters.apply_filters("hal@x", "canopy", runner=r)
    assert r.deleted == ["STALE1"]
    assert "automated-noreply" in res["skipped"]
    assert current not in r.created, "the surviving carve-out filter must not be duplicated"


def test_supersede_delete_failure_is_loud():
    import pytest
    r = _SupersedeRunner(live=[("STALE1", _stale_query())], delete_rc=1)
    with pytest.raises(inbox_filters.FilterError):
        inbox_filters.apply_filters("hal@x", "canopy", runner=r)


def test_dry_run_reports_supersessions_without_deleting():
    r = _SupersedeRunner(live=[("STALE1", _stale_query())])
    res = inbox_filters.apply_filters("hal@x", "canopy", runner=r, dry_run=True)
    assert r.deleted == [] and res["superseded"] == ["automated-noreply:STALE1"]


def test_filter_without_an_id_still_counts_as_present():
    """Guards the leniency in _existing_filters: an id-less entry can't be deleted, but
    dropping it would turn every run into 'create everything again'."""
    live = [("", f["query"]) for f in inbox_filters.FILTERS]
    res = inbox_filters.apply_filters("hal@x", "canopy", runner=_SupersedeRunner(live=live))
    assert res["applied"] == []
