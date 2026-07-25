"""Tests for the per-agent doctor (`canopy agent doctor`)."""
import json
from types import SimpleNamespace

from click.testing import CliRunner

from orchestrator.agent_doctor import (
    check_email_auth,
    check_gating,
    check_hook_wiring,
    check_identity,
    check_registration,
    check_secrets_manifest,
    run_agent_doctor,
)
from orchestrator.canopy_web import CanopyError
from orchestrator.cli import main


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------

def _agent_repo(tmp_path, *, email="hal@dimagi-ai.com", slug="hal",
                gating=True, secrets=True, hooks=True, agent_json_extra=None,
                materialize=False):
    repo = tmp_path / slug
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": slug}))
    (repo / "config").mkdir()
    agent = {"name": slug.title(), "email": email}
    agent.update(agent_json_extra or {})
    (repo / "config" / "agent.json").write_text(json.dumps(agent))
    if gating:
        (repo / "config" / "gating.json").write_text(
            json.dumps({"deny": [{"tool": "Bash", "pattern": "zzz-never-matches",
                                  "message": "m"}],
                        "approve": []}))
    if hooks:
        (repo / "hooks").mkdir()
        (repo / "hooks" / "gating_guard.py").write_text("# guard\n")
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.json").write_text(json.dumps({
            "hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write",
                                      "hooks": [{"type": "command",
                                                 "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/gating_guard.py\""}]}]}
        }))
    if secrets:
        (repo / "config" / "secrets.yaml").write_text(
            "secrets:\n"
            "  - name: gog client\n"
            f"    op: op://AI-Agents/{slug} gog client/notesPlain\n"
            '    target: "{repo}/creds.json"\n'
            "env:\n"
            '  target: "{repo}/.env"\n'
            "  vars:\n"
            f"    - key: {slug.upper()}_GMAIL_ACCOUNT\n"
            f"      value: \"{email}\"\n")
        if materialize:
            (repo / "creds.json").write_text("{}")
            (repo / ".env").write_text("X=1\n")
    return repo


def _gog_home(tmp_path, *, slug="hal", account="hal@dimagi-ai.com"):
    home = tmp_path / "gogcli"
    home.mkdir()
    (home / f"credentials-{slug}.json").write_text('{"client_id": "x"}')
    (home / "config.json").write_text(
        json.dumps({"account_clients": {account: slug}}))
    return str(home)


def _ok_runner(cmd, capture_output, text, timeout):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


class _FakeClient:
    def __init__(self, identity, *, error=None, pending=()):
        self._error, self._pending = error, list(pending)

    def pending_commands(self):
        if self._error:
            raise self._error
        return self._pending


def _client_factory(*, error=None, pending=()):
    return lambda identity: _FakeClient(identity, error=error, pending=pending)


# --------------------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------------------

def test_identity_ok_reports_slug_mailbox_client(tmp_path):
    result, ident = check_identity(_agent_repo(tmp_path))
    assert result.ok
    assert ident.account == "hal@dimagi-ai.com"
    assert "gog_client=hal" in result.detail


def test_identity_missing_mailbox_fails(tmp_path):
    result, ident = check_identity(_agent_repo(tmp_path, email=""))
    assert not result.ok
    assert ident is None


def test_identity_placeholder_mailbox_fails(tmp_path):
    # A resolvable-but-placeholder mailbox (`<slug>@example.com`, the factory default) used to
    # pass "OK" — the trap that let eva sit on eva@example.com. It must now FAIL.
    result, ident = check_identity(_agent_repo(tmp_path, slug="eva", email="eva@example.com"))
    assert not result.ok
    assert ident is not None                      # it resolved; it's just not a real address
    assert "PLACEHOLDER" in result.detail


def test_gating_missing_file_fails(tmp_path):
    result = check_gating(_agent_repo(tmp_path, gating=False))
    assert not result.ok
    assert "no rails" in result.detail


def test_gating_counts_rules(tmp_path):
    result = check_gating(_agent_repo(tmp_path))
    assert result.ok
    # a legacy config (no `channels`) contributes no baseline rails — only its own
    assert "1 effective deny rail(s)" in result.detail and "0 fleet-baseline" in result.detail


def test_secrets_manifest_missing_points_at_provision(tmp_path):
    result = check_secrets_manifest(_agent_repo(tmp_path, secrets=False))
    assert not result.ok
    assert "canopy provision" in result.detail


def test_secrets_manifest_counts_entries(tmp_path):
    result = check_secrets_manifest(_agent_repo(tmp_path))
    assert result.ok
    assert "1 file secret(s), 1 env var(s)" in result.detail


def test_email_auth_skipped_without_identity():
    result = check_email_auth(None)
    assert not result.ok
    assert "skipped" in result.detail


def test_registration_404_names_register_command(tmp_path):
    _, ident = check_identity(_agent_repo(tmp_path))
    result = check_registration(
        ident, client_factory=_client_factory(
            error=CanopyError("GET /api/agents/hal/commands -> 404: agent 'hal' not found")))
    assert not result.ok
    assert "agent-publish register" in result.detail


def test_registration_ok_counts_pending(tmp_path):
    _, ident = check_identity(_agent_repo(tmp_path))
    result = check_registration(
        ident, client_factory=_client_factory(pending=[object(), object()]))
    assert result.ok
    assert "2 pending" in result.detail


# --------------------------------------------------------------------------------------
# composition + CLI
# --------------------------------------------------------------------------------------

def test_run_agent_doctor_all_green(tmp_path):
    repo = _agent_repo(tmp_path, materialize=True)
    results, ok = run_agent_doctor(
        repo, gog_dir=_gog_home(tmp_path), runner=_ok_runner,
        client_factory=_client_factory(),
        registry_path=str(_plugin_registry(tmp_path)))
    assert ok
    assert [r.ok for r in results] == [True] * 10


def test_run_agent_doctor_identity_failure_degrades_dependents(tmp_path):
    repo = _agent_repo(tmp_path, email="")
    results, ok = run_agent_doctor(
        repo, gog_dir=_gog_home(tmp_path), runner=_ok_runner,
        client_factory=_client_factory())
    assert not ok
    by_name = {r.name: r for r in results}
    assert not by_name["Identity"].ok
    assert "skipped" in by_name["Email auth (gog)"].detail
    assert "skipped" in by_name["canopy-web board"].detail
    # non-identity checks still ran
    assert by_name["Gating rails"].ok
    assert by_name["Secrets manifest"].ok


def test_cli_agent_doctor_json_and_exit_code(tmp_path, monkeypatch):
    repo = _agent_repo(tmp_path, secrets=False)
    monkeypatch.setattr(
        "orchestrator.agent_doctor.preflight",
        lambda identity, gog_dir=None, runner=None: (True, ["OK: gog Gmail ready"]))
    monkeypatch.setattr(
        "orchestrator.agent_doctor.AgentClient", _client_factory())
    result = CliRunner().invoke(main, ["agent", "doctor", "--repo", str(repo), "--json-output"])
    assert result.exit_code == 1  # secrets manifest missing
    payload = json.loads(result.output)
    assert payload["ok"] is False
    names = [c["name"] for c in payload["checks"]]
    assert names == ["Identity", "Plugin install", "Gating rails", "Hook wiring",
                     "Secrets manifest", "Secrets materialized", "Rails enforced",
                     "Email auth (gog)", "Auth services", "canopy-web board"]


def test_cli_agent_doctor_all_sweeps_fleet_and_gates_on_any_failure(tmp_path, monkeypatch):
    # `--all` discovers every agent, runs the per-agent doctor on each, and exits non-zero if ANY
    # agent has a failing check — the fleet readiness gate.
    from orchestrator.doctor import CheckResult
    from orchestrator.fleet_align import Agent

    good, bad = tmp_path / "good", tmp_path / "bad"
    monkeypatch.setattr(
        "orchestrator.fleet_align.discover_agents",
        lambda *a, **k: [Agent("good", good, True), Agent("bad", bad, True)])

    def fake_doctor(path, **kw):
        if path == good:
            return [CheckResult("Identity", True, "ok")], True
        return [CheckResult("Identity", False, "mailbox is the factory PLACEHOLDER")], False
    monkeypatch.setattr("orchestrator.agent_doctor.run_agent_doctor", fake_doctor)

    result = CliRunner().invoke(main, ["agent", "doctor", "--all", "--json-output"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert {a["slug"]: a["ok"] for a in payload["agents"]} == {"good": True, "bad": False}


# --------------------------------------------------------------------------------------
# review tweaks (2026-07-03): hook wiring, zero-rails, self-managed provisioning,
# full remediation
# --------------------------------------------------------------------------------------

def test_hook_wiring_green_when_guard_registered(tmp_path):
    result = check_hook_wiring(_agent_repo(tmp_path))
    assert result.ok


def test_hook_wiring_fails_without_settings_json(tmp_path):
    repo = _agent_repo(tmp_path)
    (repo / ".claude" / "settings.json").unlink()
    result = check_hook_wiring(repo)
    assert not result.ok and "decorative" in result.detail


def test_hook_wiring_fails_when_settings_dont_reference_guard(tmp_path):
    repo = _agent_repo(tmp_path)
    (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": []}}))
    result = check_hook_wiring(repo)
    assert not result.ok and "decorative" in result.detail


def test_hook_wiring_fails_without_guard_file(tmp_path):
    repo = _agent_repo(tmp_path)
    (repo / "hooks" / "gating_guard.py").unlink()
    result = check_hook_wiring(repo)
    assert not result.ok and "no enforcement" in result.detail


def test_gating_zero_rails_fails_for_outbound_capable_agent(tmp_path):
    repo = _agent_repo(tmp_path)
    (repo / "config" / "gating.json").write_text(json.dumps({"deny": [], "approve": []}))
    (repo / "bin").mkdir()
    (repo / "bin" / "hal-email").write_text("#!/usr/bin/env python3\n")
    result = check_gating(repo)
    assert not result.ok and "outbound-capable" in result.detail


def test_gating_zero_rails_ok_without_email_shim(tmp_path):
    repo = _agent_repo(tmp_path)
    (repo / "config" / "gating.json").write_text(json.dumps({"deny": [], "approve": []}))
    result = check_gating(repo)
    assert result.ok


def test_secrets_self_managed_provisioning_declared_in_agent_json(tmp_path):
    repo = _agent_repo(tmp_path, secrets=False,
                       agent_json_extra={"provisioning": ".env.tpl + op inject"})
    result = check_secrets_manifest(repo)
    assert result.ok and "self-managed" in result.detail


def test_secrets_missing_still_fails_without_declared_provisioning(tmp_path):
    repo = _agent_repo(tmp_path, secrets=False)
    result = check_secrets_manifest(repo)
    assert not result.ok and "provisioning" in result.detail


def test_email_auth_keeps_full_multiline_remediation():
    ident = SimpleNamespace(slug="hal", account="hal@dimagi-ai.com", client="hal")
    def runner(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="oauth broken")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        result = check_email_auth(ident, gog_dir=d, runner=runner)
    assert not result.ok
    # the FULL fix block survives, not just line 1 (an early line AND the last line both present)
    assert "gog login" in result.detail and "SHARED fleet OAuth client" in result.detail


# --------------------------------------------------------------------------------------
# check_auth_services — Apps Script (and full service surface) coverage
# --------------------------------------------------------------------------------------

def _auth_list_runner(services):
    """Fake `gog auth list --json` returning hal's account with the given services."""
    payload = json.dumps({"accounts": [
        {"email": "hal@dimagi-ai.com", "client": "hal", "services": list(services)}]})

    def run(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")
    return run


def _hal_identity():
    from orchestrator.agent_email import EmailIdentity
    return EmailIdentity(slug="hal", account="hal@dimagi-ai.com", client="hal")


def test_auth_services_passes_when_all_granted():
    from orchestrator.agent_doctor import check_auth_services
    from orchestrator.agent_email import LOGIN_SERVICES
    services = [s.strip() for s in LOGIN_SERVICES.split(",")]
    r = check_auth_services(_hal_identity(), runner=_auth_list_runner(services))
    assert r.ok and "granted" in r.detail


def test_auth_services_does_not_require_appscript_by_default():
    """hal/ace never use Apps Script. Requiring the fleet-wide LOGIN_SERVICES of every agent
    reported both as broken over a scope they don't use — a false positive that would have
    sent a human through a pointless browser re-login."""
    from orchestrator.agent_doctor import check_auth_services
    r = check_auth_services(
        _hal_identity(),
        runner=_auth_list_runner(["gmail", "drive", "docs", "sheets", "forms"]))
    assert r.ok and "appscript" not in r.detail


def _identity_with_repo(tmp_path, services, *, slug="echo"):
    from orchestrator.agent_email import EmailIdentity
    repo = tmp_path / slug
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "agent.json").write_text(json.dumps(
        {"name": slug.title(), "email": f"{slug}@dimagi-ai.com", "gog_services": services}))
    return EmailIdentity(slug=slug, account=f"{slug}@dimagi-ai.com", client=slug, repo=repo)


def test_auth_services_honours_per_agent_declared_services(tmp_path):
    """echo genuinely needs `slides`, which LOGIN_SERVICES omits — so the old fleet-wide
    check could never have caught it missing."""
    from orchestrator.agent_doctor import check_auth_services
    ident = _identity_with_repo(tmp_path, ["gmail", "drive", "slides"])

    def runner(cmd, capture_output, text, timeout):
        payload = json.dumps({"accounts": [
            {"email": "echo@dimagi-ai.com", "client": "echo",
             "services": ["gmail", "drive"]}]})
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    r = check_auth_services(ident, runner=runner)
    assert not r.ok and "slides" in r.detail and "gog_services" in r.detail


def test_auth_services_remediation_preserves_already_granted_scopes(tmp_path):
    """`gog login --services` REPLACES the grant set, so the fix command must re-request the
    scopes the agent already has — otherwise remediating one gap silently revokes others."""
    from orchestrator.agent_doctor import check_auth_services
    ident = _identity_with_repo(tmp_path, ["gmail", "slides"])

    def runner(cmd, capture_output, text, timeout):
        payload = json.dumps({"accounts": [
            {"email": "echo@dimagi-ai.com", "client": "echo",
             "services": ["gmail", "drive", "appscript"]}]})
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    r = check_auth_services(ident, runner=runner)
    assert not r.ok
    cmd = r.detail.split("--services ", 1)[1]
    for svc in ("appscript", "drive", "gmail", "slides"):
        assert svc in cmd


def test_auth_services_skips_when_not_introspectable():
    from orchestrator.agent_doctor import check_auth_services

    def gog_missing(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="err")
    r = check_auth_services(_hal_identity(), runner=gog_missing)
    assert r.ok and "skipped" in r.detail  # email-auth owns the hard failure, not this


def test_auth_services_skipped_without_identity():
    from orchestrator.agent_doctor import check_auth_services
    r = check_auth_services(None)
    assert not r.ok and "identity" in r.detail


# --------------------------------------------------------------------------------------
# check_gating — fleet-baseline rails mounted via `channels` count as rails
# --------------------------------------------------------------------------------------

def _fleet_baseline(tmp_path, rails=("email",)):
    """A stand-in installed canopy plugin dir holding agent-core/gating-baseline.json."""
    plugin = tmp_path / "canopy-plugin"
    (plugin / "agent-core").mkdir(parents=True)
    (plugin / "agent-core" / "gating-baseline.json").write_text(json.dumps({
        "channels": {ch: [{"tool": "Bash", "pattern": "gog gmail send",
                           "message": "use bin/{slug}-email"}] for ch in rails}
    }))
    return plugin


def test_gating_channels_baseline_counts_as_effective_rails(tmp_path, monkeypatch):
    """echo/hal ship `"deny": []` + `"channels": ["email"]`. gating_guard merges the fleet
    baseline in front of the local list at call time, so they ARE railed — counting only the
    local array reported both as unrailed outbound agents."""
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(_fleet_baseline(tmp_path)))
    repo = _agent_repo(tmp_path)
    (repo / "config" / "gating.json").write_text(
        json.dumps({"slug": "hal", "channels": ["email"], "deny": [], "approve": []}))
    (repo / "bin").mkdir()
    (repo / "bin" / "hal-email").write_text("#!/usr/bin/env python3\n")
    result = check_gating(repo)
    assert result.ok and "fleet-baseline" in result.detail


def test_gating_unresolvable_baseline_fails_because_guard_fails_closed(tmp_path, monkeypatch):
    """Channels mounted but the baseline unreadable is the state where gating_guard blocks
    EVERY guarded call — a hard failure, not a pass."""
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(tmp_path / "nonexistent"))
    repo = _agent_repo(tmp_path)
    (repo / "config" / "gating.json").write_text(
        json.dumps({"slug": "hal", "channels": ["email"], "deny": [], "approve": []}))
    result = check_gating(repo)
    assert not result.ok and "fails CLOSED" in result.detail


def test_gating_still_fails_when_no_channels_and_no_local_rails(tmp_path):
    """The original protection survives: an outbound agent mounting nothing and declaring
    nothing is genuinely unrailed."""
    repo = _agent_repo(tmp_path)
    (repo / "config" / "gating.json").write_text(json.dumps({"deny": [], "approve": []}))
    (repo / "bin").mkdir()
    (repo / "bin" / "hal-email").write_text("#!/usr/bin/env python3\n")
    result = check_gating(repo)
    assert not result.ok and "0 effective deny rails" in result.detail


# --------------------------------------------------------------------------------------
# check_hook_wiring — plugin-style registration (hooks/hooks.json) is valid
# --------------------------------------------------------------------------------------

def test_hook_wiring_accepts_plugin_style_hooks_json(tmp_path):
    """ace ships AS a Claude Code plugin and registers the guard in hooks/hooks.json, not
    .claude/settings.json. Checking only the latter called ace's live rails decorative."""
    repo = _agent_repo(tmp_path, hooks=False)
    (repo / "hooks").mkdir()
    (repo / "hooks" / "gating_guard.py").write_text("# guard\n")
    (repo / "hooks" / "hooks.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command",
             "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/gating_guard.py"'}]}]}
    }))
    result = check_hook_wiring(repo)
    assert result.ok and "hooks/hooks.json" in result.detail


def test_hook_wiring_fails_when_neither_path_registers_the_guard(tmp_path):
    repo = _agent_repo(tmp_path, hooks=False)
    (repo / "hooks").mkdir()
    (repo / "hooks" / "gating_guard.py").write_text("# guard\n")
    result = check_hook_wiring(repo)
    assert not result.ok and "decorative" in result.detail


# --------------------------------------------------------------------------------------
# check_plugin_install — repo cloned but plugin never installed
# --------------------------------------------------------------------------------------

def _plugin_registry(tmp_path, *, plugins=("hal",), scope="user", version="0.1.5"):
    reg = tmp_path / "installed_plugins.json"
    reg.write_text(json.dumps({"version": 2, "plugins": {
        f"{p}@{p}": [{"scope": scope, "version": version}] for p in plugins}}))
    return reg


def test_plugin_install_ok_when_registered(tmp_path):
    from orchestrator.agent_doctor import check_plugin_install
    repo = _agent_repo(tmp_path)
    r = check_plugin_install(repo, registry_path=str(_plugin_registry(tmp_path)))
    assert r.ok and "hal@hal installed" in r.detail and "user scope" in r.detail


def test_plugin_install_fails_when_repo_present_but_plugin_absent(tmp_path):
    """The dominant new-machine gap: full checkout, valid config, every other check green —
    and not one of the agent's skills can be invoked."""
    from orchestrator.agent_doctor import check_plugin_install
    repo = _agent_repo(tmp_path, agent_json_extra={"repo": "dimagi-internal/hal"})
    r = check_plugin_install(repo, registry_path=str(_plugin_registry(tmp_path, plugins=("ace",))))
    assert not r.ok
    assert "NOT installed" in r.detail
    assert "/plugin marketplace add dimagi-internal/hal" in r.detail
    assert "/plugin install hal@hal" in r.detail


def test_plugin_install_skipped_when_registry_absent(tmp_path):
    """Absence of introspection is not evidence of breakage (same rule as auth services)."""
    from orchestrator.agent_doctor import check_plugin_install
    repo = _agent_repo(tmp_path)
    r = check_plugin_install(repo, registry_path=str(tmp_path / "nope.json"))
    assert r.ok and "skipped" in r.detail


def test_plugin_install_na_without_plugin_manifest(tmp_path):
    from orchestrator.agent_doctor import check_plugin_install
    repo = _agent_repo(tmp_path)
    (repo / ".claude-plugin" / "plugin.json").unlink()
    r = check_plugin_install(repo, registry_path=str(_plugin_registry(tmp_path)))
    assert r.ok and "n/a" in r.detail


# --------------------------------------------------------------------------------------
# check_secrets_materialized — manifest declared vs actually provisioned HERE
# --------------------------------------------------------------------------------------

def test_secrets_materialized_fails_when_targets_absent(tmp_path):
    """A fresh macOS user has every repo, every manifest, and none of the resolved files."""
    from orchestrator.agent_doctor import check_secrets_materialized
    repo = _agent_repo(tmp_path)
    r = check_secrets_materialized(repo)
    assert not r.ok and "missing on this machine" in r.detail and "canopy provision" in r.detail


def test_secrets_materialized_passes_once_targets_exist(tmp_path):
    from orchestrator.agent_doctor import check_secrets_materialized
    repo = _agent_repo(tmp_path, materialize=True)
    r = check_secrets_materialized(repo)
    assert r.ok and "all 2 provisioned target(s) present" in r.detail


def test_secrets_materialized_skipped_without_manifest(tmp_path):
    from orchestrator.agent_doctor import check_secrets_materialized
    r = check_secrets_materialized(_agent_repo(tmp_path, secrets=False))
    assert r.ok and "skipped" in r.detail


# --------------------------------------------------------------------------------------
# check_rails_fire — configured vs actually ENFORCED
# --------------------------------------------------------------------------------------

def _railed_repo(tmp_path, monkeypatch, guard_body):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(_fleet_baseline(tmp_path)))
    repo = _agent_repo(tmp_path)
    (repo / "config" / "gating.json").write_text(
        json.dumps({"slug": "hal", "channels": ["email"], "deny": [], "approve": []}))
    (repo / "hooks" / "gating_guard.py").write_text(guard_body)
    return repo


def test_rails_fire_passes_when_guard_blocks_the_probe(tmp_path, monkeypatch):
    from orchestrator.agent_doctor import check_rails_fire
    repo = _railed_repo(tmp_path, monkeypatch, "import sys\nsys.exit(2)\n")
    r = check_rails_fire(repo)
    assert r.ok and "in force" in r.detail


def test_rails_fire_catches_configured_but_unenforced(tmp_path, monkeypatch):
    """The failure only an ACTIVE probe can see: valid config, guard that stops nothing —
    a broken import or bad interpreter leaves every file-reading check green."""
    from orchestrator.agent_doctor import check_rails_fire
    repo = _railed_repo(tmp_path, monkeypatch, "import sys\nsys.exit(0)\n")
    r = check_rails_fire(repo)
    assert not r.ok and "DECLARED BUT NOT ENFORCED" in r.detail


def test_rails_fire_skips_when_no_rail_predicts_a_block(tmp_path, monkeypatch):
    """Never assert a block the agent's own config doesn't call for."""
    from orchestrator.agent_doctor import check_rails_fire
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(_fleet_baseline(tmp_path, rails=())))
    repo = _agent_repo(tmp_path)
    (repo / "config" / "gating.json").write_text(
        json.dumps({"slug": "hal", "channels": [], "deny": [], "approve": []}))
    r = check_rails_fire(repo)
    assert r.ok and "skipped" in r.detail


# --------------------------------------------------------------------------------------
# heal_agent — --fix applies only the safe, non-interactive repairs
# --------------------------------------------------------------------------------------

def test_heal_agent_runs_fixer_only_for_failing_checks(tmp_path):
    from orchestrator.agent_doctor import heal_agent
    from orchestrator.doctor import CheckResult
    called = []
    fixers = {"Secrets materialized": ("provision", lambda repo: called.append(repo) or "did it")}
    results = [CheckResult("Secrets materialized", False, "missing"),
               CheckResult("Email auth (gog)", False, "dead token")]
    actions = heal_agent(tmp_path, results, fixers=fixers)
    assert actions == [("provision", True, "did it")]
    assert len(called) == 1  # the un-fixable check was left alone, not half-attempted


def test_heal_agent_reports_fixer_failure_without_raising(tmp_path):
    from orchestrator.agent_doctor import heal_agent
    from orchestrator.doctor import CheckResult

    def boom(repo):
        raise RuntimeError("op not signed in")

    actions = heal_agent(tmp_path, [CheckResult("Secrets materialized", False, "missing")],
                         fixers={"Secrets materialized": ("provision", boom)})
    assert actions == [("provision", False, "op not signed in")]


def test_heal_agent_noop_when_all_green(tmp_path):
    from orchestrator.agent_doctor import heal_agent
    from orchestrator.doctor import CheckResult
    assert heal_agent(tmp_path, [CheckResult("Secrets materialized", True, "fine")]) == []
