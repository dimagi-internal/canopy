"""The fleet raw-labs-log rail (agent-core/gating-baseline.json `always`).

WHY THIS FILE EXISTS. labs-read (labs-jj-observe-readonly) denies raw app log groups
(/ecs/labs-jj-*) by design and reads their scrubbed copies under /labs-jj/observe/*.
An agent that tries the raw group gets a bare AccessDenied and concludes it cannot read
logs at all, while /labs-jj/observe/alb-requests holds the per-route answer. The rail
turns that dead end into the scrubbed group, for every agent.

The NEGATIVE half matters most: the scrubbed reads the message recommends, log-group
metadata, and a grep for the raw path must all pass, or the rail blocks its own remedy.

Run: uv run pytest tests/test_gating_baseline_labs_raw_logs.py
"""
import json
import re
from pathlib import Path

import pytest

BASELINE = Path(__file__).parent.parent / "plugins" / "canopy" / "agent-core" / "gating-baseline.json"
ALWAYS = json.loads(BASELINE.read_text())["always"]
RAILS = [r for r in ALWAYS if r.get("tool") == "Bash" and "/ecs/labs-jj-" in r.get("pattern", "")]

# Mirrors agent-core/gating_guard.py::_STATEMENT_SPLIT — this rail is per_statement.
_SPLIT = re.compile(r"[\n;]|&&|\|\||[|&]")


def blocks(cmd: str) -> bool:
    stmts = [s.strip() for s in _SPLIT.split(cmd) if s.strip()]
    return any(re.search(r["pattern"], s) for r in RAILS for s in stmts)


def test_the_rail_exists_and_is_agent_only():
    assert len(RAILS) == 1, "exactly one raw-labs-log rail in `always`"
    assert RAILS[0].get("agent_only") is True, "a person's `labs` SSO profile may read raw logs"


@pytest.mark.parametrize("cmd", [
    "aws logs tail /ecs/labs-jj-canopy-web --since 10m --profile labs-read",
    "aws logs filter-log-events --log-group-name /ecs/labs-jj-web --limit 1 --profile labs-observe",
    "aws logs get-log-events --log-group-name /ecs/labs-jj-canopy-web --log-stream-name x --profile labs-read",
    "aws logs start-query --log-group-name /ecs/labs-jj-canopy-web --start-time 1 --end-time 2 --query-string 'stats count(*)'",
    "aws logs start-live-tail --log-group-identifiers arn:aws:logs:us-east-1:1:log-group:/ecs/labs-jj-web",
    'P="--profile labs-read"; aws logs tail /ecs/labs-jj-canopy-web --since 10m $P | tail -40',
    "AWS_PROFILE=labs-read aws logs tail /ecs/labs-jj-canopy-web",
])
def test_raw_labs_log_reads_are_blocked(cmd):
    assert blocks(cmd), cmd


@pytest.mark.parametrize("cmd", [
    # the remedy
    "aws logs tail /labs-jj/observe/alb-requests --since 15m --profile labs-read",
    "aws logs filter-log-events --log-group-name /labs-jj/observe/canopy-web-errors --profile labs-read",
    "aws logs start-query --log-group-name /labs-jj/observe/alb-requests --start-time 1 --end-time 2 --query-string 'x'",
    # metadata on the raw group is allowed by the IAM policy too
    "aws logs describe-log-streams --log-group-name /ecs/labs-jj-canopy-web --profile labs-read",
    "aws logs describe-subscription-filters --log-group-name /ecs/labs-jj-canopy-web --profile labs-read",
    "aws logs describe-log-groups --log-group-name-prefix /ecs/labs-jj- --profile labs-read",
    # searching for or quoting the raw path
    'grep -rn "/ecs/labs-jj-canopy-web" infra/',
    'echo "aws logs tail /ecs/labs-jj-canopy-web is denied"',
    # usage
    "aws logs tail /ecs/labs-jj-canopy-web --help",
])
def test_scrubbed_reads_metadata_and_searches_pass(cmd):
    assert not blocks(cmd), cmd


def test_the_message_names_the_scrubbed_groups():
    """Rails, not gates: a block that doesn't say where to read instead just stalls the turn."""
    (r,) = RAILS
    for group in ("/labs-jj/observe/alb-requests", "/labs-jj/observe/request-telemetry",
                  "/labs-jj/observe/<app>-errors", "/labs-jj/observe/ecs-events"):
        assert group in r["message"], group
    assert "start-query" in r["message"]


def test_the_credentials_rail_no_longer_says_logs_are_unreadable():
    (cred,) = [r for r in ALWAYS if "canopy\\s+aws\\s+credentials" in r.get("pattern", "")]
    assert "Log events, S3 objects" not in cred["message"]
    assert "/labs-jj/observe/" in cred["message"]


def _guard(monkeypatch):
    import importlib.machinery
    import importlib.util

    plugin = Path(__file__).parent.parent / "plugins" / "canopy"
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(plugin))
    loader = importlib.machinery.SourceFileLoader("gating_guard", str(plugin / "agent-core" / "gating_guard.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    guard = importlib.util.module_from_spec(spec)
    loader.exec_module(guard)
    return guard


def test_the_real_guard_blocks_it_for_an_agent(monkeypatch, tmp_path):
    guard = _guard(monkeypatch)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gating.json").write_text(
        json.dumps({"slug": "ada", "channels": [], "deny": [], "approve": []}))
    call = lambda cmd: guard.run(str(tmp_path), {"tool_name": "Bash", "tool_input": {"command": cmd}})

    code, _, err = call("aws logs tail /ecs/labs-jj-canopy-web --since 10m --profile labs-read")
    assert code == 2
    assert "/labs-jj/observe/alb-requests" in err
    assert call("aws logs tail /labs-jj/observe/alb-requests --since 10m --profile labs-read")[0] == 0
