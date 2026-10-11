"""Tests for the shareout gather/post pipeline (deterministic parts only)."""
import datetime as dt
import json
from pathlib import Path

import pytest

from orchestrator import shareout


# ---------------------------------------------------------------------------
# resolve_range
# ---------------------------------------------------------------------------

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 6, 4, 14, 30, 0, tzinfo=UTC)
TODAY = dt.date(2026, 6, 4)
YESTERDAY = dt.date(2026, 6, 3)


def _dt(y, m, d, hh=0, mm=0, ss=0):
    return dt.datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


class TestResolveRange:
    def test_explicit_from_and_to_is_whole_day_window(self):
        assert shareout.resolve_range("2026-06-01", "2026-06-02", None, now=NOW) == (
            _dt(2026, 6, 1, 0, 0, 0),
            _dt(2026, 6, 2, 23, 59, 59),
        )

    def test_days_is_rolling_window_ending_now(self):
        assert shareout.resolve_range(None, None, 7, now=NOW) == (
            NOW - dt.timedelta(days=7),
            NOW,
        )

    def test_from_only_extends_to_end_of_today(self):
        assert shareout.resolve_range("2026-06-01", None, None, now=NOW) == (
            _dt(2026, 6, 1, 0, 0, 0),
            _dt(2026, 6, 4, 23, 59, 59),
        )

    def test_to_only_is_single_whole_day(self):
        assert shareout.resolve_range(None, "2026-05-01", None, now=NOW) == (
            _dt(2026, 5, 1, 0, 0, 0),
            _dt(2026, 5, 1, 23, 59, 59),
        )

    def test_no_args_fallback_is_yesterday_whole_day(self):
        assert shareout.resolve_range(None, None, None, now=NOW) == (
            _dt(2026, 6, 3, 0, 0, 0),
            _dt(2026, 6, 3, 23, 59, 59),
        )

    def test_inverted_range_raises(self):
        with pytest.raises(ValueError):
            shareout.resolve_range("2026-06-05", "2026-06-01", None, now=NOW)


# ---------------------------------------------------------------------------
# session_in_range
# ---------------------------------------------------------------------------


class TestSessionInRange:
    def _s(self, first, last):
        return {"first_ts": first, "last_ts": last}

    def test_session_within_window(self):
        s = self._s("2026-06-04T13:00:00Z", "2026-06-04T13:30:00Z")
        assert shareout.session_in_range(s, _dt(2026, 6, 4, 12), _dt(2026, 6, 4, 14))

    def test_session_before_window_excluded(self):
        s = self._s("2026-06-04T09:00:00Z", "2026-06-04T11:00:00Z")
        assert not shareout.session_in_range(s, _dt(2026, 6, 4, 12), _dt(2026, 6, 4, 14))

    def test_session_spanning_into_window(self):
        s = self._s("2026-06-04T11:30:00Z", "2026-06-04T12:30:00Z")
        assert shareout.session_in_range(s, _dt(2026, 6, 4, 12), _dt(2026, 6, 4, 14))

    def test_missing_timestamps_excluded(self):
        assert not shareout.session_in_range(
            {"first_ts": None, "last_ts": None}, _dt(2026, 6, 4), _dt(2026, 6, 4, 23, 59, 59)
        )


# ---------------------------------------------------------------------------
# gather
# ---------------------------------------------------------------------------


def _write_transcript(path: Path, prompts, ts="2026-06-03T10:00:00Z"):
    lines = [{"type": "last-prompt", "sessionId": path.stem}]
    for p in prompts:
        lines.append({"type": "user", "message": {"content": p}, "timestamp": ts})
    path.write_text("\n".join(json.dumps(line) for line in lines))


class TestGather:
    def test_groups_by_repo_and_filters_by_date(self, tmp_path):
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-Users-me-canopy"
        proj.mkdir(parents=True)
        # in-range session
        _write_transcript(proj / "s1.jsonl", ["build the shareout feed"], "2026-06-03T10:00:00Z")
        # out-of-range session (older)
        _write_transcript(proj / "s2.jsonl", ["old work"], "2026-05-01T10:00:00Z")

        repo_map = {"-Users-me-canopy": "jjackson/canopy"}
        corpus = shareout.gather(
            projects_dir=projects_dir,
            repo_map=repo_map,
            labels={},
            start=YESTERDAY,
            end=YESTERDAY,
            fetch_prs_fn=lambda *a, **k: [],
        )
        # dates coerce to whole-day timestamp bounds
        assert corpus["period"]["start"].startswith("2026-06-03T00:00:00")
        assert corpus["period"]["end"].startswith("2026-06-03T23:59:59")
        assert "jjackson/canopy" in corpus["projects"]
        sessions = corpus["projects"]["jjackson/canopy"]["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["prompts"] == ["build the shareout feed"]

    def test_prs_attached_per_repo(self, tmp_path):
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-Users-me-canopy"
        proj.mkdir(parents=True)
        _write_transcript(proj / "s1.jsonl", ["work"], "2026-06-03T10:00:00Z")

        def fake_prs(repo, start, end, author="@me"):
            return [{"number": 1, "title": "Add feed", "url": "u", "state": "MERGED"}]

        corpus = shareout.gather(
            projects_dir=projects_dir,
            repo_map={"-Users-me-canopy": "jjackson/canopy"},
            labels={},
            start=YESTERDAY,
            end=YESTERDAY,
            fetch_prs_fn=fake_prs,
        )
        assert corpus["projects"]["jjackson/canopy"]["prs"][0]["number"] == 1

    def test_project_filter(self, tmp_path):
        projects_dir = tmp_path / "projects"
        for key, repo in [("-Users-me-canopy", "jjackson/canopy"), ("-Users-me-ace", "jjackson/ace")]:
            p = projects_dir / key
            p.mkdir(parents=True)
            _write_transcript(p / "s.jsonl", ["work"], "2026-06-03T10:00:00Z")
        repo_map = {"-Users-me-canopy": "jjackson/canopy", "-Users-me-ace": "jjackson/ace"}
        corpus = shareout.gather(
            projects_dir=projects_dir,
            repo_map=repo_map,
            labels={},
            start=YESTERDAY,
            end=YESTERDAY,
            project_filter="ace",
            fetch_prs_fn=lambda *a, **k: [],
        )
        assert list(corpus["projects"].keys()) == ["jjackson/ace"]


# ---------------------------------------------------------------------------
# build_post_payload
# ---------------------------------------------------------------------------


class TestBuildPostPayload:
    def _authoring(self):
        return {
            "period_start": "2026-06-03",
            "period_end": "2026-06-03",
            "author": "jjackson",
            "rollup": {"title": "Yesterday", "summary": "tl;dr", "content": "## Roll-up"},
            "projects": [
                {
                    "project_slug": "canopy",
                    "title": "Shareouts",
                    "summary": "s",
                    "content": "## What",
                    "links": [{"label": "PR #83", "url": "u"}],
                }
            ],
        }

    def test_rollup_has_null_slug_and_source_stamped(self):
        payload = shareout.build_post_payload(self._authoring(), source="canopy:shareout@T")
        items = payload["shareouts"]
        assert len(items) == 2
        rollup = items[0]
        assert rollup["project_slug"] is None
        assert rollup["source"] == "canopy:shareout@T"
        assert rollup["period_start"] == "2026-06-03"
        assert rollup["author"] == "jjackson"

    def test_project_item_carries_slug_and_links(self):
        payload = shareout.build_post_payload(self._authoring(), source="src")
        proj = payload["shareouts"][1]
        assert proj["project_slug"] == "canopy"
        assert proj["links"] == [{"label": "PR #83", "url": "u"}]
        assert proj["source"] == "src"

    def test_no_rollup_only_projects(self):
        a = self._authoring()
        a["rollup"] = None
        payload = shareout.build_post_payload(a, source="src")
        assert len(payload["shareouts"]) == 1
        assert payload["shareouts"][0]["project_slug"] == "canopy"

    def test_produced_by_agent_stamped_on_every_item(self):
        payload = shareout.build_post_payload(
            self._authoring(), source="src", produced_by_agent="eva"
        )
        assert all(item["produced_by_agent"] == "eva" for item in payload["shareouts"])

    def test_produced_by_agent_omitted_when_empty(self):
        # Human run: the key must be ABSENT (not ""), so a StrictModel canopy-web
        # that predates the field still accepts the post.
        payload = shareout.build_post_payload(self._authoring(), source="src")
        assert all("produced_by_agent" not in item for item in payload["shareouts"])


class TestDetectAgentSlug:
    def test_env_override_wins(self, tmp_path):
        assert shareout.detect_agent_slug(tmp_path, {"CANOPY_AGENT_SLUG": "hal"}) == "hal"

    def test_reads_config_agent_json_slug(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "agent.json").write_text('{"slug": "eva", "name": "Eva"}')
        assert shareout.detect_agent_slug(tmp_path, {}) == "eva"

    def test_falls_back_to_name_lowercased(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "agent.json").write_text('{"name": "Ada"}')
        assert shareout.detect_agent_slug(tmp_path, {}) == "ada"

    def test_empty_when_no_marker(self, tmp_path):
        assert shareout.detect_agent_slug(tmp_path, {}) == ""


class TestResolveDefaultRange:
    def test_no_prior_shareout_falls_back_to_last_24h(self):
        assert shareout.resolve_default_range(None, now=NOW) == (NOW - dt.timedelta(days=1), NOW)

    def test_continues_from_exact_end_of_last_shareout(self):
        last_end = _dt(2026, 6, 3, 17, 30, 0)
        assert shareout.resolve_default_range(last_end, now=NOW) == (last_end, NOW)

    def test_last_shareout_already_at_now_collapses(self):
        # already current (or ahead) → start clamped to now, never inverted
        assert shareout.resolve_default_range(NOW, now=NOW) == (NOW, NOW)
        assert shareout.resolve_default_range(NOW + dt.timedelta(hours=1), now=NOW) == (NOW, NOW)


class TestFetchLatestPeriodEnd:
    @staticmethod
    def _serve(monkeypatch, status, body, seen=None):
        from orchestrator import canopy_web

        def transport(method, url, headers, data):
            if seen is not None:
                seen.append(url)
            return status, body
        monkeypatch.setattr(canopy_web, "urllib_transport", transport)

    def test_parses_period_end_from_items(self, monkeypatch):
        payload = json.dumps({"items": [{"period_end": "2026-06-03T17:30:00Z"}], "total": 1})
        self._serve(monkeypatch, 200, payload)
        assert shareout.fetch_latest_period_end("https://x", "tok") == _dt(2026, 6, 3, 17, 30, 0)

    def test_empty_feed_returns_none(self, monkeypatch):
        self._serve(monkeypatch, 200, '{"items": [], "total": 0}')
        assert shareout.fetch_latest_period_end("https://x", "tok") is None

    def test_network_error_returns_none(self, monkeypatch):
        from orchestrator import canopy_web

        def boom(*a, **k):
            raise OSError("unreachable")
        monkeypatch.setattr(canopy_web, "urllib_transport", boom)
        assert shareout.fetch_latest_period_end("https://x", "tok") is None

    def test_the_feed_read_is_workspace_scoped(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")
        seen = []
        self._serve(monkeypatch, 200, '{"items": []}', seen)
        shareout.fetch_latest_period_end("https://x", "tok")
        assert seen == ["https://x/api/w/connect/shareouts/?limit=1"]

    def test_a_workspace_required_refusal_says_so(self, monkeypatch, capsys):
        monkeypatch.delenv("CANOPY_WEB_WORKSPACE", raising=False)
        self._serve(monkeypatch, 422, json.dumps(
            {"type": "https://canopy-web.dimagi.com/problems/workspace-required", "detail": "x"}))
        assert shareout.fetch_latest_period_end("https://x", "tok") is None
        assert "CANOPY_WEB_WORKSPACE" in capsys.readouterr().err


class TestFillAllPrsFromCorpus:
    def _corpus(self):
        return {
            "period": {"start": "2026-06-03", "end": "2026-06-03"},
            "projects": {
                "jjackson/ace": {"prs": [
                    {"number": 685, "title": "auto-capture", "url": "u685", "state": "MERGED", "body": "x"},
                ]},
                "jjackson/canopy": {"prs": [
                    {"number": 124, "title": "auto-promote", "url": "u124", "state": "MERGED"},
                ]},
            },
        }

    def test_fills_by_repo_basename_match(self):
        authoring = {
            "period_start": "2026-06-03", "period_end": "2026-06-03",
            "projects": [
                {"project_slug": "ace", "title": "t", "content": "c"},
                {"project_slug": "canopy", "title": "t", "content": "c"},
            ],
        }
        shareout.fill_all_prs_from_corpus(authoring, self._corpus())
        ace = authoring["projects"][0]["all_prs"]
        assert ace == [{"number": 685, "title": "auto-capture", "url": "u685", "state": "MERGED"}]
        assert authoring["projects"][1]["all_prs"][0]["number"] == 124

    def test_does_not_overwrite_existing_all_prs(self):
        authoring = {
            "period_start": "2026-06-03", "period_end": "2026-06-03",
            "projects": [{"project_slug": "ace", "title": "t", "content": "c",
                          "all_prs": [{"number": 1, "title": "kept", "url": "u", "state": "OPEN"}]}],
        }
        shareout.fill_all_prs_from_corpus(authoring, self._corpus())
        assert authoring["projects"][0]["all_prs"][0]["title"] == "kept"

    def test_build_payload_passes_all_prs(self):
        authoring = {
            "period_start": "2026-06-03", "period_end": "2026-06-03", "author": "j",
            "projects": [{"project_slug": "ace", "title": "t", "content": "c",
                          "all_prs": [{"number": 685, "title": "x", "url": "u", "state": "MERGED"}]}],
        }
        payload = shareout.build_post_payload(authoring, source="src")
        assert payload["shareouts"][0]["all_prs"][0]["number"] == 685


class TestPrInWindow:
    def _pr(self, created=None, merged=None):
        return {"createdAt": created, "mergedAt": merged}

    def test_merged_in_window(self):
        pr = self._pr(created="2026-06-01T00:00:00Z", merged="2026-06-04T12:00:00Z")
        assert shareout._pr_in_window(pr, _dt(2026, 6, 4), _dt(2026, 6, 5))

    def test_merged_before_window_excluded(self):
        # merged on the prior day — must NOT show in this window even though it
        # was created earlier and would match a coarse date filter
        pr = self._pr(created="2026-06-01T00:00:00Z", merged="2026-06-03T14:00:00Z")
        assert not shareout._pr_in_window(pr, _dt(2026, 6, 3, 23, 59, 59), NOW)

    def test_unmerged_excluded(self):
        # open / closed-without-merge didn't ship → never in any window
        pr = self._pr(created="2026-06-04T09:00:00Z", merged=None)
        assert not shareout._pr_in_window(pr, _dt(2026, 6, 4), _dt(2026, 6, 5))

    def test_boundary_belongs_to_previous_window(self):
        # a PR merged exactly at the shared boundary is in the prior window
        # (..end], not this one ((start..) — no double-count across chained runs
        boundary = _dt(2026, 6, 3, 23, 59, 59)
        pr = self._pr(merged="2026-06-03T23:59:59Z")
        assert shareout._pr_in_window(pr, _dt(2026, 6, 3), boundary)          # prior: ..end]
        assert not shareout._pr_in_window(pr, boundary, NOW)                  # next: (start..

    def test_updated_does_not_resurface(self):
        # old merged PR with no mergedAt change still anchors on merge time
        pr = self._pr(created="2026-05-30T00:00:00Z", merged="2026-05-31T00:00:00Z")
        assert not shareout._pr_in_window(pr, _dt(2026, 6, 4), NOW)


def test_feed_url():
    assert shareout.feed_url("https://x.app/") == "https://x.app/shareouts"
