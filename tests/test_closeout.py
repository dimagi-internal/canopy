"""closeout: the verdict an unattended incident turn may end on is computed, not typed.

Fixtures are the audit cases (Ada, 2026-10-29) over Hal's CloudWatch alarm turns
2026-08-26..10-29: 36 ended "✅ Session complete — safe to close.", ~12 had closed the incident.
Each case below is a quoted turn note reduced to the facts it states; every one of them was
"complete" and none of them should have been.
"""
import json

import pytest
from click.testing import CliRunner

from orchestrator import closeout as co
from orchestrator.closeout_cli import closeout_group

STEP_OK = [{"name": "hal-turn-close", "exit_code": 0}]
EVIDENCE = "https://github.com/dimagi-internal/connect-labs/issues/2768"


def rec(**kw):
    base = {"subject": "labs-jj-alb-latency-high",
            "incident": {"state": "OK", "checked_via": "describe_alarm"},
            "cause": {"status": "confirmed", "evidence": EVIDENCE},
            "remedy": "shipped_verified_live", "open_items": [], "closeout_steps": STEP_OK}
    base.update(kw)
    return base


def hal_task(what="re-check", task="T31", when="2026-10-30T10:00Z"):
    owner = {"agent": "hal", "task": task}
    if when:
        owner["recheck_at"] = when
    return {"what": what, "owner": owner}


# ---- the 10:12Z 2026-10-29 cloud turn (a72a5d39) -------------------------------------------
# "the 09:56Z episode never cleared. The alarm re-fired at 10:01, 10:06 and 10:11Z ... It still
#  asks someone with AWS SSO to pull per-path response times ... the cause is still
#  undiagnosed. The slow workflow-run page (#2514) is the candidate, unconfirmed ... I only
#  looked at the end of its output, so I didn't confirm the workspace-refresh step succeeded."
TURN_1012Z = {
    "subject": "labs-jj-alb-latency-high",
    "incident": {"state": "ALARM", "checked_via": "alarm_mail:1a187245aeee7a24 (10:11Z ALARM)"},
    "cause": {"status": "likely",
              "evidence": "https://github.com/dimagi-internal/connect-labs/issues/2514"},
    "remedy": "none",
    "open_items": [{"what": "pull per-path response times + request counts for 08:40-10:10Z",
                    "owner": "someone with AWS SSO"}],
    "closeout_steps": [{"name": "hal-turn-close", "exit_code": None}],
}


def test_the_1012z_turn_is_not_closed():
    v = co.compute(TURN_1012Z)
    assert v.verdict == co.INCOMPLETE
    assert any("someone with AWS SSO" in p for p in v.problems)
    assert any("hal-turn-close has no exit code" in p for p in v.problems)
    assert "safe to close" not in v.status_line
    assert v.status_line.endswith("[closeout: INCOMPLETE]")


def test_the_1012z_turn_with_owners_recorded_is_needs_you():
    fixed = json.loads(json.dumps(TURN_1012Z))
    fixed["open_items"][0]["owner"] = {"person": "@jjackson", "decision": "run `aws sso login --profile labs` so Hal can pull per-path latency"}
    fixed["closeout_steps"][0]["exit_code"] = 0
    v = co.compute(fixed)
    assert v.verdict == co.NEEDS_YOU
    assert v.status_line == ("⏸ Session paused — waiting on you for: @jjackson: run `aws sso login "
                             "--profile labs` so Hal can pull per-path latency. [closeout: NEEDS_YOU]")


# ---- 2026-10-14 web-cpu: "CPU attribution stays unconfirmed" -------------------------------
def test_cause_unconfirmed_with_nobody_owning_the_next_step_is_incomplete():
    v = co.compute(rec(incident={"state": "OK", "checked_via": "alarm_mail:x (OK)"},
                       cause={"status": "unknown"}, remedy="none"))
    assert v.verdict == co.INCOMPLETE
    assert "nothing open is owned" in v.problems[0]


# ---- "merged it ... but it isn't live yet ... I didn't wait for the deploy" ------------------
def test_merged_not_live_is_not_closed():
    assert co.compute(rec(remedy="merged_not_live")).verdict == co.INCOMPLETE


def test_merged_not_live_with_a_scheduled_recheck_is_monitoring():
    v = co.compute(rec(remedy="merged_not_live",
                       open_items=[hal_task("connect-labs#2404 is live and the alarm stays OK")]))
    assert v.verdict == co.MONITORING
    assert "hal re-checks connect-labs#2404 is live" in v.status_line
    assert "safe to close" not in v.status_line


# ---- "Decisions for you: Run aws sso login…" inside a "complete" ----------------------------
def test_a_decision_for_a_person_is_needs_you_even_when_the_incident_is_closed():
    v = co.compute(rec(open_items=[{"what": "post the write-up",
                                    "owner": {"person": "Jonathan", "decision": "whether to post this"}}]))
    assert v.verdict == co.NEEDS_YOU
    assert v.asks == ["Jonathan: whether to post this"]


def test_a_person_owner_without_the_decision_is_rejected():
    v = co.compute(rec(open_items=[{"what": "x", "owner": {"person": "@jjackson"}}]))
    assert v.verdict == co.INCOMPLETE and "names no decision" in v.problems[0]


@pytest.mark.parametrize("who", ["someone", "someone with SSO", "the team", "on-call", "TBD", "anyone", ""])
def test_a_role_or_someone_is_not_an_owner(who):
    v = co.compute(rec(open_items=[{"what": "x", "owner": {"person": who, "decision": "d"}}]))
    assert v.verdict == co.INCOMPLETE


# ---- "Next turn picks up the falsifier … I'll profile the save endpoint" --------------------
def test_promised_future_work_with_no_board_task_is_rejected():
    v = co.compute(rec(remedy="none", cause={"status": "likely"},
                       open_items=[{"what": "profile the save endpoint", "owner": {"agent": "hal"}}]))
    assert v.verdict == co.INCOMPLETE and "no board task" in v.problems[0]


def test_promised_future_work_on_the_board_without_a_recheck_is_incomplete():
    v = co.compute(rec(remedy="none", cause={"status": "likely"},
                       open_items=[hal_task("profile the save endpoint", when=None)]))
    assert v.verdict == co.INCOMPLETE and "no re-check time" in v.problems[0]


def test_promised_future_work_on_the_board_with_a_recheck_is_monitoring():
    v = co.compute(rec(remedy="none", cause={"status": "likely"},
                       open_items=[hal_task("profile the save endpoint")]))
    assert v.verdict == co.MONITORING


# ---- user-facing alarm threads left unread "for some future dispatch" ----------------------
def test_left_for_a_future_dispatch_is_not_owned():
    v = co.compute(rec(open_items=[{"what": "the 3 unread alarm threads",
                                    "owner": "a future dispatch"}]))
    assert v.verdict == co.INCOMPLETE


def test_an_actual_dispatch_is_owned():
    v = co.compute(rec(remedy="proposed", open_items=[
        {"what": "fix the fan-out", "owner": {"dispatched": "turn 7f3c2a10-aaaa-bbbb-cccc-000000000000"}}]))
    assert v.verdict == co.MONITORING


# ---- "filed an issue" treated as done (connect-labs #2575, #2768, #2769, #1906 unassigned) --
def test_a_filed_issue_nobody_owns_is_rejected():
    v = co.compute(rec(remedy="proposed", open_items=[
        {"what": "connect-labs#2769", "owner": None},
        {"what": "connect-labs#1906", "owner": {"agent": "hal"}}]))
    assert v.verdict == co.INCOMPLETE and len(v.problems) == 2
    assert "filing a GitHub issue is not owning it" in v.problems[1]


# ---- "only looked at the tail of bin/hal-turn-close" (4 turns) -----------------------------
def test_a_closeout_step_read_from_its_tail_is_unverified():
    v = co.compute(rec(closeout_steps=[{"name": "hal-turn-close"}]))
    assert v.verdict == co.INCOMPLETE and "no exit code" in v.problems[0]


def test_a_failed_closeout_step_is_incomplete():
    v = co.compute(rec(closeout_steps=[{"name": "agent-publish", "exit_code": 1}]))
    assert v.verdict == co.INCOMPLETE and "exited 1" in v.problems[0]


def test_no_closeout_steps_is_incomplete():
    assert co.compute(rec(closeout_steps=[])).verdict == co.INCOMPLETE


# ---- the incident state is CHECKED, not assumed --------------------------------------------
def test_an_ok_that_was_not_checked_is_unknown():
    v = co.compute(rec(incident={"state": "OK"}))
    assert v.verdict == co.INCOMPLETE and "was not checked" in v.problems[0]


def test_a_confirmed_cause_needs_evidence():
    v = co.compute(rec(cause={"status": "confirmed"}))
    assert v.verdict == co.INCOMPLETE and "no evidence" in v.problems[0]


# ---- what CLOSED is ------------------------------------------------------------------------
def test_shipped_verified_live_and_ok_is_closed():
    v = co.compute(rec())
    assert v.verdict == co.CLOSED
    assert v.status_line == "✅ Session complete — safe to close. [closeout: CLOSED]"


def test_a_non_event_needs_no_remedy_and_no_confirmed_cause():
    v = co.compute(rec(cause={"status": "unknown"}, remedy="none_needed"))
    assert v.verdict == co.CLOSED


def test_a_likely_cause_with_a_live_fix_is_not_closed():
    assert co.compute(rec(cause={"status": "likely"})).verdict == co.INCOMPLETE


def test_still_firing_is_never_closed():
    v = co.compute(rec(incident={"state": "ALARM", "checked_via": "describe_alarm"}))
    assert v.verdict == co.INCOMPLETE and "the incident is ALARM" in v.problems[0]


def test_closed_may_carry_owned_agent_followups():
    assert co.compute(rec(open_items=[hal_task("tidy the alarm threshold")])).verdict == co.CLOSED


@pytest.mark.parametrize("bad", [None, [], "x", {"incident": "OK"}])
def test_garbage_is_incomplete_not_a_crash(bad):
    assert co.compute(bad).verdict == co.INCOMPLETE


def test_the_token_round_trips():
    for v in co.VERDICTS:
        assert co.TOKEN_RE.search(co.TOKEN.format(verdict=v)).group(1) == v


# ---- the record + CLI ----------------------------------------------------------------------
def test_write_and_read(tmp_path):
    doc = co.write(TURN_1012Z, "sess-1", turn_id="a72a5d39", root=str(tmp_path), now=5.0)
    assert doc["verdict"] == co.INCOMPLETE and doc["recorded_at"] == 5.0
    assert co.read("sess-1", root=str(tmp_path))["status_line"] == doc["status_line"]
    assert oct((tmp_path / "sess-1.json").stat().st_mode & 0o777) == "0o600"


def test_cli_record_prints_the_status_line_and_exits_3_on_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(co, "ROOT", str(tmp_path))
    f = tmp_path / "r.json"
    f.write_text(json.dumps(TURN_1012Z))
    out = CliRunner().invoke(closeout_group, ["record", "--file", str(f), "--session-id", "s1",
                                              "--turn-id", "t1"])
    assert out.exit_code == 3
    assert out.output.strip().splitlines()[-1].endswith("[closeout: INCOMPLETE]")
    assert co.read("s1", root=str(tmp_path))["turn_id"] == "t1"


def test_cli_step_flag_supplies_the_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(co, "ROOT", str(tmp_path))
    f = tmp_path / "r.json"
    f.write_text(json.dumps(rec(closeout_steps=[])))
    out = CliRunner().invoke(closeout_group, ["record", "--file", str(f), "--session-id", "s2",
                                              "--step", "hal-turn-close=0"])
    assert out.exit_code == 0, out.output
    assert out.output.strip() == "✅ Session complete — safe to close. [closeout: CLOSED]"


def test_cli_needs_a_session_id(tmp_path, monkeypatch):
    for e in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(e, raising=False)
    f = tmp_path / "r.json"
    f.write_text(json.dumps(rec()))
    out = CliRunner().invoke(closeout_group, ["record", "--file", str(f)])
    assert out.exit_code != 0 and "no session id" in out.output


def test_cli_dry_run_stores_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(co, "ROOT", str(tmp_path))
    f = tmp_path / "r.json"
    f.write_text(json.dumps(rec()))
    out = CliRunner().invoke(closeout_group, ["record", "--file", str(f), "--dry-run", "--json"])
    assert out.exit_code == 0 and json.loads(out.output)["verdict"] == co.CLOSED
    assert not list(tmp_path.glob("*.json")) or [p.name for p in tmp_path.glob("*.json")] == ["r.json"]
