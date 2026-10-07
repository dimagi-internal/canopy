"""canopy#789 — autonomous narrative review (scripts.ddd.narrative_guard)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.ddd import loop_config
from scripts.ddd import narrative_guard as ng

BRIEF = {
    "problem": "Sophie runs supplier tenders by email and loses track of who answered.",
    "spine": [
        {"id": "S1", "claim": "The tender record shows which suppliers quoted, which are silent, and what is missing."},
        {"id": "S2", "claim": "Sophie sends a reminder to the silent suppliers from the record."},
    ],
}


def _spec(scenes=None, narrative="Sophie checks her tender record."):
    return {
        "name": "supply-sophie-sheets",
        "narrative": narrative,
        "base_url": "http://x",
        "personas": {"sophie": {"name": "Sophie", "role": "procurement officer", "color": "#000", "intro": ""}},
        "scenes": scenes
        or [
            {
                "id": "record",
                "persona": "sophie",
                "title": "The tender record",
                "show": "",
                "concept_claim": "",
                "provenance": "S1",
                "narrative": "Sophie opens the tender record and sees three suppliers quoted and three are silent.",
                "features": [{"id": "tender-record", "description": "Tender record lists each supplier's quote status", "verify": "x"}],
            },
            {
                "id": "remind",
                "persona": "sophie",
                "title": "Remind the silent suppliers",
                "show": "",
                "concept_claim": "",
                "provenance": "S2",
                "narrative": "She sends one reminder to the silent suppliers.",
                "features": [{"id": "send-reminder", "description": "Send a reminder to silent suppliers", "verify": "x"}],
            },
        ],
    }


@pytest.fixture
def repo(tmp_path):
    spec = tmp_path / "docs" / "walkthroughs" / "supply-sophie-sheets.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text(yaml.safe_dump(_spec()))
    (spec.parent / "supply-sophie-sheets.why_brief.yaml").write_text(yaml.safe_dump(BRIEF))
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    return spec, run_dir


def _edit(spec: Path, fn):
    raw = yaml.safe_load(spec.read_text())
    fn(raw)
    spec.write_text(yaml.safe_dump(raw))


def test_first_check_records_v0_and_the_baseline(repo):
    spec, run_dir = repo
    rec = ng.check(spec, run_dir)
    assert rec["decision"] == "baseline" and rec["version"] == 0
    ledger = ng.load_ledger(spec)
    assert "silent" in ledger["baseline"]["brief"]
    assert ng.ledger_path(spec).name == "supply-sophie-sheets.intent.yaml"
    assert ng.check(spec, run_dir)["decision"] == "unchanged"


def test_comparable_feature_nobody_asked_for_is_rejected(repo):
    # The supply-sophie-sheets case: "comparable" crept in as a narrated feature.
    spec, run_dir = repo
    ng.check(spec, run_dir)

    def add(raw):
        raw["scenes"][0]["features"].append(
            {"id": "all-comparable", "description": "Quotes flagged comparable when units match", "verify": "x"}
        )
        raw["scenes"][0]["narrative"] += " All three of three quotes are comparable."

    _edit(spec, add)
    rec = ng.check(spec, run_dir, reason="tell the story better")
    assert rec["decision"] == "reject"
    assert any(v["rule"] == ng.UNREQUESTED_FEATURE and "comparable" in v["detail"] for v in rec["violations"])
    assert rec["version"] is None
    # A rejected edit is not a version: the last accepted one stays the reference.
    assert ng.latest_version(run_dir)[0] == 0
    assert ng.unchecked_change(spec, run_dir) is True


def test_a_recorded_human_steer_grounds_the_feature(repo):
    spec, run_dir = repo
    ng.check(spec, run_dir)
    ng.add_steer(spec, "show whether the quotes are comparable", by="Jonathan", require=["comparable"])
    _edit(
        spec,
        lambda raw: raw["scenes"][0]["features"].append(
            {"id": "all-comparable", "description": "Quotes flagged comparable when units match", "verify": "x"}
        ),
    )
    rec = ng.check(spec, run_dir, reason="the human asked for it")
    assert rec["decision"] == "accept", rec["violations"]
    assert rec["material"] and "features added" in rec["material_why"][0]


def test_email_focus_returning_after_a_steer_is_rejected(repo):
    spec, run_dir = repo
    ng.check(spec, run_dir)
    ng.add_steer(
        spec,
        "the video I saw was way too focused on the e-mail aspect vs. a clean and clear view",
        by="Jonathan",
        limit=["email"],
    )
    _edit(
        spec,
        lambda raw: raw["scenes"][1].__setitem__(
            "narrative", "She drafts an email, edits the email, and sends the email to the silent suppliers."
        ),
    )
    rec = ng.check(spec, run_dir, reason="restore the reminder flow")
    assert rec["decision"] == "reject"
    v = [v for v in rec["violations"] if v["rule"] == ng.CONTRADICTS_STEER]
    assert v and "email" in v[0]["detail"] and "e-mail aspect" in v[0]["detail"]


def test_forbidden_term_brought_back_is_rejected_and_standing_ones_reported(repo):
    spec, run_dir = repo
    ng.check(spec, run_dir)
    ng.add_steer(spec, "comparable is not a key feature", forbid=["comparable"])
    _edit(spec, lambda raw: raw["scenes"][0].__setitem__("narrative", raw["scenes"][0]["narrative"] + " Each is comparable."))
    rec = ng.check(spec, run_dir)
    assert rec["decision"] == "reject"
    assert rec["violations"][0]["rule"] == ng.CONTRADICTS_STEER


def test_judge_driven_edit_that_adds_to_the_story_is_rejected(repo):
    spec, run_dir = repo
    ng.check(spec, run_dir)
    _edit(
        spec,
        lambda raw: raw["scenes"][1].__setitem__(
            "narrative",
            "She sends one reminder to the silent suppliers, choosing the deadline, the tone, the "
            "copy list, and the follow-up date before the record updates itself.",
        ),
    )
    rec = ng.check(spec, run_dir, reason="answer the clarity finding on scene 2")
    assert rec["decision"] == "reject"
    assert any(v["rule"] == ng.JUDGE_DRIVEN for v in rec["violations"])


def test_judge_driven_edit_that_makes_words_follow_the_product_is_accepted(repo):
    spec, run_dir = repo
    ng.check(spec, run_dir)
    _edit(spec, lambda raw: raw["scenes"][1].__setitem__("narrative", "She reminds the silent suppliers."))
    rec = ng.check(spec, run_dir, findings=["f-12"], reason="claim_reality_coherence: narration overstated")
    assert rec["decision"] == "accept"
    assert rec["material"] is False and rec["post_review"] is False


def test_explanatory_narration_is_rejected(repo):
    spec, run_dir = repo
    ng.check(spec, run_dir)
    _edit(
        spec,
        lambda raw: raw["scenes"][0].__setitem__(
            "narrative",
            raw["scenes"][0]["narrative"] + " This means she no longer has to search her inbox to know who answered.",
        ),
    )
    rec = ng.check(spec, run_dir)
    assert [v["rule"] for v in rec["violations"]] == [ng.EXPLANATION]


def test_material_revision_goes_to_the_human_only_in_polish_mode(repo):
    spec, run_dir = repo
    ng.check(spec, run_dir)
    _edit(spec, lambda raw: raw["scenes"].reverse())
    build = ng.check(spec, run_dir, mode=ng.BUILD)
    assert build["decision"] == "accept" and build["material"] and not build["post_review"]
    _edit(spec, lambda raw: raw["scenes"].reverse())
    polish = ng.check(spec, run_dir, mode=ng.POLISH)
    assert polish["material"] and polish["post_review"]


def test_digest_lists_material_drift_and_rejections_not_minor_wording(repo):
    spec, run_dir = repo
    ng.check(spec, run_dir)
    _edit(spec, lambda raw: raw["scenes"][1].__setitem__("narrative", "She reminds the silent suppliers."))
    ng.check(spec, run_dir)
    _edit(spec, lambda raw: raw["scenes"].reverse())
    ng.check(spec, run_dir)
    _edit(spec, lambda raw: raw.__setitem__("narrative", raw["narrative"] + " This shows the whole tender at a glance."))
    ng.check(spec, run_dir)
    d = ng.digest(run_dir)
    assert d["versions"] == 2
    assert len(d["material"]) == 1 and d["material"][0]["why"] == ["scenes reordered"]
    assert len(d["rejected"]) == 1


@pytest.mark.parametrize(
    "configured,objective,expected",
    [
        ("auto", "demo", "polish"),
        ("auto", "product", "build"),
        ("auto", "auto", "build"),
        (None, None, "build"),
        ("polish", "product", "polish"),
        ("build", "demo", "build"),
    ],
)
def test_mode_defaults_from_the_objective(configured, objective, expected):
    assert ng.resolve_mode(configured, objective) == expected


def test_loop_config_reads_narrative_mode():
    assert loop_config.parse({"loop": {"narrative_mode": "polish"}}).loop.narrative_mode == "polish"
    assert loop_config.parse({"loop": {"narrative_mode": "bogus"}}).loop.narrative_mode == "auto"
    assert loop_config.parse({}).loop.narrative_mode == "auto"


def test_cli_check_exits_2_on_reject(repo, monkeypatch, capsys):
    spec, run_dir = repo
    monkeypatch.setattr(ng, "_run_dir", lambda run_id: run_dir)
    monkeypatch.setattr(ng, "_mode_for_run", lambda run_id: ng.BUILD)
    assert ng.main(["check", str(spec), "--run", "r1"]) == 0
    _edit(spec, lambda raw: raw.__setitem__("narrative", raw["narrative"] + " In other words, nothing is lost."))
    assert ng.main(["check", str(spec), "--run", "r1"]) == 2
    assert "NARRATIVE EDIT REJECTED" in capsys.readouterr().out


def test_review_feedback_lands_in_the_ledger(repo):
    from scripts.ddd.narrative import record_feedback_steers

    spec, _ = repo
    n = record_feedback_steers(
        spec,
        {"feedback": [{"scope": "overall", "ref": "", "text": "the story is the record, not the email"},
                      {"scope": "scene", "ref": "remind", "text": ""}]},
        source="resp.json",
    )
    assert n == 1
    steers = ng.load_ledger(spec)["steers"]
    assert steers[0]["quote"] == "the story is the record, not the email"


def test_assemble_safety_net_reviews_an_unchecked_edit(repo):
    from types import SimpleNamespace

    from scripts.ddd.assemble import _narrative_guard

    spec, run_dir = repo
    state = SimpleNamespace(objective="product", iteration=3, narrative_guard=None)
    cfg = loop_config.parse({})
    first = _narrative_guard(state, str(spec), run_dir, cfg)
    assert first["decision"] == "baseline" and first["mode"] == "build"
    _edit(spec, lambda raw: raw.__setitem__("narrative", raw["narrative"] + " Which means nothing slips."))
    out = _narrative_guard(state, str(spec), run_dir, cfg)
    assert out["decision"] == "reject" and state.narrative_guard is out
    # Nothing changed since: the stored result stands, no re-review.
    assert _narrative_guard(state, str(spec), run_dir, cfg) is out


def test_post_refuses_while_the_runs_review_is_pending(monkeypatch, tmp_path, capsys):
    from scripts.ddd import narrative

    spec = tmp_path / "s.yaml"
    spec.write_text("x: 1")

    class RV:
        @staticmethod
        def get_review(rid):
            return {"status": "pending"}

    monkeypatch.setattr(narrative, "_pending_review_for_run", lambda run_id, rv: "rid-1")
    with pytest.raises(SystemExit) as e:
        narrative._cmd_post(str(spec), "run-1")
    assert e.value.code == 3
    assert "still pending" in capsys.readouterr().err
