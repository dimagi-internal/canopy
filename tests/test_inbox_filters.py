"""Fleet inbox filters — the Gmail half of the inbound-email table, applied via gog."""
import json
from types import SimpleNamespace

import pytest

from orchestrator import inbox_filters, inbox_rules


def _flt(name):
    return next(f for f in inbox_filters.FILTERS if f["name"] == name)


class _Gog:
    """A fake gog: labels list/create, filters list/create/delete, search, thread modify."""

    def __init__(self, live=(), labels=(), pages=None, create_rc=0, delete_rc=0, modify_rc=0):
        # live: (id, query) or (id, query, [label names]) — label names map to ids "L:<name>"
        self.live = [(t[0], t[1], list(t[2]) if len(t) > 2 else []) for t in live]
        self.labels = set(labels)
        self.pages = dict(pages or {})
        self.create_rc, self.delete_rc, self.modify_rc = create_rc, delete_rc, modify_rc
        self.created, self.deleted, self.labels_made, self.modified = [], [], [], []

    def __call__(self, cmd, **kw):
        ok = SimpleNamespace(returncode=0, stdout="{}", stderr="")
        if cmd[2] == "labels" and cmd[3] == "list":
            payload = {"labels": [{"id": "INBOX", "name": "INBOX"}]
                       + [{"id": f"L:{n}", "name": n} for n in sorted(self.labels)]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if cmd[2] == "labels" and cmd[3] == "create":
            self.labels.add(cmd[4])
            self.labels_made.append(cmd[4])
            return ok
        if "filters" in cmd and "list" in cmd:
            payload = {"filters": [
                {"id": i, "criteria": {"query": q},
                 "action": {"addLabelIds": [f"L:{n}" for n in lbls],
                            "removeLabelIds": ["UNREAD", "INBOX"]}}
                for i, q, lbls in self.live]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if "filters" in cmd and "delete" in cmd:
            self.deleted.append(cmd[cmd.index("delete") + 1])
            return SimpleNamespace(returncode=self.delete_rc, stdout="{}",
                                   stderr="boom" if self.delete_rc else "")
        if "filters" in cmd and "create" in cmd:
            self.created.append((cmd[cmd.index("--query") + 1], cmd[cmd.index("--add-label") + 1]))
            return SimpleNamespace(returncode=self.create_rc, stdout="{}",
                                   stderr="boom" if self.create_rc else "")
        if "search" in cmd:
            for key, page_list in self.pages.items():
                if any(key in c for c in cmd):
                    ids = page_list.pop(0) if page_list else []
                    return SimpleNamespace(returncode=0, stdout=json.dumps(
                        {"threads": [{"id": i} for i in ids]}), stderr="")
            return SimpleNamespace(returncode=0, stdout='{"threads": []}', stderr="")
        if "modify" in cmd:
            self.modified.append(tuple(cmd[cmd.index("modify") + 1:cmd.index("modify") + 4]))
            return SimpleNamespace(returncode=self.modify_rc, stdout="{}",
                                   stderr="boom" if self.modify_rc else "")
        return ok


# --- FILTERS is derived from the table ----------------------------------------------------

def test_every_filter_archives_marks_read_and_labels_with_its_row():
    assert inbox_filters.FILTERS
    for f in inbox_filters.FILTERS:
        assert f["query"] and f["archive"] and f["mark_read"]
        assert f["add_label"] == f["row"] and inbox_rules.ROWS[f["row"]].bucket == "archive"


def test_wake_rows_never_become_filters():
    assert not any(f["row"].startswith("wanted/") for f in inbox_filters.FILTERS)


def test_ooo_queries_are_the_ones_the_old_rules_ran():
    """The two OOO queries carry months of tuning (see inbox_rules): the table must keep them
    byte-for-byte, or `apply_filters` would treat them as new rules and orphan the old ones."""
    assert _flt("ignored/out-of-office")["query"] == (
        'subject:("out of office" OR "automatic reply" OR "auto-reply" OR autoreply '
        'OR "away from my email" OR "away from the office" OR "offline through" '
        'OR "offline until")')
    q = _flt("ignored/out-of-office#2")["query"].lower()
    assert "subject:(offline" in q
    body = q.split(")", 1)[1]
    assert "i will be offline" in body, \
        "bare subject markers must be ANDed with first-person body phrases, never used alone"


# --- apply ---------------------------------------------------------------------------------

def test_apply_creates_labels_then_labelled_filters_and_is_idempotent():
    g = _Gog()
    res = inbox_filters.apply_filters("hal@x", "canopy", runner=g)
    assert set(g.labels_made) == set(inbox_rules.archive_labels())
    assert res["applied"] == [f["name"] for f in inbox_filters.FILTERS]
    assert {(f["query"], f["add_label"]) for f in inbox_filters.FILTERS} == set(g.created)

    live = [(f"id{i}", f["query"], [f["add_label"]]) for i, f in enumerate(inbox_filters.FILTERS)]
    g2 = _Gog(live=live, labels=inbox_rules.archive_labels())
    res2 = inbox_filters.apply_filters("hal@x", "canopy", runner=g2)
    assert res2["applied"] == [] and g2.created == [] and g2.deleted == []


def test_same_query_without_the_label_is_replaced_create_first():
    """Every pre-table filter has the right query but no label. It must be swapped for the
    labelled one — and the new one created BEFORE the old is deleted, so no mail slips
    through unfiltered in between."""
    q = _flt("ignored/github")["query"]
    order = []
    g = _Gog(live=[("OLD", q)])
    real = g.__call__

    def spy(cmd, **kw):
        if "filters" in cmd and ("create" in cmd or "delete" in cmd):
            order.append("create" if "create" in cmd else "delete")
        return real(cmd, **kw)

    res = inbox_filters.apply_filters("hal@x", "canopy", runner=spy)
    assert (q, "ignored/github") in g.created
    assert g.deleted == ["OLD"]
    assert res["relabelled"] == ["ignored/github:OLD"]
    first_delete = order.index("delete")
    assert "create" in order[:first_delete]


def test_apply_raises_on_create_error():
    with pytest.raises(inbox_filters.FilterError):
        inbox_filters.apply_filters("hal@x", "canopy", runner=_Gog(create_rc=1))


def test_unreadable_labels_is_loud():
    def run(cmd, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="no auth")
    with pytest.raises(inbox_filters.FilterError, match="labels"):
        inbox_filters.apply_filters("hal@x", "canopy", runner=run)


# --- supersedes ----------------------------------------------------------------------------

_OLD_NOREPLY = ('from:(noreply OR no-reply OR donotreply OR "do-not-reply" OR mailer-daemon '
                'OR postmaster) -from:sns.amazonaws.com')


def test_the_old_noreply_filter_is_superseded():
    """The old query's bare `from:noreply` also matched comments-noreply@docs.google.com and
    drive-shares-dm-noreply@google.com (Gmail tokenises addresses), archiving every Doc
    comment — explicit @-mentions of the agent included. It must be deleted, not left
    beside the narrower one."""
    g = _Gog(live=[("STALE1", _OLD_NOREPLY)])
    res = inbox_filters.apply_filters("hal@x", "canopy", runner=g)
    assert "STALE1" in g.deleted
    assert "ignored/no-reply-sender:STALE1" in res["superseded"]


def test_supersede_delete_failure_is_loud():
    with pytest.raises(inbox_filters.FilterError):
        inbox_filters.apply_filters("hal@x", "canopy",
                                    runner=_Gog(live=[("STALE1", _OLD_NOREPLY)], delete_rc=1))


def test_dry_run_changes_nothing():
    g = _Gog(live=[("STALE1", _OLD_NOREPLY)])
    res = inbox_filters.apply_filters("hal@x", "canopy", runner=g, dry_run=True)
    assert g.deleted == [] and g.labels_made == []
    assert res["superseded"] == ["ignored/no-reply-sender:STALE1"]
    assert set(res["labels_created"]) == set(inbox_rules.archive_labels())


def test_filter_without_an_id_still_counts_as_present():
    """An id-less entry can't be deleted, but dropping it would turn every run into
    'create everything again'."""
    live = [("", f["query"], [f["add_label"]]) for f in inbox_filters.FILTERS]
    res = inbox_filters.apply_filters("hal@x", "canopy",
                                      runner=_Gog(live=live, labels=inbox_rules.archive_labels()))
    assert res["applied"] == []


# --- the wanted rows survive the Gmail layer ----------------------------------------------

def test_noreply_filter_spares_alarms_doc_comments_and_other_rows_senders():
    """Gmail applies EVERY matching filter, so the broad no-reply query must exclude the
    senders a wanted row (alarms, Doc comments) or a more specific row owns."""
    q = _flt("ignored/no-reply-sender")["query"]
    assert q.startswith("from:(noreply")
    for excluded in ("-from:sns.amazonaws.com", "-from:docs.google.com",
                     "drive-shares-dm-noreply@google.com", "calendar-noreply@google.com"):
        assert excluded in q


def test_ses_event_receipts_are_junk_but_alarms_are_not():
    """SES receipts share no-reply@sns.amazonaws.com with CloudWatch alarms (14 caller
    sessions on one ace@ thread, 2026-09-23/24). Pinned to the receipts' fixed subject so an
    ALARM:/OK: from the same sender still wakes an agent."""
    q = _flt("ignored/no-reply-sender#2")["query"]
    assert q == 'from:no-reply@sns.amazonaws.com subject:"Amazon SES Email Event Notification"'


def test_no_filter_touches_doc_comment_mail():
    """Doc comments are classified by the runner, which reads the body; no Gmail query may
    archive them first."""
    for f in inbox_filters.FILTERS:
        assert "comments-noreply" not in f["query"]


# --- sweep ---------------------------------------------------------------------------------

def test_sweep_uses_correct_flags_labels_and_counts_only_successes():
    first = inbox_filters.FILTERS[0]
    g = _Gog(pages={first["query"][:20]: [["t1", "t2"]]})
    res = inbox_filters.sweep_existing("hal@x", "canopy", runner=g)
    assert res[first["name"]] == 2
    assert ("t1", "--remove=INBOX,UNREAD", f"--add={first['add_label']}") in g.modified


def test_sweep_failed_modifies_raise_instead_of_lying():
    g = _Gog(pages={inbox_filters.FILTERS[0]["query"][:20]: [["t1", "t2"]]}, modify_rc=1)
    with pytest.raises(inbox_filters.FilterError, match="modify failed"):
        inbox_filters.sweep_existing("hal@x", "canopy", runner=g)


def test_sweep_pages_past_50_result_cap():
    first = inbox_filters.FILTERS[0]
    g = _Gog(pages={first["query"][:20]: [[f"t{i}" for i in range(50)], ["t50", "t51"]]})
    assert inbox_filters.sweep_existing("hal@x", "canopy", runner=g)[first["name"]] == 52
