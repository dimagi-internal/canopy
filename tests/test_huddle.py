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
          "sharing_rule": "share freely", "round": 1, "brief": "", "principal_key": "jonathan",
          "today": "2026-10-09"}


def test_work_type_loads_from_package_data():
    ht = H.load_type("work")
    assert set(ht.rounds) == {1, 2, 3, 4}
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


# ── round 4: the lead resolves an amend ─────────────────────────────────────────
def test_validate_round4_resolutions():
    ht = H.load_type("work")
    revised = {"title": "t", "lead": "eva", "with": ["echo"], "priority": "p",
               "project": {"name": "x"}, "why": "w", "plan": ["s"], "effort": "S",
               "success_measure": "m", "confidence": 0.5}
    ok = {"huddle": "h", "round": 4, "member": "eva",
          "resolutions": [{"title": "t", "lead": "eva", "resolution": "accept", "note": "",
                           "proposal": revised},
                          {"title": "u", "lead": "eva", "resolution": "reject",
                           "note": "public only is too narrow"}]}
    assert H.validate_block(ht, 4, ok) == []
    assert "missing resolutions" in H.validate_block(ht, 4, {k: v for k, v in ok.items()
                                                             if k != "resolutions"})
    no_prop = {**ok, "resolutions": [{k: v for k, v in ok["resolutions"][0].items()
                                      if k != "proposal"}]}
    assert H.validate_block(ht, 4, no_prop) == [
        "resolutions[0] missing proposal (required when resolution is accept)"]
    bad = {**ok, "resolutions": [{**ok["resolutions"][1], "resolution": "maybe"}]}
    assert any("resolution must be one of" in p for p in H.validate_block(ht, 4, bad))


def test_round4_template_quotes_the_amends():
    ht = H.load_type("work")
    out = H.render_round(ht, 4, {"huddle": "h", "member": "eva", "leader": "ada", "round": 4,
                                 "resolve": "RESOLVE-BLOCK", "brief": "", "earlier": "EARLIER"})
    assert "RESOLVE-BLOCK" in out and "accept" in out and "reject" in out and "{{" not in out
    assert '"round": 4' in out


def test_resolvers_are_the_lead_and_a_proposer_who_named_it():
    assert H.resolvers_of({**_p("A", with_=["echo"], answers={"echo": "amend"}),
                           "proposed_by": "eva"}) == ["eva"]
    named = {**_p("B", lead="hal", with_=["hal", "echo"]), "proposed_by": "eva",
             "answers": {"echo": "amend", "hal": "co-sign"}}
    assert H.resolvers_of(named) == ["hal", "eva"]
    # the named lead amended — only the proposer can resolve its amend
    assert H.resolvers_of({**named, "answers": {"echo": "co-sign", "hal": "amend"}}) == ["eva"]
    # no amend → no round 4
    assert H.resolvers_of({**named, "answers": {"echo": "co-sign", "hal": "co-sign"}}) == []


def test_gates_amend_accepted_files_and_amend_rejected_holds():
    filed, held = H.work_gates(
        [_p("A", with_=["echo"], answers={"echo": H.AMEND_ACCEPTED}),
         _p("B", with_=["echo"], answers={"echo": H.AMEND_REJECTED})], {"Q4 pipeline"}, [])
    assert [p["title"] for p in filed] == ["A"]
    assert held[0]["held"] == "amend rejected by lead (echo)"


# ── huddle prompting v2: the brief, both block shapes, overlaps, merges, asks ────
BRIEF = ("1. Close two Q4 funders — hard dates: 2026-10-20 — source: goals sheet\n"
         "2. IDM talk lands well — hard dates: 2026-10-15 — source: calendar\n"
         "3. Fleet reliability — hard dates: none — source: goals sheet\n"
         "Not now: new products")


def test_brief_items_and_section():
    assert H.brief_items(BRIEF) == {
        1: "Close two Q4 funders — hard dates: 2026-10-20 — source: goals sheet",
        2: "IDM talk lands well — hard dates: 2026-10-15 — source: calendar",
        3: "Fleet reliability — hard dates: none — source: goals sheet"}
    sec = H.brief_section(BRIEF, "Jonathan")
    assert sec.startswith("## Jonathan's top priorities (the brief — work toward these; "
                          "do not re-derive them)\n\n1. Close")
    assert sec.endswith("Not now: new products\n\n")
    assert H.brief_section("", "Jonathan") == ""


@pytest.mark.parametrize("v,n", [(2, 2), ("2", 2), ("#2", 2), ("2. IDM talk", 2),
                                 ("priority 3", 3), ("Q4 funder pipeline", None), (True, None),
                                 (None, None)])
def test_priority_number(v, n):
    assert H.priority_number(v) == n


def test_priority_label_names_the_brief_line():
    assert H.priority_label(2, BRIEF) == "priority 2: IDM talk lands well"
    assert H.priority_label("Q4 pipeline", BRIEF) == "Q4 pipeline"
    assert H.priority_label(9, BRIEF) == "9"


def _lever(**kw):
    return {"priority": 1, "move": "m", "kind": "new", "task": "", "blocked_by": "",
            "verified": True, **kw}


def _r1b(**kw):
    return {"huddle": "h", "round": 1, "member": "eva", "state": ["T41 mid-way"],
            "levers": [_lever()], "offers": [], "needs": [{"from": "jonathan", "ask": "a"}], **kw}


def test_validate_round1_accepts_the_brief_shape_and_the_old_shape():
    ht = H.load_type("work")
    assert H.validate_block(ht, 1, _r1b()) == []
    old = {"huddle": "h", "round": 1, "member": "eva", "worked_on": ["x"], "priorities": ["p"],
           "projects": [{"name": "Q4", "state": "on"}], "needs": ["a string need"]}
    assert H.validate_block(ht, 1, old) == []


def test_validate_round1_brief_shape_problems():
    ht = H.load_type("work")
    probs = H.validate_block(ht, 1, _r1b(levers=[_lever(), _lever(priority="1", kind="maybe"),
                                                  _lever(priority="two", kind="existing")]))
    assert "levers[1] repeats priority 1 (at most one per priority)" in probs
    assert "levers[1] kind must be one of new|unblock|existing" in probs
    assert "levers[2] priority must be a number" in probs
    assert "levers[2] missing task (required when kind is existing)" in probs
    assert "needs[0] missing ask" in H.validate_block(ht, 1, _r1b(needs=[{"from": "x"}]))


def _p2(**kw):
    return {"title": "t", "lead": "eva", "with": [], "priority": 1, "kind": "new",
            "project": {"name": "Q4", "new": False}, "why": "w (checked)",
            "plan": ["2026-10-09: start"], "effort": "S", "success_measure": "s",
            "cost_to_jonathan": {"kind": "none", "detail": ""}, "fails_if": "f", **kw}


def test_validate_round2_accepts_both_proposal_shapes():
    ht = H.load_type("work")
    blk = {"huddle": "h", "round": 2, "member": "eva", "proposals": [_p2()]}
    assert H.validate_block(ht, 2, blk) == []
    old = {k: v for k, v in _p2(priority="verbatim", confidence=0.5).items()
           if k not in ("kind", "cost_to_jonathan", "fails_if")}
    assert H.validate_block(ht, 2, {**blk, "proposals": [old]}) == []
    # round 3 revisions follow the same two shapes
    r3 = {"huddle": "h", "round": 3, "member": "eva", "answers": [], "proposals": [_p2()]}
    assert H.validate_block(ht, 3, r3) == []


def test_validate_round2_brief_shape_problems():
    ht = H.load_type("work")
    bad = _p2(kind="existing", cost_to_jonathan={"kind": "money"})
    probs = H.validate_block(ht, 2, {"huddle": "h", "round": 2, "member": "eva",
                                     "proposals": [bad]})
    assert "proposals[0] cost_to_jonathan.kind must be one of none|yes|decision|time" in probs
    assert "proposals[0] missing why_huddle (required when kind is existing)" in probs
    no_fail = {k: v for k, v in _p2().items() if k != "fails_if"}
    assert "proposals[0] missing fails_if" in H.validate_block(
        ht, 2, {"huddle": "h", "round": 2, "member": "eva", "proposals": [no_fail]})


def test_round_templates_pick_the_brief_or_the_old_variant():
    ht = H.load_type("work")
    with_brief = H.render_round(ht, 1, {**R1_CTX, "brief": H.brief_section(BRIEF, "Jonathan")})
    assert with_brief.index("## Jonathan's top priorities") < with_brief.index("READ-ONLY")
    assert '"levers"' in with_brief and '"worked_on"' not in with_brief
    assert '"from": "<teammate slug, or jonathan>"' in with_brief
    old = H.render_round(ht, 1, R1_CTX, variant="nobrief")
    assert '"worked_on"' in old and '"priorities"' in old and '"levers"' not in old
    assert "{{" not in with_brief + old
    for (n, _v), tmpl in ht.variants.items():
        assert '--session-id "huddle:{{huddle}}:{{member}}:r{{round}}"' in tmpl, n


def _o(title, lead, with_=(), priority=1, **kw):
    return {"title": title, "lead": lead, "with": list(with_), "priority": priority, **kw}


def test_overlaps_need_the_same_priority_and_a_shared_person():
    props = H.find_overlaps([_o("IDM demo", "ace", ["echo"]), _o("IDM live demo", "eva", ["ace"]),
                             _o("IDM story", "echo", priority="1"), _o("Funders", "ace", priority=2),
                             _o("Alarms", "hal")])
    by = {p["title"]: p for p in props}
    assert by["IDM demo"]["overlaps"] == ["IDM live demo", "IDM story"]   # "1" == 1
    assert by["IDM live demo"]["overlaps"] == ["IDM demo"]
    assert by["Funders"]["overlaps"] == [] and by["Alarms"]["overlaps"] == []
    assert by["IDM demo"]["unresolved_overlaps"] == by["IDM demo"]["overlaps"]
    assert len(H.unresolved_overlaps(props)) == 2


def test_merges_absorb_unions_people_and_drop_the_absorbed():
    props = [_o("IDM demo", "ace", ["echo"], ask_of_partners={"echo": "story slide"}),
             _o("IDM live demo", "eva", ["ace", "hal"], ask_of_partners={"hal": "infra"})]
    out, probs = H.apply_merges(props, {"IDM demo": {"absorbs": ["idm live demo"],
                                                     "why": "same demo"}})
    assert probs == []
    [kept] = out
    assert kept["title"] == "IDM demo" and kept["with"] == ["echo", "eva", "hal"]
    assert kept["ask_of_partners"]["echo"] == "story slide"
    assert kept["ask_of_partners"]["hal"] == "infra"
    assert "IDM live demo" in kept["ask_of_partners"]["eva"]
    assert kept["absorbed"] == ["IDM live demo"] and kept["merge_why"] == "same demo"
    assert kept["unresolved_overlaps"] == []
    # idempotent: applying the same merges to the merged list changes nothing (lenient)
    again, probs = H.apply_merges(out, {"IDM demo": {"absorbs": ["IDM live demo"]}},
                                  strict=False)
    assert probs == [] and again[0]["with"] == kept["with"]


def test_merges_distinct_from_resolves_both_directions():
    props = [_o("IDM demo", "ace"), _o("IDM slides", "ace")]
    out, _ = H.apply_merges(props, {"IDM slides": {"distinct_from": ["IDM demo"], "why": "x"}})
    assert all(p["overlaps"] and not p["unresolved_overlaps"] for p in out)
    assert H.unresolved_overlaps(out) == []


def test_merges_problems():
    props = [_o("A", "ace"), _o("B", "ace")]
    _, probs = H.apply_merges(props, {"Nope": {"absorbs": ["A"]}, "A": {"absorbs": ["A", "Z"]},
                                      "B": {"distinct_from": ["Q"]}})
    assert "merges: no proposal titled 'Nope'" in probs
    assert "merges: 'A' cannot absorb 'A' (itself)" in probs
    assert "merges: 'A' cannot absorb 'Z' (no such proposal)" in probs
    assert "merges: 'B' distinct_from 'Q': no such proposal" in probs
    _, lenient = H.apply_merges(props, {"Nope": {"absorbs": ["A"]}, "A": {"absorbs": ["Z"]}},
                                strict=False)
    assert lenient == []


def test_asks_of_principal_dedupes_and_keeps_everyone_asking():
    r1 = {"ace": {"needs": [{"from": "Jonathan", "ask": "Rank the IDM demo options"},
                            {"from": "eva", "ask": "intro"}]},
          "eva": {"needs": [{"from": "jonathan", "ask": "rank the IDM demo options!"},
                            {"from": "jonathan", "ask": "Approve T22 budget"}]},
          "echo": {"needs": ["an old-shape string need"]}}
    props = [_o("PRIDE", "hal", cost_to_jonathan={"kind": "decision",
                                                  "detail": "Pick the T22 vendor"}),
             _o("Alarms", "hal", cost_to_jonathan={"kind": "none", "detail": ""}),
             _o("Demo", "ace", cost_to_jonathan={"kind": "time", "detail": "20 minutes Tue"})]
    asks = H.asks_of_principal(r1, props, "Jonathan")
    assert asks == [
        {"ask": "Rank the IDM demo options", "who": ["ace", "eva"], "for": "", "kind": "need"},
        {"ask": "Approve T22 budget", "who": ["eva", "hal"], "for": "PRIDE", "kind": "decision"},
        {"ask": "20 minutes Tue", "who": ["ace"], "for": "Demo", "kind": "time"}]


def test_gates_with_a_brief_take_brief_numbers():
    ok = {**_p("A"), "priority": 2, "critique_answered": True}
    off = {**_p("B"), "priority": 7, "critique_answered": True}
    text = {**_p("C"), "priority": "Q4 funder pipeline", "critique_answered": True}
    filed, held = H.work_gates([ok, off, text], set(), [], brief_numbers={1, 2, 3})
    assert [p["title"] for p in filed] == ["A"]
    assert {h["title"]: h["held"] for h in held} == {
        "B": "priority 7 is not in the brief",
        "C": "priority is not a brief number: 'Q4 funder pipeline'"}
    # no brief: the round-1 rule, unchanged
    filed, held = H.work_gates([text], {"Q4 funder pipeline (sheet)"}, [])
    assert [p["title"] for p in filed] == ["C"]
