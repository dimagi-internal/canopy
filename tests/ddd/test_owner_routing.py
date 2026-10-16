"""Owned findings go to their owners as issues — with a fake gh, never the real one."""
from __future__ import annotations

from scripts.ddd import loop_config, owner_routing


class FakeGh:
    def __init__(self, *, available=(True, "ok"), existing: dict | None = None, fail_create: bool = False):
        self._available = available
        self.existing = existing or {}  # repo -> [(marker, url)]
        self.fail_create = fail_create
        self.comments: list[tuple[str, str]] = []
        self.created: list[dict] = []

    def available(self):
        return self._available

    def find_open_issue(self, repo, mark):
        for m, url in self.existing.get(repo, []):
            if m == mark:
                return url
        return None

    def comment(self, url, body):
        self.comments.append((url, body))
        return url + "#issuecomment-1"

    def create(self, repo, title, body, labels):
        if self.fail_create:
            raise RuntimeError("HTTP 403: no write access")
        n = len(self.created) + 100
        self.created.append({"repo": repo, "title": title, "body": body, "labels": labels})
        return f"https://github.com/{repo}/issues/{n}"


def _cfg(**loop) -> loop_config.DDDConfig:
    return loop_config.parse({"loop": loop})


def _summary(oos: list[dict] | None = None, clarity: list[dict] | None = None) -> dict:
    return {
        "run_id": "noora-2026-10-15-001", "narrative_slug": "noora",
        "ending": {"action": "stop_max_iter", "terminal_status": "stopped_not_converged"},
        "out_of_scope": oos or [], "product_clarity": clarity or [],
    }


LABS = {"match": "labs template", "name": "labs-report-template", "owner": "ACE (labs report template)",
        "repo": "dimagi/ace"}


def _oos(fid: str, **kw) -> dict:
    return {"finding_id": fid, "scene": 1, "dimension": "visual_polish", "fix": "Move the floating button",
            "reason": "names a fixed surface", "surface": "labs-report-template",
            "owner": "ACE (labs report template)", **kw}


def test_a_named_issue_gets_this_runs_evidence_as_a_comment():
    gh = FakeGh()
    out = owner_routing.route(_summary([_oos("s1.visual_polish.1", issue="dimagi/ace#2337")]), cfg=_cfg(), client=gh)
    (r,) = out["results"]
    assert r["status"] == "commented" and r["issue"] == "dimagi/ace#2337"
    url, body = gh.comments[0]
    assert url == "https://github.com/dimagi/ace/issues/2337"
    assert "noora-2026-10-15-001" in body and "s1.visual_polish.1" in body and "scene 1" in body
    assert "<!-- ddd-finding:noora/s1.visual_polish.1 -->" in body
    assert not gh.created


def test_an_open_issue_carrying_the_marker_is_commented_not_duplicated():
    mark = owner_routing.marker("noora/s1.visual_polish.1")
    gh = FakeGh(existing={"dimagi/ace": [(mark, "https://github.com/dimagi/ace/issues/9")]})
    out = owner_routing.route(_summary([_oos("s1.visual_polish.1")]), cfg=_cfg(fixed_surfaces=[LABS]), client=gh)
    assert out["results"][0]["status"] == "commented" and not gh.created
    assert gh.comments[0][0] == "https://github.com/dimagi/ace/issues/9"


def test_no_issue_opens_one_in_the_owners_repo_capped_per_run():
    gh = FakeGh()
    rows = [_oos(f"s{i}.visual_polish.1") for i in range(1, 6)]
    out = owner_routing.route(_summary(rows), cfg=_cfg(fixed_surfaces=[LABS], max_new_issues=3), client=gh)
    statuses = [r["status"] for r in out["results"]]
    assert statuses == ["opened", "opened", "opened", "capped", "capped"]
    assert {c["repo"] for c in gh.created} == {"dimagi/ace"} and len(gh.created) == 3
    assert "<!-- ddd-finding:noora/s1.visual_polish.1 -->" in gh.created[0]["body"]
    assert gh.created[0]["labels"] == []


def test_an_owner_without_a_repo_is_listed_never_guessed():
    gh = FakeGh()
    surface = {k: v for k, v in LABS.items() if k != "repo"}
    out = owner_routing.route(_summary([_oos("s1.visual_polish.1"), {"finding_id": "s3.trust.1"}]),
                              cfg=_cfg(fixed_surfaces=[surface]), client=gh)
    assert out["results"] == [] and not gh.comments and not gh.created
    whys = {u["finding_id"]: u["why"] for u in out["unrouted"]}
    assert "add `repo:`" in whys["s1.visual_polish.1"]
    assert "no owner recorded" in whys["s3.trust.1"]


def test_product_clarity_goes_to_the_product_repo_as_documentation():
    gh = FakeGh()
    row = {"finding_id": "s2.clarity.1", "scene": 2, "dimension": "clarity",
           "ambiguity": "whether 'active' counts paused workers"}
    out = owner_routing.route(_summary(clarity=[row]), cfg=_cfg(product_repo="dimagi/connect-labs"), client=gh)
    assert out["results"][0]["status"] == "opened"
    (c,) = gh.created
    assert c["repo"] == "dimagi/connect-labs" and c["labels"] == ["documentation"]
    assert "unambiguous" in c["title"] and "paused workers" in c["title"]
    assert "product documentation" in c["body"]


def test_the_product_repo_defaults_to_the_target_repos_origin():
    for url in ("git@github.com:dimagi/connect-labs.git", "https://github.com/dimagi/connect-labs"):
        assert owner_routing.product_repo(_cfg(), "/repo", _git=lambda _root, u=url: u) == "dimagi/connect-labs"
    assert owner_routing.product_repo(_cfg(), "/repo", _git=lambda _r: "https://gitlab.com/x/y") is None
    assert owner_routing.product_repo(_cfg(), None) is None


def test_without_gh_every_filing_is_recorded_as_intended():
    gh = FakeGh(available=(False, "gh is not authenticated"))
    out = owner_routing.route(_summary([_oos("s1.visual_polish.1", issue="dimagi/ace#2337"),
                                        _oos("s2.visual_polish.1")]),
                              cfg=_cfg(fixed_surfaces=[LABS]), client=gh)
    assert out["mode"] == "dry" and "not authenticated" in out["why"]
    assert [(r["status"], r["action"]) for r in out["results"]] == [("intended", "comment"),
                                                                    ("intended", "comment_or_open")]
    assert not gh.comments and not gh.created


def test_route_off_by_env_sends_nothing(monkeypatch):
    monkeypatch.setenv("CANOPY_DDD_ROUTE", "off")
    gh = FakeGh()
    out = owner_routing.route(_summary([_oos("s1.visual_polish.1", issue="dimagi/ace#2337")]), cfg=_cfg(), client=gh)
    assert out["mode"] == "dry" and out["results"][0]["status"] == "intended" and not gh.comments


def test_rebuilding_the_summary_does_not_file_twice():
    gh = FakeGh()
    s = _summary([_oos("s1.visual_polish.1")])
    first = owner_routing.route(s, cfg=_cfg(fixed_surfaces=[LABS]), client=gh)
    again = owner_routing.route(s, cfg=_cfg(fixed_surfaces=[LABS]), client=gh, prior=first)
    assert len(gh.created) == 1 and again["results"][0]["status"] == "opened"
    assert again["results"][0]["why"] == "already filed for this run"


def test_a_failed_filing_is_recorded_and_the_rest_continue():
    gh = FakeGh(fail_create=True)
    out = owner_routing.route(_summary([_oos("s1.visual_polish.1"), _oos("s2.visual_polish.1", issue="dimagi/ace#1")]),
                              cfg=_cfg(fixed_surfaces=[LABS]), client=gh)
    assert [r["status"] for r in out["results"]] == ["error", "commented"]
    assert "403" in out["results"][0]["why"]


def test_fixed_surface_repo_and_routing_knobs_parse():
    cfg = _cfg(fixed_surfaces=[LABS], product_repo="dimagi/connect-labs", max_new_issues=0)
    assert cfg.loop.surfaces[0].repo == "dimagi/ace"
    assert cfg.loop.product_repo == "dimagi/connect-labs" and cfg.loop.max_new_issues == 0
    assert _cfg().loop.max_new_issues == 3 and _cfg().loop.product_repo is None
    gh = FakeGh()
    out = owner_routing.route(_summary([_oos("s1.visual_polish.1")]), cfg=cfg, client=gh)
    assert out["results"][0]["status"] == "capped" and not gh.created
