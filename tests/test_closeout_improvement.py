"""closeout improvement: every opted-in incident turn ends with a MERGED PR that fixes the cause
or makes the next fire easier to diagnose.

Jonathan, 2026-10-29: "anytime an alarm is fired, it should either result in a solution or an
improvement in how to figure out why we can't find a solution". Ada's review of 24 labs alarm
incidents since 2026-08-26: ~10 ended "cause unknown, needs someone with AWS SSO", ~8 "improved"
only Hal's own skill prose (hal#144, #255, #275, #279, #297), and the diagnosability asks sat as
unowned connect-labs issues (#1575, #1578). Each rejection below is one of those endings.
"""
import json
import shutil
import subprocess

import pytest
from click.testing import CliRunner

from orchestrator import closeout as co
from orchestrator.closeout_cli import closeout_group

HAL_POLICY = {"required": True,
              "prose_only": [{"repo": "dimagi-internal/hal", "paths": ["skills/**", "**/*.md"]}]}
PR_2404 = "https://github.com/dimagi-internal/connect-labs/pull/2404"
GH_2404 = {"state": "MERGED", "mergedAt": "2026-10-09T15:03:18Z", "url": PR_2404,
           "files": [{"path": "infra/labs-tenant-monitoring.yml"}]}


def gh(doc=None, code=0, err=""):
    calls = []

    def run(args, timeout=60):
        calls.append(args)
        return code, json.dumps(doc) if doc is not None else "", err
    run.calls = calls
    return run


def rec(improvement=None, policy=HAL_POLICY, **kw):
    base = {"subject": "labs-jj-alb-target-5xx-high",
            "incident": {"state": "OK", "checked_via": "describe_alarm"},
            "cause": {"status": "confirmed", "evidence": PR_2404},
            "remedy": "shipped_verified_live", "open_items": [],
            "closeout_steps": [{"name": "hal-turn-close", "exit_code": 0}]}
    if policy is not None:
        base["improvement_policy"] = policy
    if improvement is not None:
        base["improvement"] = improvement
    base.update(kw)
    return base


def verdict(r, run=None):
    return co.compute(co.check_improvement(r, run=run or gh(GH_2404)))


# ---- the two real records ------------------------------------------------------------------

# canopy-web turn a72a5d39-3ae9-41ec-8aa4-00776b868b28 (cloud-ec2-live, 2026-10-29 10:11-10:12Z),
# its result_note reduced to facts: "I added one comment to it and opened nothing new ... It still
# asks someone with AWS SSO to pull per-path response times ... the cause is still undiagnosed.
# The slow workflow-run page (#2514) is the candidate, unconfirmed ... I only looked at the end
# of its output ... ✅ Session complete — safe to close."
TURN_A72A5D39 = {
    "subject": "labs-jj-alb-latency-high",
    "incident": {"state": "ALARM", "checked_via": "alarm_mail:1a187245aeee7a24 (10:11Z ALARM)"},
    "cause": {"status": "likely",
              "evidence": "https://github.com/dimagi-internal/connect-labs/issues/2514"},
    "remedy": "none",
    "open_items": [{"what": "pull per-path response times + request counts for 08:40-10:10Z",
                    "owner": "someone with AWS SSO"}],
    "closeout_steps": [{"name": "hal-turn-close", "exit_code": None}],
    "improvement_policy": HAL_POLICY,
}


def test_replay_a72a5d39_is_incomplete_and_says_no_improvement():
    v = verdict(TURN_A72A5D39, run=gh(code=99))
    assert v.verdict == co.INCOMPLETE
    assert any(p.startswith("no improvement recorded") for p in v.problems)
    assert v.status_line.endswith("[closeout: INCOMPLETE]")


def test_replay_a72a5d39_even_with_owners_fixed_still_lacks_the_improvement():
    r = json.loads(json.dumps(TURN_A72A5D39))
    r["open_items"][0]["owner"] = {"person": "@jjackson", "decision": "aws sso login"}
    r["closeout_steps"][0]["exit_code"] = 0
    v = verdict(r, run=gh(code=99))
    assert v.verdict == co.INCOMPLETE and v.problems[0].startswith("no improvement recorded")


def test_replay_a72a5d39_its_issue_comment_is_not_an_improvement():
    r = dict(TURN_A72A5D39, improvement={
        "kind": "diagnosability", "signal": "runbook_query",
        "pr": "https://github.com/dimagi-internal/connect-labs/issues/2768"})
    run = gh(code=99)
    v = verdict(r, run=run)
    assert any("is a filed issue, not a merged PR" in p for p in v.problems)
    assert run.calls == []  # never asked gh to treat an issue as a PR


def test_replay_connect_labs_2404_an_alarm_description_change_passes():
    run = gh(GH_2404)
    checked = co.check_improvement(rec({"kind": "diagnosability", "signal": "alarm_description",
                                         "pr": PR_2404}), run=run)
    v = co.compute(checked)
    assert v.verdict == co.CLOSED, v.problems
    assert run.calls[0][:5] == ["gh", "pr", "view", "2404", "-R"]
    assert checked["closeout_steps"][-1] == {
        "name": "gh pr view dimagi-internal/connect-labs#2404 (improvement merged?)", "exit_code": 0}


@pytest.mark.skipif(not shutil.which("gh"), reason="needs gh")
def test_replay_connect_labs_2404_live_through_the_cli(tmp_path, monkeypatch):
    """The real gh, the real PR: merged 2026-10-09, one file, infra/labs-tenant-monitoring.yml."""
    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        pytest.skip("gh not signed in")
    monkeypatch.setattr(co, "ROOT", str(tmp_path))
    f, p = tmp_path / "r.json", tmp_path / "policy.json"
    r = rec({"kind": "diagnosability", "signal": "alarm_description", "pr": PR_2404}, policy=None)
    f.write_text(json.dumps(r))
    p.write_text(json.dumps({"improvement": HAL_POLICY}))
    out = CliRunner().invoke(closeout_group, ["record", "--file", str(f), "--improvement-policy",
                                              str(p), "--dry-run", "--json"])
    doc = json.loads(out.output)
    if doc["record"]["improvement"]["checked"].get("error"):
        pytest.skip("gh cannot read dimagi-internal/connect-labs here")
    assert out.exit_code == 0 and doc["verdict"] == co.CLOSED, doc["problems"]


# ---- each rejection ------------------------------------------------------------------------

def test_missing_improvement_is_incomplete():
    v = verdict(rec())
    assert v.verdict == co.INCOMPLETE and "ship the missing signal" in v.problems[0]


def test_an_unknown_kind_is_rejected():
    v = verdict(rec({"kind": "skill_edit", "pr": PR_2404}))
    assert v.verdict == co.INCOMPLETE and "improvement.kind 'skill_edit'" in v.problems[0]


def test_diagnosability_must_name_its_signal():
    v = verdict(rec({"kind": "diagnosability", "pr": PR_2404}))
    assert v.verdict == co.INCOMPLETE and "names its signal" in v.problems[0]


def test_no_pr_is_rejected():
    assert "names no PR" in verdict(rec({"kind": "cause_fix"})).problems[0]


def test_a_filed_issue_is_rejected():
    v = verdict(rec({"kind": "cause_fix",
                     "pr": "https://github.com/dimagi-internal/connect-labs/issues/1575"}))
    assert v.verdict == co.INCOMPLETE and "filed issue" in v.problems[0]


def test_a_short_issue_ref_fails_the_gh_check_and_its_exit_code_is_a_step():
    run = gh(code=1, err="GraphQL: Could not resolve to a PullRequest with the number of 1578.")
    r = co.check_improvement(rec({"kind": "cause_fix", "pr": "dimagi-internal/connect-labs#1578"}),
                             run=run)
    v = co.compute(r)
    assert v.verdict == co.INCOMPLETE
    assert any("could not be verified as a PR: GraphQL" in p for p in v.problems)
    assert any("improvement merged?) exited 1" in p for p in v.problems)


def test_an_open_pr_is_not_merged():
    v = verdict(rec({"kind": "cause_fix", "pr": PR_2404}), run=gh(dict(GH_2404, state="OPEN")))
    assert v.verdict == co.INCOMPLETE and "is OPEN, not MERGED" in v.problems[0]


def test_a_closed_unmerged_pr_is_rejected():
    v = verdict(rec({"kind": "cause_fix", "pr": PR_2404}), run=gh(dict(GH_2404, state="CLOSED")))
    assert "is CLOSED, not MERGED" in v.problems[0]


@pytest.mark.parametrize("files", [["skills/labs-perf/SKILL.md"],                      # hal#297
                                   ["skills/turn/SKILL.md", "CLAUDE.md"],
                                   ["skills/labs-perf/scripts/notes.txt", "docs/alarms.md"]])
def test_a_pr_that_only_edits_the_agents_own_prose_does_not_count(files):
    pr = "https://github.com/dimagi-internal/hal/pull/297"
    doc = {"state": "MERGED", "url": pr, "files": [{"path": f} for f in files]}
    v = verdict(rec({"kind": "diagnosability", "signal": "runbook_query", "pr": pr}), run=gh(doc))
    assert v.verdict == co.INCOMPLETE and "own skill/prose files" in v.problems[0]


def test_an_agent_repo_pr_that_ships_code_counts():
    pr = "https://github.com/dimagi-internal/hal/pull/320"
    doc = {"state": "MERGED", "url": pr,
           "files": [{"path": "skills/labs-perf/SKILL.md"}, {"path": "bin/hal-labs-perf-scope"}]}
    assert verdict(rec({"kind": "diagnosability", "signal": "runbook_query", "pr": pr}),
                   run=gh(doc)).verdict == co.CLOSED


def test_markdown_in_another_repo_is_not_the_agents_prose():
    pr = "https://github.com/dimagi-internal/connect-labs/pull/9"
    doc = {"state": "MERGED", "url": pr, "files": [{"path": "infra/RUNBOOK.md"}]}
    assert verdict(rec({"kind": "diagnosability", "signal": "runbook_query", "pr": pr}),
                   run=gh(doc)).verdict == co.CLOSED


def test_a_typed_check_is_discarded_and_reverified():
    r = rec({"kind": "cause_fix", "pr": PR_2404, "checked": {"state": "MERGED", "files": ["x.py"]}})
    v = verdict(r, run=gh(dict(GH_2404, state="OPEN")))
    assert "is OPEN" in v.problems[0]


def test_an_unchecked_pr_is_rejected():
    v = co.compute(rec({"kind": "cause_fix", "pr": PR_2404}))
    assert v.verdict == co.INCOMPLETE and "was not checked" in v.problems[0]


# ---- the one exception: already diagnosable in one step -----------------------------------
Q = ("aws logs start-query --profile labs-observe --log-group-name /ecs/labs-jj-web "
     "--query-string 'fields path, duration | sort duration desc | limit 20'")
OUT = "https://github.com/dimagi-internal/connect-labs/issues/2768#issuecomment-1"


def test_already_diagnosable_with_the_query_that_found_the_cause_passes():
    r = rec({"kind": "already_diagnosable", "query": Q, "output": OUT},
            cause={"status": "confirmed", "evidence": OUT, "identified_by": Q})
    run = gh(code=99)
    assert verdict(r, run=run).verdict == co.CLOSED and run.calls == []


def test_already_diagnosable_when_the_cause_was_not_found_by_that_query_is_rejected():
    r = rec({"kind": "already_diagnosable", "query": Q, "output": OUT},
            cause={"status": "confirmed", "evidence": OUT, "identified_by": "read the dashboard"})
    v = verdict(r)
    assert v.verdict == co.INCOMPLETE and "identified_by = that same query" in v.problems[0]


def test_already_diagnosable_with_an_unknown_cause_is_rejected():
    r = rec({"kind": "already_diagnosable", "query": Q, "output": OUT},
            cause={"status": "unknown"}, remedy="none_needed")
    assert verdict(r).verdict == co.INCOMPLETE


@pytest.mark.parametrize("q", ["", "aws logs tail x && aws logs tail y", "a\nb", "a; b"])
def test_already_diagnosable_needs_a_single_command(q):
    r = rec({"kind": "already_diagnosable", "query": q, "output": OUT},
            cause={"status": "confirmed", "evidence": OUT, "identified_by": q})
    assert any("ONE query/command" in p for p in verdict(r).problems)


def test_already_diagnosable_needs_an_output_link():
    r = rec({"kind": "already_diagnosable", "query": Q, "output": "it showed /workflow/run"},
            cause={"status": "confirmed", "evidence": OUT, "identified_by": Q})
    assert any("link to that query's output" in p for p in verdict(r).problems)


# ---- opt-in: other agents' turns are unchanged ---------------------------------------------

def test_without_the_policy_nothing_is_required():
    assert co.compute(rec(policy=None)).verdict == co.CLOSED


def test_without_the_policy_and_no_improvement_check_improvement_is_a_no_op():
    run = gh(code=99)
    r = rec(policy=None)
    assert co.check_improvement(r, run=run) == r and run.calls == []


def test_the_cli_policy_flag_opts_in(tmp_path, monkeypatch):
    monkeypatch.setattr(co, "ROOT", str(tmp_path))
    f, p = tmp_path / "r.json", tmp_path / "policy.json"
    f.write_text(json.dumps(rec(policy=None)))
    p.write_text(json.dumps({"improvement": HAL_POLICY}))
    out = CliRunner().invoke(closeout_group, ["record", "--file", str(f), "--session-id", "s1",
                                              "--improvement-policy", str(p)])
    assert out.exit_code == 3 and out.output.strip().splitlines()[-1].endswith("[closeout: INCOMPLETE]")
    assert co.read("s1", root=str(tmp_path))["record"]["improvement_policy"]["required"] is True


@pytest.mark.parametrize("pat,path,hit", [("skills/**", "skills/a/b/SKILL.md", True),
                                          ("**/*.md", "README.md", True),
                                          ("**/*.md", "docs/x/y.md", True),
                                          ("*.md", "docs/y.md", False),
                                          ("skills/**", "bin/hal-turn-close", False)])
def test_globs(pat, path, hit):
    assert bool(co._glob_re(pat).match(path)) is hit
