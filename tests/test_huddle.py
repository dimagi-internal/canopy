"""The pure half of `canopy huddle` — ids, keys, tags, the `work` type's templates,
schema and gates. No I/O (huddle_cli / huddle_store carry that)."""
import datetime as dt

import pytest

from orchestrator import huddle as H


def test_id_and_suffix():
    d = dt.date(2026, 10, 6)
    assert H.huddle_id("work", "fleet", d, set()) == "work-fleet-20261006"
    assert H.huddle_id("work", "fleet", d, {"work-fleet-20261006"}) == "work-fleet-20261006-2"
    assert H.huddle_id("work", "fleet", d,
                       {"work-fleet-20261006", "work-fleet-20261006-2"}) == "work-fleet-20261006-3"


def test_keys_and_refs():
    assert H.idempotency_key("h", "eva", 2, 1) == "huddle-h-eva-r2-a1"
    ref = H.round_origin_ref("h", "work", 2, "eva", 1)
    assert ref == {"kind": "huddle_round", "huddle": "h", "type": "work", "round": 2,
                   "member": "eva", "attempt": 1, "thread_key": "huddle-h-eva-r2-a1"}
    a = H.anchor_origin_ref("h", "work", "fleet", "ada", ["eva"])
    assert a == {"kind": "huddle", "huddle": "h", "type": "work", "team": "fleet",
                 "leader": "ada", "members": ["eva"]}


R1_CTX = {"huddle": "h", "member": "eva", "leader": "ada", "principal": "Jonathan",
          "days": 7, "since": "2026-09-29", "context": "ctx", "prior": "none",
          "sharing_rule": "share freely", "round": 1}


def test_work_type_loads_from_package_data():
    ht = H.load_type("work")
    assert set(ht.rounds) == {1, 2, 3}
    out = H.render_round(ht, 1, R1_CTX)
    assert "```huddle" in out and '"huddle": "h"' in out and "{{" not in out


def test_every_round_template_files_its_closeout_with_a_session_id():
    """`canopy agent turn` refuses without --session-id (or --upload): a template that
    omitted it would make every member's close-out fail, and the reply would be lost."""
    ht = H.load_type("work")
    for n, tmpl in ht.rounds.items():
        assert "canopy agent turn" in tmpl, n
        assert '--session-id "huddle:{{huddle}}:{{member}}:r{{round}}"' in tmpl, n


def test_unknown_type_is_a_clear_error():
    with pytest.raises(ValueError, match="nope"):
        H.load_type("nope")


def test_render_missing_var_names_it():
    with pytest.raises(ValueError, match="context"):
        H.render_round(H.load_type("work"), 1, {"huddle": "h"})


def test_validate_round1():
    ht = H.load_type("work")
    ok = {"huddle": "h", "round": 1, "member": "eva", "worked_on": ["a"],
          "priorities": ["p (from x)"], "projects": [], "offers": [], "needs": []}
    assert H.validate_block(ht, 1, ok) == []
    probs = H.validate_block(ht, 1, {**ok, "worked_on": ["1", "2", "3", "4", "5", "6"]})
    assert any("worked_on" in p for p in probs)
    no_pri = {k: v for k, v in ok.items() if k != "priorities"}
    assert any("priorities" in p for p in H.validate_block(ht, 1, no_pri))
    assert H.validate_block(ht, 1, ["not", "a", "dict"]) == ["block is not a JSON object"]


def test_validate_round2_proposal_fields():
    ht = H.load_type("work")
    prop = {"title": "t", "lead": "eva", "with": [], "priority": "p", "project": {"name": "x"},
            "why": "w", "plan": ["s"], "effort": "S", "success_measure": "m", "confidence": 0.5}
    ok = {"huddle": "h", "round": 2, "member": "eva", "proposals": [prop]}
    assert H.validate_block(ht, 2, ok) == []
    bad = {**ok, "proposals": [{k: v for k, v in prop.items() if k != "why"}]}
    assert H.validate_block(ht, 2, bad) == ["proposals[0] missing why"]
    assert any("max 3" in p for p in H.validate_block(ht, 2, {**ok, "proposals": [prop] * 4}))


def test_extract_block_checks_huddle_round_and_takes_the_last():
    text = ('```huddle\n{"huddle": "h", "round": 1, "member": "eva", "worked_on": ["old"]}\n```\n'
            'later\n'
            '```huddle\n{"huddle": "h", "round": 1, "member": "eva", "worked_on": ["new"]}\n```')
    b, err = H.extract_block(text, "h", 1)
    assert b["worked_on"] == ["new"] and err == ""
    assert H.extract_block(text, "h2", 1)[0] is None
    assert H.extract_block(text, "h", 2)[0] is None
    b, err = H.extract_block("```huddle\n{not json\n```", "h", 1)
    assert b is None and "JSON" in err


def _p(title, lead="eva", with_=(), priority="Q4 pipeline", answers=None, critique=True,
       project="Q4"):
    return {"title": title, "lead": lead, "with": list(with_), "priority": priority,
            "project": {"name": project, "new": False}, "answers": answers or {},
            "critique_answered": critique}


def test_gates_joint_needs_all_cosigns():
    filed, held = H.work_gates([_p("A", with_=["echo"], answers={"echo": "co-sign"}),
                                _p("B", with_=["echo", "hal"], answers={"echo": "co-sign"}),
                                _p("C", with_=["echo"], answers={"echo": "decline"}),
                                _p("D", with_=["echo"], answers={"echo": "amend"})],
                               {"Q4 pipeline"}, [])
    assert [p["title"] for p in filed] == ["A"]
    assert {p["title"]: p["held"] for p in held} == {
        "B": "hal has not co-signed", "C": "echo declined",
        "D": "amend unresolved (echo)"}


def test_gates_lead_listed_in_with_is_not_its_own_partner():
    filed, _ = H.work_gates([_p("A", with_=["eva"])], {"Q4 pipeline"}, [])
    assert [p["title"] for p in filed] == ["A"]


def test_gates_a_lead_named_by_someone_else_must_cosign():
    # eva proposed it but named hal as lead: hal never consented, so hal must answer too.
    base = _p("Diagnose MCP", lead="hal", with_=["hal", "eva"])
    filed, held = H.work_gates([{**base, "proposed_by": "eva", "answers": {"eva": "co-sign"}}],
                               {"Q4 pipeline"}, [])
    assert not filed and held[0]["held"] == "hal has not co-signed"
    filed, _ = H.work_gates([{**base, "proposed_by": "eva",
                              "answers": {"eva": "co-sign", "hal": "co-sign"}}],
                            {"Q4 pipeline"}, [])
    assert [p["title"] for p in filed] == ["Diagnose MCP"]
    # the lead's own proposal needs no self-co-sign
    filed, _ = H.work_gates([{**_p("Mine", lead="hal"), "proposed_by": "hal"}], {"Q4 pipeline"}, [])
    assert [p["title"] for p in filed] == ["Mine"]


def test_gates_priority_project_critique_cap_and_declined():
    props = [_p(f"P{i}") for i in range(7)] + [
        _p("X", priority="unknown"), _p("E", priority=""), _p("Y", critique=False),
        _p("N", project=""), _p("Old thing")]
    filed, held = H.work_gates(props, {"Q4 pipeline (from goals sheet)"},
                               [{"title": "Old thing", "fate": "declined: not now"}])
    assert len(filed) == 5
    reasons = {p["title"]: p["held"] for p in held}
    assert reasons["X"].startswith("priority not stated in any round-1 report")
    assert reasons["E"].startswith("priority not stated in any round-1 report")
    assert reasons["Y"] == "critique not answered"
    assert reasons["N"] == "no project named"
    assert reasons["Old thing"].startswith("declined before")
    assert reasons["P5"] == "over the 5-outcome cap"


def test_gates_declined_repeat_passes_with_new_evidence():
    p = {**_p("Old thing"), "new_evidence": "the funder replied 2026-10-05"}
    filed, _ = H.work_gates([p], {"Q4 pipeline"}, [{"title": "old  thing!", "fate": "declined: x"}])
    assert [x["title"] for x in filed] == ["Old thing"]


def test_outcome_record_shape():
    rec = H.outcome_record({"id": "h", "type": "work", "team": "fleet", "leader": "ada",
                            "members": ["eva"], "leader_turn": "t"},
                           [{**_p("A", with_=["echo"], answers={"echo": "co-sign"}),
                             "task": {"agent": "eva", "ext_id": "T1"}, "why": "w"}],
                           [{**_p("B"), "held": "x"}], [{"member": "hal", "why": "timed out"}])
    assert rec["version"] == 1 and rec["id"] == "h" and rec["leader_turn"] == "t"
    a, b = rec["outcomes"]
    assert a["fate"] == "filed" and a["partners"] == ["echo"] and a["cosign"] == {"echo": "co-sign"}
    assert a["task"] == {"agent": "eva", "ext_id": "T1"} and a["why"] == "w"
    assert b["fate"] == "held: x" and rec["not_reached"][0]["member"] == "hal"
    assert rec["finished_at"]
