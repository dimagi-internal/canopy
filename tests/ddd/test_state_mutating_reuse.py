"""State-mutating narratives: per-render reseed must not defeat reuse or the guard.

Measured on connect-labs ``supply-sophie-rutf-2026-09-26-001`` (canopy 0.2.528):
``setup: rerun: per_render`` minted new ids every take, so ``judge_scope plan``
re-judged 7/7 scenes every iteration (the fingerprints hashed resolved ids) and
``regression_guard`` warned on ~10 "disappeared" id-bearing actions per
iteration. And the user-artifact judge's findings never reached ``assemble``.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts.ddd import judge_scope, regression_guard, stable_ids
from scripts.ddd.run_pipeline import _ordinal


def _report(
    tender: int, quote: int, *, ok: bool = True, drop_click: bool = False, as_of: str = "2026-08-20"
) -> dict:
    actions = [
        {
            "scene_index": 1,
            "kind": "hover",
            "target": f"css:[data-testid=overview-row][data-tender-id='{tender}']",
            "ok": True,
        },
        {
            "scene_index": 2,
            "kind": "goto",
            "target": f"/supply/procurement/tenders/{tender}/compare/?program_id=10672",
            "ok": True,
        },
        {
            "scene_index": 2,
            "kind": "capture",
            "target": "css:[data-testid=ranked-row]",
            "ok": True,
            "capture_var": "answered_quote_id",
            "capture_value": str(quote),
        },
    ]
    if not drop_click:
        actions.append(
            {
                "scene_index": 2,
                "kind": "click",
                "target": f"css:form:has(input[name=quote_id][value='{quote}']) button",
                "ok": ok,
            }
        )
    return {
        "setup": {
            "rerun": "per_render",
            "variables": {
                "program_id": 10672,
                "round2_tender_id": tender,
                "as_of_date": as_of,
                "flag": True,
                "n": 7,
            },
        },
        "actions": actions,
    }


def _render(
    run: Path,
    tender: int,
    quote: int,
    *,
    text_extra: str = "",
    png: bytes = b"png",
    as_of: str = "2026-08-20",
) -> Path:
    snaps = run / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    for i in (1, 2):
        (snaps / f"scene_{i}.png").write_bytes(png + str(i).encode())
        (snaps / f"scene_{i}_page_text.json").write_text(
            json.dumps(
                {
                    "scene_index": i,
                    "url": f"https://x/supply/procurement/tenders/{tender}/compare/",
                    "page_text": f"Tender {tender} quote {quote} as of {as_of}{text_extra}",
                    "render_id": f"r-{tender}",
                }
            )
        )
    (run / "run-report.json").write_text(json.dumps(_report(tender, quote, as_of=as_of)))
    spec = run / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {"name": "t", "scenes": [{"title": "One"}, {"title": "Two"}]}
        )
    )
    return spec


class TestStableIds:
    def test_bindings_come_from_setup_and_captures(self) -> None:
        v = stable_ids.resolved_vars(_report(94, 232))
        assert v["round2_tender_id"] == "94" and v["answered_quote_id"] == "232"
        # bools and one-character values cannot be matched safely
        assert "flag" not in v and "n" not in v

    def test_only_whole_tokens_are_replaced(self) -> None:
        v = {"round2_tender_id": "94"}
        assert stable_ids.unsubstitute("tenders/94/compare", v) == "tenders/${round2_tender_id}/compare"
        assert stable_ids.unsubstitute("in 1942, 194 and 945", v) == "in 1942, 194 and 945"
        assert stable_ids.unsubstitute("id='94'", v) == "id='${round2_tender_id}'"

    def test_page_text_only_normalises_id_named_variables(self) -> None:
        v = {"round2_tender_id": "94", "as_of_date": "2026-08-20", "quoteId": "12"}
        assert set(stable_ids.id_vars(v)) == {"round2_tender_id", "quoteId"}
        assert "paid" not in stable_ids.id_vars({"paid": "123"})


class TestReuseAcrossReseeds:
    def test_a_reseed_alone_does_not_change_any_scene(self, tmp_path: Path) -> None:
        spec = _render(tmp_path, 92, 227)
        before = judge_scope.fingerprints(tmp_path, judge_scope.spec_scenes(spec))
        _render(tmp_path, 94, 232)
        after = judge_scope.fingerprints(tmp_path, judge_scope.spec_scenes(spec))
        assert before == after

    def test_a_real_text_change_still_rejudges(self, tmp_path: Path) -> None:
        spec = _render(tmp_path, 92, 227)
        before = judge_scope.fingerprints(tmp_path, judge_scope.spec_scenes(spec))
        _render(tmp_path, 94, 232, text_extra=" — now provisional")
        after = judge_scope.fingerprints(tmp_path, judge_scope.spec_scenes(spec))
        assert all(before[s] != after[s] for s in before)

    def test_a_non_id_variable_on_the_page_is_never_masked(self, tmp_path: Path) -> None:
        # The as-of date the page SHOWS moved: that is content, not an id.
        spec = _render(tmp_path, 92, 227, as_of="2026-08-20")
        before = judge_scope.fingerprints(tmp_path, judge_scope.spec_scenes(spec))
        _render(tmp_path, 94, 232, as_of="2026-08-27")
        after = judge_scope.fingerprints(tmp_path, judge_scope.spec_scenes(spec))
        assert all(before[s] != after[s] for s in before)

    def test_a_frame_change_still_rejudges_and_says_which_input(self, tmp_path: Path) -> None:
        spec = _render(tmp_path, 92, 227)
        judge_scope.plan(tmp_path, spec)
        judge_scope.record(tmp_path, spec, iteration=1)
        _render(tmp_path, 94, 232, png=b"other")
        scope = judge_scope.plan(tmp_path, spec)
        assert scope["full"] is False and scope["rejudge"] == [1, 2]
        # v3 (canopy#780): a frame that does not decode falls back to exact bytes,
        # and both image components (the region crop and the layout guard) say so.
        assert scope["changed_components"] == {
            "1": ["layout", "region_image"],
            "2": ["layout", "region_image"],
        }

    def test_reseeded_render_reuses_every_scene(self, tmp_path: Path) -> None:
        spec = _render(tmp_path, 92, 227)
        judge_scope.plan(tmp_path, spec)
        judge_scope.record(tmp_path, spec, iteration=1)
        _render(tmp_path, 94, 232)
        scope = judge_scope.plan(tmp_path, spec)
        assert (scope["rejudge"], scope["reuse"], scope["arc"]) == ([], [1, 2], False)

    def test_a_ledger_from_the_old_scheme_forces_an_honest_full_pass(self, tmp_path: Path) -> None:
        spec = _render(tmp_path, 92, 227)
        cache = tmp_path / "judge-cache"
        cache.mkdir()
        # exactly what 0.2.528 wrote: no fingerprint_version, no components
        (cache / "index.json").write_text(
            json.dumps({"iteration": 2, "full": False, "fingerprints": {"1": "a", "2": "b"}})
        )
        scope = judge_scope.plan(tmp_path, spec)
        assert scope["full"] is True and "scheme v1" in scope["reason"]


class TestRegressionGuardAcrossReseeds:
    def _write(self, run: Path, report: dict) -> dict:
        (run / "run-report.json").write_text(json.dumps(report))
        return regression_guard.record(run)

    def test_reseeded_ids_are_not_disappeared_actions(self, tmp_path: Path) -> None:
        self._write(tmp_path, _report(92, 227))
        out = self._write(tmp_path, _report(94, 232))
        assert out["verdict"] == "pass" and out["disappeared"] == []

    def test_a_really_dropped_action_still_warns(self, tmp_path: Path) -> None:
        self._write(tmp_path, _report(92, 227))
        out = self._write(tmp_path, _report(94, 232, drop_click=True))
        assert out["verdict"] == "warn"
        assert [d["target"] for d in out["disappeared"]] == [
            "css:form:has(input[name=quote_id][value='${answered_quote_id}']) button"
        ]

    def test_a_now_failing_action_still_fails(self, tmp_path: Path) -> None:
        self._write(tmp_path, _report(92, 227))
        out = self._write(tmp_path, _report(94, 232, ok=False))
        assert out["verdict"] == "fail"

    def test_history_written_by_an_older_guard_still_compares(self, tmp_path: Path) -> None:
        report = _report(92, 227)
        old = {
            "iteration": None,
            "actions": regression_guard._snapshot_actions(report),
            "scores": {},
            "ok": 4,
            "total": 4,
        }
        (tmp_path / "iteration-history.json").write_text(json.dumps([old]))
        out = self._write(tmp_path, report)
        assert out["verdict"] == "pass"


class TestUserFindingsReachTheLoop:
    def test_user_verdict_findings_are_loaded_in_the_contract(self, tmp_path: Path) -> None:
        from scripts.ddd import assemble

        (tmp_path / "design_findings.json").write_text(json.dumps([{"scene": "1", "detail": "d"}]))
        (tmp_path / "verdict-user.yaml").write_text(
            yaml.safe_dump(
                {
                    "findings": [
                        {
                            "scene": 3,
                            "dimension": "clarity",
                            "score": 3,
                            "fix_kind": "mechanical",
                            "fix_recommendation": "Gloss 'RFQ issued'.",
                        },
                        "not a finding",
                    ]
                }
            )
        )
        found = assemble._load_findings(tmp_path)
        assert len(found) == 2
        user = found[1]
        assert user["source"] == "user_artifact" and user["route"] == "PRODUCT"
        assert user["detail"] == "Gloss 'RFQ issued'."

    def test_merge_user_keeps_reused_scenes_findings(self) -> None:
        prior = {
            "dimensions": {"clarity": {"score": 3}},
            "per_scene": {1: {"clarity": 3}, 2: {"clarity": 4}},
            "findings": [
                {"scene": 1, "fix_recommendation": "reused scene defect"},
                {"scene": "2: title", "fix_recommendation": "stale, scene re-judged"},
            ],
        }
        partial = {
            "dimensions": {"clarity": {"score": 4}},
            "per_scene": {2: {"clarity": 4}},
            "findings": [{"scene": 2, "fix_recommendation": "fresh"}],
        }
        merged = judge_scope.merge_user(prior, partial, [1])
        assert [f["fix_recommendation"] for f in merged["findings"]] == [
            "fresh",
            "reused scene defect",
        ]


def test_ordinals() -> None:
    assert [_ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 111)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd", "111th",
    ]
