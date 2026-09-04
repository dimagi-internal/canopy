"""Tests for the agent factory (canopy create-agent)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.agent_factory import (
    AgentSpec,
    AgentFactoryError,
    create_agent,
    normalize_slug,
)


def test_normalize_slug_ok():
    assert normalize_slug("Echo") == "echo"
    assert normalize_slug("sales bot") == "sales-bot"
    assert normalize_slug("partner_outreach") == "partner-outreach"


def test_normalize_slug_rejects_bad():
    with pytest.raises(AgentFactoryError):
        normalize_slug("1bad")          # must start with a letter
    with pytest.raises(AgentFactoryError):
        normalize_slug("x")             # too short
    with pytest.raises(AgentFactoryError):
        normalize_slug("Has Spaces!")   # punctuation


def test_normalize_slug_rejects_builtin_collision():
    # Naming an agent after a Claude Code built-in silently breaks slash dispatch.
    for reserved in ("doctor", "config", "model", "review"):
        with pytest.raises(AgentFactoryError):
            normalize_slug(reserved)


def _spec():
    return AgentSpec(
        slug="echo",
        display_name="Echo",
        mandate="be the marketing agent.",
        mailbox="echo@dimagi-ai.com",
        stakeholders="the Connect team",
    )


def test_create_agent_writes_full_layout(tmp_path):
    written = create_agent(_spec(), tmp_path / "echo")
    names = {p.relative_to(tmp_path / "echo").as_posix() for p in written}
    # The load-bearing primitives must all be present.
    for required in (
        ".claude-plugin/plugin.json",
        "CLAUDE.md",
        "persona.md",
        "config/gating.json",
        "config/agent.json",
        ".claude/settings.json",
        "hooks/gating_guard.py",
        "bin/echo-email",
        "skills/turn/SKILL.md",
        "skills/agent-turn-review/SKILL.md",
        "skills/task-tracker/SKILL.md",
    ):
        assert required in names, f"missing {required}"


def test_create_agent_stamps_auto_bump(tmp_path):
    """Issue #357: every new agent ships bump-on-every-merge automation."""
    written = create_agent(_spec(), tmp_path / "echo")
    names = {p.relative_to(tmp_path / "echo").as_posix() for p in written}
    assert "scripts/bump-plugin-version.py" in names
    assert ".github/workflows/auto-version-bump.yml" in names


def test_bump_script_bumps_all_version_fields(tmp_path):
    """The stamped bump script advances plugin.json + both marketplace.json fields."""
    root = tmp_path / "echo"
    create_agent(_spec(), root)
    # The factory doesn't stamp marketplace.json, so add one the way live agents carry it.
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "echo",
                "metadata": {"version": "0.1.0"},
                "plugins": [{"name": "echo", "source": "./", "version": "0.1.0"}],
            },
            indent=2,
        )
    )
    script = root / "scripts" / "bump-plugin-version.py"
    assert script.stat().st_mode & 0o111, "bump script should be executable"
    out = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "0.1.0 -> 0.1.1"
    plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
    assert plugin["version"] == "0.1.1"
    assert market["metadata"]["version"] == "0.1.1"
    assert market["plugins"][0]["version"] == "0.1.1"


def test_bump_script_tolerates_missing_marketplace(tmp_path):
    """Bumping must not crash on an agent that has no marketplace.json yet."""
    root = tmp_path / "echo"
    create_agent(_spec(), root)
    assert not (root / ".claude-plugin" / "marketplace.json").exists()
    subprocess.run(
        [sys.executable, str(root / "scripts" / "bump-plugin-version.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["version"] == "0.1.1"


def test_create_agent_substitutes_tokens(tmp_path):
    create_agent(_spec(), tmp_path / "echo")
    claude_md = (tmp_path / "echo" / "CLAUDE.md").read_text()
    assert "Echo" in claude_md
    assert "echo@dimagi-ai.com" in claude_md
    assert "{{" not in claude_md, "an unsubstituted token leaked into output"


def test_generated_json_is_valid(tmp_path):
    create_agent(_spec(), tmp_path / "echo")
    root = tmp_path / "echo"
    for rel in (".claude-plugin/plugin.json", "config/gating.json", ".claude/settings.json"):
        json.loads((root / rel).read_text())  # raises on malformed JSON
    plugin = json.loads((root / ".claude-plugin/plugin.json").read_text())
    assert plugin["name"] == "echo"
    assert plugin["version"] == "0.1.0"


def test_hook_is_executable_and_stdlib_only(tmp_path):
    create_agent(_spec(), tmp_path / "echo")
    hook = tmp_path / "echo" / "hooks" / "gating_guard.py"
    assert hook.stat().st_mode & 0o111, "hook should be executable"
    src = hook.read_text()
    # Hooks run under system python3 which may lack PyYAML — must not import it.
    assert "import yaml" not in src
    assert "import pyyaml" not in src.lower()


def test_create_agent_refuses_nonempty_dir(tmp_path):
    target = tmp_path / "echo"
    target.mkdir()
    (target / "existing.txt").write_text("hi")
    with pytest.raises(AgentFactoryError):
        create_agent(_spec(), target)
    # force=True scaffolds anyway without deleting the pre-existing file.
    create_agent(_spec(), target, force=True)
    assert (target / "existing.txt").exists()
    assert (target / "CLAUDE.md").exists()


def test_gating_defaults_to_deny_rails_only(tmp_path):
    """Issue #263 / shared-gog-gdrive.md §4: rails, not approval gates — now mounts-based.

    The templated gating carries channel MOUNTS (baseline deny rails ship centrally in the
    canopy plugin's agent-core/gating-baseline.json and are merged by the hook at call time)
    plus an EMPTY local deny list and an EMPTY approve list.
    """
    create_agent(_spec(), tmp_path / "echo")
    cfg = json.loads((tmp_path / "echo" / "config" / "gating.json").read_text())
    assert cfg["approve"] == []
    assert cfg["deny"] == []           # agent-specific ADDITIONS only; baseline is central
    assert cfg["channels"] == ["email"]
    assert cfg["slug"] == "echo"
    assert "rails" in cfg["_doc"].lower()
    assert "add-only" in cfg["_doc"].lower()


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


def test_agent_json_carries_email_identity(tmp_path):
    """Issue #261: `canopy email` resolves mailbox + gog client from config/agent.json."""
    create_agent(_spec(), tmp_path / "echo")
    agent = json.loads((tmp_path / "echo" / "config" / "agent.json").read_text())
    assert agent["email"] == "echo@dimagi-ai.com"
    # gog_client is the SHARED fleet OAuth client, not the per-agent slug — the mailbox is the
    # per-agent identity; the client is reused fleet-wide.
    assert agent["gog_client"] == "canopy"


def test_email_shim_is_executable_and_targets_canopy_engine(tmp_path):
    create_agent(_spec(), tmp_path / "echo")
    shim = tmp_path / "echo" / "bin" / "echo-email"
    assert shim.stat().st_mode & 0o111, "shim should be executable"
    src = shim.read_text()
    assert '"email", "send"' in src and '"--repo"' in src
    compile(src, str(shim), "exec")  # valid python
    # The shim resolves identity from ITS OWN repo, and records the routing contract.
    assert "thread_id" in src


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


def test_stub_skills_reference_agent_core(tmp_path):
    """turn + task-tracker are stamped as thin stubs that resolve the installed canopy
    plugin and read the canonical agent-core doc — never a full process copy."""
    create_agent(_spec(), tmp_path / "echo")
    for name in ("turn", "task-tracker", "manager-sync"):
        text = (tmp_path / "echo" / "skills" / name / "SKILL.md").read_text()
        assert "installed_plugins.json" in text, f"{name} stub must resolve the installed canopy path"
        assert f"agent-core/{name}.md" in text, f"{name} stub must point at its core doc"
        assert "canopy-update-check.sh" in text, f"{name} stub must staleness-check the core"
        assert "{{" not in text
        assert len(text) < 3000, f"{name} looks like a full copy, not a stub"


def test_gating_hook_fails_closed_when_baseline_unreadable(tmp_path):
    """channels mounted + baseline unresolvable → deny (exit 2) with the /canopy:update fix.
    A stale/absent canopy install must never silently run an agent without its fleet rails."""
    import os as _os
    create_agent(_spec(), tmp_path / "echo")
    hook = tmp_path / "echo" / "hooks" / "gating_guard.py"
    env = {**_os.environ, "CANOPY_PLUGIN_DIR": str(tmp_path / "nonexistent")}
    r = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}),
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 2
    assert "canopy:update" in r.stderr


def test_gating_hook_legacy_config_stays_local_only(tmp_path):
    """A config WITHOUT `channels` (legacy full-copy style, e.g. ACE's plugin-level setup)
    keeps local-rails-only behavior — no baseline lookup, no fail-closed brick."""
    import os as _os
    create_agent(_spec(), tmp_path / "echo")
    root = tmp_path / "echo"
    gating = root / "config" / "gating.json"
    gating.write_text(json.dumps({
        "deny": [{"tool": "Bash", "pattern": "forbidden_local_thing", "message": "BLOCKED: local rail."}],
        "approve": [],
    }))
    env = {**_os.environ, "CANOPY_PLUGIN_DIR": str(tmp_path / "nonexistent")}

    def run(command):
        return subprocess.run(
            [sys.executable, str(root / "hooks" / "gating_guard.py")],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True, text=True, env=env,
        )

    assert run("forbidden_local_thing now").returncode == 2
    assert run("git status").returncode == 0        # no channels → no baseline → no brick


def test_internal_skills_are_not_user_launchable(tmp_path):
    """Internal skills ship user-invocable:false (Claude-only); the turn skill stays launchable.

    A skill is already a slash command in Claude Code — launchability is native frontmatter,
    not a commands/ wrapper. Internal sub-procedures opt out of the /menu here.
    """
    create_agent(_spec(), tmp_path / "echo")
    root = tmp_path / "echo"
    for internal in ("task-tracker", "agent-turn-review"):
        fm = (root / "skills" / internal / "SKILL.md").read_text().split("---", 2)[1]
        assert "user-invocable: false" in fm, f"{internal} must be Claude-only"
    turn_fm = (root / "skills" / "turn" / "SKILL.md").read_text().split("---", 2)[1]
    assert "user-invocable: false" not in turn_fm, "turn must stay human-launchable"
    # the walk-back removed command wrappers entirely
    assert not (root / "commands").exists(), "factory no longer stamps commands/ wrappers"


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


def test_gating_hook_is_a_loader_with_no_agent_specifics(tmp_path):
    """The generated hook must carry NO agent-specific text and NO matching logic.

    This is the property that makes it shared: if the file were templated per agent, or held
    rules, we would be back to N forked copies — the state measured on 2026-08-13 (three of
    four agents silently behind on rail features, a fourth holding one nobody else could use).
    """
    create_agent(_spec(), tmp_path / "echo")
    body = (tmp_path / "echo" / "hooks" / "gating_guard.py").read_text()
    assert "Echo" not in body and "echo" not in body.replace("echo's", "")
    assert "runpy.run_path" in body
    # the engine's real matching surface must NOT be duplicated here
    assert "def matches(" not in body and "baseline_rails" not in body


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


def test_gating_loader_degrades_without_bricking_or_weakening(tmp_path):
    """Engine unresolvable. Availability may suffer; safety may not."""
    create_agent(_spec(), tmp_path / "echo")
    root = tmp_path / "echo"
    hook = root / "hooks" / "gating_guard.py"
    gating = root / "config" / "gating.json"
    broken = _hook_env(CANOPY_PLUGIN_DIR=str(tmp_path / "nonexistent"))

    def run(cmd, env):
        return subprocess.run([sys.executable, str(hook)],
                              input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
                              capture_output=True, text=True, env=env)

    # (a) legacy config, no channels -> local rails still enforced, reads still free
    gating.write_text(json.dumps({"deny": [
        {"tool": "Bash", "pattern": "forbidden_local_thing", "message": "BLOCKED: local rail."}]}))
    assert run("forbidden_local_thing now", broken).returncode == 2
    assert run("git status", broken).returncode == 0

    # (b) a rule using an engine-only feature must be assumed to FIRE, never skipped
    gating.write_text(json.dumps({"deny": [
        {"tool": "Bash", "per_statement": True, "pattern": "nope", "message": "BLOCKED: rich rule."}]}))
    assert run("anything at all", broken).returncode == 2

    # (c) channels mounted -> depends on rails it cannot read -> fail closed, naming the fix
    gating.write_text(json.dumps({"channels": ["email"], "deny": []}))
    r = run("git status", broken)
    assert r.returncode == 2 and "/canopy:update" in r.stderr


def test_templates_carry_non_ascii_and_round_trip(tmp_path):
    """The shipped templates contain non-ASCII, and it must survive the write.

    This is the regression that broke `canopy create-agent` on Windows: write_text with no
    encoding uses cp1252 there, and the CLAUDE.md template's arrow is not representable, so
    the very first command in section 4 of the onboarding doc died with UnicodeEncodeError
    and left a 0-byte file behind. Asserting the arrow is still IN the template matters as
    much as the round trip — if someone "fixes" this by making the templates ASCII, the
    encoding bug goes quiet without being fixed, and comes back with the next em-dash.
    """
    written = create_agent(_spec(), tmp_path / "agent")
    claude_md = next(p for p in written if p.name == "CLAUDE.md")
    body = claude_md.read_text(encoding="utf-8")
    assert "→" in body, "template lost its non-ASCII — the encoding bug is now untested"
    assert claude_md.stat().st_size > 0

    non_ascii = [p for p in written if any(ord(c) > 127 for c in p.read_text(encoding="utf-8"))]
    assert len(non_ascii) > 1, "expected several templates to carry non-ASCII"


def test_partial_scaffold_is_rolled_back(tmp_path, monkeypatch):
    """A crash mid-scaffold must leave NOTHING behind, so the retry isn't blocked.

    Before this, a failed run left a half-written repo — some files, a 0-byte one, empty
    dirs, no git init — and the obvious retry then failed on "target is not empty", which
    reads as a second, unrelated bug.
    """
    from orchestrator import agent_factory

    target = tmp_path / "agent"
    real_write = Path.write_text
    calls = {"n": 0}

    def exploding_write(self, data, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise UnicodeEncodeError("charmap", "x", 0, 1, "simulated cp1252 failure")
        return real_write(self, data, *a, **kw)

    monkeypatch.setattr(Path, "write_text", exploding_write)
    with pytest.raises(UnicodeEncodeError):
        agent_factory.create_agent(_spec(), target)
    assert not target.exists(), f"partial scaffold left behind: {list(target.rglob('*'))}"


def test_rollback_into_existing_dir_keeps_pre_existing_files(tmp_path, monkeypatch):
    """Rollback removes only what THIS call wrote — never a file that was already there."""
    from orchestrator import agent_factory

    target = tmp_path / "agent"
    target.mkdir()
    keeper = target / "PRE-EXISTING.md"
    keeper.write_text("do not delete me", encoding="utf-8")

    real_write = Path.write_text
    calls = {"n": 0}

    def exploding_write(self, data, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("simulated failure")
        return real_write(self, data, *a, **kw)

    monkeypatch.setattr(Path, "write_text", exploding_write)
    with pytest.raises(OSError):
        agent_factory.create_agent(_spec(), target, force=True)
    assert target.exists()
    assert keeper.read_text(encoding="utf-8") == "do not delete me"


def test_email_shim_help_survives_a_non_utf8_console(tmp_path):
    """`<slug>-email --help` must not die on a Windows console codepage.

    The shim prints its own docstring (which carries em-dashes) straight to stdout, and
    stdout encodes with STRICT errors — so on cp1252/cp437 the help text raised
    UnicodeEncodeError and the flag that exists to explain the tool killed it instead.
    (stderr is unaffected: it defaults to backslashreplace. Only stdout is strict, which
    is why this bites --help and not the warnings.)

    PYTHONIOENCODING=ascii is a stricter stand-in for a Windows console codepage, so this
    reproduces the failure on any platform.
    """
    written = create_agent(_spec(), tmp_path / "agent")
    shim = next(p for p in written if p.parent.name == "bin" and p.name.endswith("-email"))
    env = {**os.environ, "PYTHONIOENCODING": "ascii"}
    r = subprocess.run([sys.executable, str(shim), "--help"], capture_output=True, env=env)
    assert r.returncode == 0, f"shim --help crashed on an ascii console: {r.stderr.decode()[-400:]}"
    assert b"canopy email send" in r.stdout
