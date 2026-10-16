"""The bootstrap reads earlier runs' summaries from canopy-web — a fake API here."""
from __future__ import annotations

from pathlib import Path

from scripts.ddd import prior_runs

LEARNED = {
    "ending": "stop_max_iter", "terminal_status": "stopped_not_converged", "score_history": [2.0, 2.0],
    "never_cleared": [{"cell": "2:clarity", "open_passes": 4}],
    "out_of_scope": [{"finding_id": "s1.visual_polish.1", "scene": 1, "dimension": "visual_polish",
                      "owner": "ACE", "issue": "dimagi/ace#2337", "fix": "Move the floating button"}],
    "calibration": {"judged": 3, "hits": 0, "hit_rate": 0.0, "mean_overshoot": 1.0},
    "filed": [{"kind": "out_of_scope", "status": "commented", "url": "https://github.com/dimagi/ace/issues/2337"}],
}


class FakeApi:
    def __init__(self, by_scope: dict):
        self.by_scope = by_scope
        self.calls: list[dict] = []

    def __call__(self, method, path, body=None, query=None):
        self.calls.append(query)
        key = "subject" if "subject" in query else "repo_slug"
        return 200, self.by_scope.get(key, [])


def _web(_d):
    return "web", "connect-labs"


def _run(ext_id, learned=LEARNED, subject="noora"):
    return {"ext_id": ext_id, "subject": subject, "status": "stopped_not_converged",
            "summary": {"phase": "judge", "learned": learned}}


def test_reads_the_newest_finished_runs_of_the_narrative():
    api = FakeApi({"subject": [_run("noora-3"), _run("noora-2", learned=None), _run("noora-1"), _run("noora-0")]})
    found = prior_runs.fetch("noora", Path("/x"), limit=2, exclude="noora-3", _call=api, _mode=_web)
    assert found["source"] == "canopy-web" and found["scope"] == "narrative"
    assert [r["run_id"] for r in found["runs"]] == ["noora-1", "noora-0"]
    q = api.calls[0]
    assert q["kind"] == "ddd" and q["subject"] == "noora" and q["active"] == "false"


def test_falls_back_to_the_same_repo_when_the_narrative_has_none():
    api = FakeApi({"subject": [], "repo_slug": [_run("other-1", subject="other")]})
    found = prior_runs.fetch("noora", Path("/x"), _call=api, _mode=_web)
    assert found["scope"] == "repo" and found["runs"][0]["run_id"] == "other-1"
    assert api.calls[1]["repo_slug"] == "connect-labs"
    assert "same repo" in prior_runs.brief(found)


def test_local_store_or_unreachable_is_said_not_raised():
    local = prior_runs.fetch("noora", Path("/x"), _mode=lambda _d: ("local", "no canopy-web token"))
    assert local["source"] == "none" and "no canopy-web token" in local["why"]

    def down(*a, **k):
        raise OSError("connection refused")

    gone = prior_runs.fetch("noora", Path("/x"), _call=down, _mode=_web)
    assert gone["source"] == "none" and "unreachable" in gone["why"]
    assert "none to learn from" in prior_runs.brief(gone)


def test_the_brief_is_what_the_loop_can_act_on():
    text = prior_runs.brief({"source": "canopy-web", "scope": "narrative", "runs": [
        {"run_id": "noora-1", "learned": LEARNED}]})
    assert "noora-1: ended `stop_max_iter`" in text and "scores 2, 2" in text
    assert "never cleared (brief a DIFFERENT approach): 2:clarity (4 passes)" in text
    assert "park up front: scene 1 visual_polish — ACE dimagi/ace#2337" in text
    assert "predictions: 0/3 hit, mean overshoot 1.0" in text
    assert "already filed (out_of_scope, commented): https://github.com/dimagi/ace/issues/2337" in text
