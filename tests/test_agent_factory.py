"""Integration tests for the agent factory's generated hook + the REAL canopy plugin engine.

The factory itself (create_agent, AgentSpec, normalize_slug, templates, gating_config) moved
to the standalone `canopy_agent_factory` package (see EXTRACTION-BRIEF.md) — its own
structural/token/rollback tests moved with it, to packages/canopy_agent_factory/tests/.

What stays here: tests that spawn the STAMPED hooks/gating_guard.py (a thin loader) against
the real plugins/canopy/agent-core engine — the baseline deny rails, the agent-core docs, the
MCP Drive rails, per_statement, and the shared-engine delegation. Those fixtures live in this
repo's plugin tree, not in the standalone package, so this is where the integration is
provable.
"""
import json
import subprocess
import sys
from pathlib import Path

from canopy_agent_factory import AgentSpec, create_agent


def _spec():
    return AgentSpec(
        slug="echo",
        display_name="Echo",
        mandate="be the marketing agent.",
        mailbox="echo@dimagi-ai.com",
        stakeholders="the Connect team",
    )


def test_gating_baseline_ships_in_plugin():
    """The fleet-baseline rails live once, in the versioned plugin — a rail fix propagates
    via /canopy:update, never via per-agent backports."""
    base = json.loads((Path(__file__).resolve().parents[1]
                       / "plugins" / "canopy" / "agent-core" / "gating-baseline.json").read_text())
    email = base["channels"]["email"]
    pats = [r["pattern"] for r in email]
    assert any("gog" in p and "gmail" in p for p in pats), "raw gog send rail missing"
    assert any("--account" in p for p in pats), "identity-bleed rail missing"
    for r in email:
        assert "{slug}" in r["message"], "baseline messages are slug-templated at call time"
        assert "{{" not in json.dumps(r), "stamp-time tokens do not belong in the runtime baseline"


def test_gating_hook_blocks_deny_asks_approve_allows_reads(tmp_path):
    """End-to-end: the generated hook enforces deny (exit 2) / approve (ask) / allow.

    The deny rail under test is the TEMPLATED one (raw gog send); approve rules ship
    empty by default (rails, not gates) so one is injected to prove the engine still
    honors them for agents that opt in.
    """
    create_agent(_spec(), tmp_path / "echo")
    root = tmp_path / "echo"
    hook = root / "hooks" / "gating_guard.py"

    gating = root / "config" / "gating.json"
    cfg = json.loads(gating.read_text())
    cfg["approve"] = [{"tool": "Edit", "message": "Echo edits only with approval."}]
    gating.write_text(json.dumps(cfg))

    import os as _os
    env = {**_os.environ,
           "CANOPY_PLUGIN_DIR": str(Path(__file__).resolve().parents[1] / "plugins" / "canopy")}

    def run(payload):
        return subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps(payload), capture_output=True, text=True, env=env,
        )

    # templated deny rail -> exit 2, message names the sanctioned path
    r = run({"tool_name": "Bash", "tool_input": {"command": "gog gmail send --to a@b.c"}})
    assert r.returncode == 2
    assert "bin/echo-email" in r.stderr

    # chained invocation is also railed
    r = run({"tool_name": "Bash", "tool_input": {"command": "cd /x && gog gmail reply --to a@b.c"}})
    assert r.returncode == 2

    # deny pattern only in prose (mid-line) -> NOT blocked
    r = run({"tool_name": "Bash", "tool_input": {"command": "git commit -m 'the gog gmail send rule'"}})
    assert r.returncode == 0

    # injected approve (Edit) -> ask
    r = run({"tool_name": "Edit", "tool_input": {"file_path": "/tmp/x"}})
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"

    # read (Bash git status) -> allow, no output
    r = run({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_gating_hook_rails_identity_override_but_allows_shim_and_other_email_cmds(tmp_path):
    """The identity-bleed rail: `canopy email send --account` from a Bash call is denied
    (identity comes from the repo's agent.json via the shim); the shim path, other
    canopy email subcommands, and --account on non-send subcommands stay free."""
    create_agent(_spec(), tmp_path / "echo")
    root = tmp_path / "echo"
    hook = root / "hooks" / "gating_guard.py"

    import os as _os
    env = {**_os.environ,
           "CANOPY_PLUGIN_DIR": str(Path(__file__).resolve().parents[1] / "plugins" / "canopy")}

    def run(command):
        return subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True, text=True, env=env,
        )

    r = run("canopy email send --account other@dimagi-ai.com --to x@y.z "
            "--subject s --body-file b.txt")
    assert r.returncode == 2
    assert "identity" in r.stderr.lower()

    assert run("bin/echo-email --to x@y.z --subject s --body-file b.txt").returncode == 0
    assert run("canopy email send --repo . --to x@y.z --subject s --body-file b.txt").returncode == 0
    assert run("canopy email preflight --account other@dimagi-ai.com").returncode == 0
    assert run("canopy email mark-read --account other@dimagi-ai.com t1").returncode == 0


def test_agent_core_docs_exist_and_are_agent_agnostic():
    """The stubs stamped by the factory point at agent-core docs shipped in the plugin;
    those docs must exist, be substantial, and carry no stamp-time {{TOKEN}}s
    (they are read at RUNTIME by any agent — identity lives in the stub)."""
    root = Path(__file__).resolve().parents[1] / "plugins" / "canopy" / "agent-core"
    for name in ("turn", "task-tracker", "manager-sync"):
        doc = root / f"{name}.md"
        assert doc.is_file(), f"missing agent-core doc: {doc}"
        text = doc.read_text()
        assert len(text) > 1000, f"{doc} suspiciously small — did the template body move here?"
        assert "{{" not in text, f"stamp-time token leaked into runtime doc {doc}"


def test_gating_hook_rails_mcp_drive_creates_and_reads_their_arguments(tmp_path):
    """The MCP half of the deliverable-filing rails (2026-08-13).

    Two structural gaps made this impossible before, and both are in the hook, not the
    rules: `_subject` returned "" for any non-built-in tool (so a pattern could never see an
    MCP call's arguments), and rules could only pin ONE exact tool name (while the same
    gdrive server is mounted under a different prefix per agent). Result: every rail was
    `tool: "Bash"`, and an agent holding a Drive-creating MCP tool could file anywhere.
    """
    create_agent(_spec(), tmp_path / "echo")
    hook = tmp_path / "echo" / "hooks" / "gating_guard.py"

    cfg_path = tmp_path / "echo" / "config" / "gating.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["channels"] = ["email", "gws"]          # mount the Drive rails
    cfg_path.write_text(json.dumps(cfg))

    import os as _os
    env = {**_os.environ,
           "CANOPY_PLUGIN_DIR": str(Path(__file__).resolve().parents[1] / "plugins" / "canopy")}

    def run(tool, tool_input):
        return subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
            capture_output=True, text=True, env=env,
        )

    # no destination -> blocked, and the message names the sanctioned path
    r = run("mcp__plugin_chrome-sales_gdrive__drive_create_file", {"name": "roster.csv"})
    assert r.returncode == 2
    assert "canopy gsheet publish" in r.stderr or "canopy gdoc publish" in r.stderr

    # a different plugin mount of the same server is matched by shape, not by an exact name
    r = run("mcp__plugin_ace_ace-gdrive__drive_create_folder", {"name": "Trip"})
    assert r.returncode == 2

    # destination supplied -> allowed
    r = run("mcp__plugin_chrome-sales_gdrive__drive_create_file",
            {"name": "roster.csv", "parent_id": "FOLDER123"})
    assert r.returncode == 0

    # reads and non-Drive MCP tools are untouched
    assert run("mcp__plugin_chrome-sales_gdrive__sheets_read", {"spreadsheet_id": "S"}).returncode == 0
    assert run("mcp__plugin_chrome-sales_salesforce__sf_query", {"soql": "SELECT Id FROM Account"}).returncode == 0


def test_gating_hook_rails_raw_gog_sheets_create(tmp_path):
    """`gog sheets create` with no --parent — the exact command that put a 45-row roster in
    Eva's My Drive root on 2026-08-12, unshared and dead-linked to the human who asked."""
    create_agent(_spec(), tmp_path / "echo")
    hook = tmp_path / "echo" / "hooks" / "gating_guard.py"
    cfg_path = tmp_path / "echo" / "config" / "gating.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["channels"] = ["email", "gws"]
    cfg_path.write_text(json.dumps(cfg))

    import os as _os
    env = {**_os.environ,
           "CANOPY_PLUGIN_DIR": str(Path(__file__).resolve().parents[1] / "plugins" / "canopy")}

    def run(cmd):
        return subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
            capture_output=True, text=True, env=env,
        )

    r = run("gog sheets create 'SF Trip Targets' -a echo@dimagi-ai.com")
    assert r.returncode == 2
    assert "My Drive root" in r.stderr

    assert run("gog sheets create 'X' --parent FOLDER123").returncode == 0
    assert run("gog sheets create --help").returncode == 0      # usage must stay readable
    assert run("gog sheets read SID 'A1:B2'").returncode == 0   # reads free


def _hook_env(**extra):
    import os as _os
    return {**_os.environ,
            "CANOPY_PLUGIN_DIR": str(Path(__file__).resolve().parents[1] / "plugins" / "canopy"),
            **extra}


def test_gating_hook_delegates_to_the_shared_engine(tmp_path):
    """End-to-end through the loader: deny / approve / allow all still work."""
    create_agent(_spec(), tmp_path / "echo")
    root = tmp_path / "echo"
    hook = root / "hooks" / "gating_guard.py"
    gating = root / "config" / "gating.json"
    cfg = json.loads(gating.read_text())
    cfg["approve"] = [{"tool": "Edit", "message": "Echo edits only with approval."}]
    gating.write_text(json.dumps(cfg))

    def run(payload):
        return subprocess.run([sys.executable, str(hook)], input=json.dumps(payload),
                              capture_output=True, text=True, env=_hook_env())

    r = run({"tool_name": "Bash", "tool_input": {"command": "gog gmail send --to a@b.c"}})
    assert r.returncode == 2 and "bin/echo-email" in r.stderr

    r = run({"tool_name": "Edit", "tool_input": {"file_path": "/tmp/x"}})
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    # the approval prompt still names the agent — read from config, not templated in
    assert "APPROVE Echo" in json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]

    assert run({"tool_name": "Bash", "tool_input": {"command": "git status"}}).returncode == 0


def test_per_statement_reaches_every_agent(tmp_path):
    """ada's `per_statement`, promoted to the shared engine on 2026-08-13.

    It sat in ada's private copy for three weeks; eva, hal and echo could not use it. This
    test is the guarantee that a feature invented at one leaf now reaches the fleet.
    """
    create_agent(_spec(), tmp_path / "echo")
    root = tmp_path / "echo"
    gating = root / "config" / "gating.json"
    cfg = json.loads(gating.read_text())
    # a multi-lookahead rail: a write verb AND the target host, in the SAME statement
    cfg["deny"] = [{
        "tool": "Bash",
        "per_statement": True,
        "pattern": r"(?=[\s\S]*\bcurl\b)(?=[\s\S]*example\.com)(?=[\s\S]*-X\s*POST)",
        "message": "BLOCKED: no writes to example.com.",
    }]
    gating.write_text(json.dumps(cfg))

    def run(cmd):
        return subprocess.run([sys.executable, str(root / "hooks" / "gating_guard.py")],
                              input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
                              capture_output=True, text=True, env=_hook_env())

    # the real violation still blocks
    assert run("curl -X POST https://example.com/items").returncode == 2
    # the false positive ada hit: a free GET plus an UNRELATED post in the next statement
    assert run("curl https://example.com/items && curl -X POST https://other.test/x").returncode == 0

