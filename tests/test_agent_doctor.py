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
    # `gog auth list` must answer with a real payload: a healthy box has hal@ authed under
    # the client its repo pins, with the core scopes. Returning empty here would read as
    # "this mailbox has no token anywhere" and fail the Auth client check on a green box.
    if cmd[:3] == ["gog", "auth", "list"]:
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"accounts": [
            {"email": "hal@dimagi-ai.com", "client": "hal",
             "services": ["gmail", "drive", "docs", "sheets", "forms"]}]}))
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
    assert "LEGACY" in result.detail


def test_secrets_manifest_env_tpl_is_primary_no_secrets_yaml_needed(tmp_path):
    """`.env.tpl` + `op inject` is the standard now — it must pass PRIMARY, on its own,
    with no `config/secrets.yaml` present."""
    repo = _agent_repo(tmp_path, secrets=False)
    (repo / ".env.tpl").write_text(
        "HAL_GMAIL_ACCOUNT=hal@dimagi-ai.com\n"
        "GDRIVE_ROOT_FOLDER=op://Agent-Hal/gdrive-root-folder/credential\n"
    )
    result = check_secrets_manifest(repo)
    assert result.ok
    assert ".env.tpl" in result.detail
    assert "2 var(s), 1 op:// ref(s)" in result.detail


def test_secrets_manifest_prefers_env_tpl_over_legacy_yaml(tmp_path):
    """When BOTH exist (mid-migration), `.env.tpl` wins — it's the primary manifest now."""
    repo = _agent_repo(tmp_path, secrets=True)
    (repo / ".env.tpl").write_text("KEY=op://Agent-Hal/some-item/credential\n")
    result = check_secrets_manifest(repo)
    assert result.ok
    assert ".env.tpl" in result.detail
    assert "LEGACY" not in result.detail


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
    assert [r.ok for r in results] == [True] * 12


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
    assert names == ["Identity", "Plugin install", "Required plugins", "Gating rails",
                     "Hook wiring", "Secrets manifest", "Secrets materialized",
                     "Rails enforced", "Email auth (gog)", "Auth client", "Auth services",
                     "canopy-web board"]


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
# check_required_plugins — sibling plugins the agent's own skills call
# --------------------------------------------------------------------------------------

def test_required_plugins_na_when_none_declared(tmp_path):
    from orchestrator.agent_doctor import check_required_plugins
    r = check_required_plugins(_agent_repo(tmp_path),
                               registry_path=str(_plugin_registry(tmp_path)))
    assert r.ok and "n/a" in r.detail


def test_required_plugins_fails_when_dependency_absent(tmp_path):
    """Eva's real 2026-07-26 state: her OWN plugin installed, chrome-sales — which every
    Salesforce/Drive tool she calls comes from — not installed, and only prose said so."""
    from orchestrator.agent_doctor import check_required_plugins
    repo = _agent_repo(tmp_path, slug="eva", email="eva@dimagi-ai.com", agent_json_extra={
        "required_plugins": [{"name": "chrome-sales",
                              "marketplace": "dimagi-internal/chrome-sales",
                              "note": "then run the chrome-sales:setup skill"}]})
    r = check_required_plugins(repo, registry_path=str(_plugin_registry(tmp_path,
                                                                       plugins=("eva",))))
    assert not r.ok
    assert "chrome-sales" in r.detail
    assert "/plugin marketplace add dimagi-internal/chrome-sales" in r.detail
    assert "/plugin install chrome-sales@chrome-sales" in r.detail
    assert "chrome-sales:setup skill" in r.detail


def test_required_plugins_accepts_bare_string_entries(tmp_path):
    from orchestrator.agent_doctor import check_required_plugins
    repo = _agent_repo(tmp_path, agent_json_extra={"required_plugins": ["chrome-sales"]})
    r = check_required_plugins(repo, registry_path=str(_plugin_registry(tmp_path)))
    assert not r.ok and "/plugin install chrome-sales@chrome-sales" in r.detail


def test_required_plugins_passes_when_installed(tmp_path):
    from orchestrator.agent_doctor import check_required_plugins
    repo = _agent_repo(tmp_path, agent_json_extra={"required_plugins": ["chrome-sales"]})
    reg = _plugin_registry(tmp_path, plugins=("hal", "chrome-sales"))
    r = check_required_plugins(repo, registry_path=str(reg))
    assert r.ok and "all 1 declared plugin(s) installed" in r.detail and "chrome-sales" in r.detail


def test_required_plugins_ignores_marketplace_suffix_when_matching(tmp_path):
    """Registry keys are `plugin@marketplace`; a dependency installed from a differently-named
    marketplace still counts as present."""
    from orchestrator.agent_doctor import check_required_plugins
    repo = _agent_repo(tmp_path, agent_json_extra={"required_plugins": ["chrome-sales"]})
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"version": 2, "plugins": {
        "chrome-sales@dimagi-internal": [{"scope": "user", "version": "2.0.0"}]}}))
    r = check_required_plugins(repo, registry_path=str(reg))
    assert r.ok


def test_required_plugins_skipped_when_registry_absent(tmp_path):
    """Absence of introspection is not evidence of breakage."""
    from orchestrator.agent_doctor import check_required_plugins
    repo = _agent_repo(tmp_path, agent_json_extra={"required_plugins": ["chrome-sales"]})
    r = check_required_plugins(repo, registry_path=str(tmp_path / "nope.json"))
    assert r.ok and "skipped" in r.detail


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


def test_secrets_materialized_skips_env_tpl_no_declared_target(tmp_path):
    """`.env.tpl` declares no target field (unlike `secrets.yaml`'s `env.target`) — this check
    can't guess where `op inject -o` was told to write, so it skips rather than false-failing."""
    from orchestrator.agent_doctor import check_secrets_materialized
    repo = _agent_repo(tmp_path, secrets=False)
    (repo / ".env.tpl").write_text("KEY=op://Agent-Hal/some-item/credential\n")
    r = check_secrets_materialized(repo)
    assert r.ok and "skipped" in r.detail and "op inject" in r.detail


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


# --------------------------------------------------------------------------------------
# check_auth_client — the mailbox is authed, but under which OAuth client?
#
# The state that took echo down on 2026-08-10 and that NOTHING in the shared doctor saw:
# the cloud box had a live echo@ token under the legacy `echo` client while the repo pinned
# `canopy`. `granted_services` looks up (account, client) as a pair, so the mismatch read as
# "account not found" -> None -> check_auth_services returned OK/"skipped". The one check
# that could have named the problem went green on it.
# --------------------------------------------------------------------------------------

def _accounts_runner(accounts, *, returncode=0):
    payload = json.dumps({"accounts": accounts})

    def run(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=returncode, stdout=payload, stderr="")
    return run


def test_auth_client_passes_when_the_pin_matches_the_token():
    from orchestrator.agent_doctor import check_auth_client
    r = check_auth_client(_hal_identity(), runner=_accounts_runner([
        {"email": "hal@dimagi-ai.com", "client": "hal", "services": ["gmail"]}]))
    assert r.ok and "hal" in r.detail


def test_auth_client_fails_when_mailbox_is_authed_under_another_client():
    """The exact cloud-box state. This must FAIL loudly and name the re-login, not skip."""
    from orchestrator.agent_doctor import check_auth_client
    from orchestrator.agent_email import EmailIdentity
    ident = EmailIdentity(slug="echo", account="echo@dimagi-ai.com", client="canopy")
    r = check_auth_client(ident, runner=_accounts_runner([
        {"email": "echo@dimagi-ai.com", "client": "echo",
         "services": ["gmail", "drive"]}]))
    assert not r.ok
    assert "`echo`" in r.detail and "canopy" in r.detail
    assert "gog login echo@dimagi-ai.com --client canopy" in r.detail


def test_auth_client_relogin_fix_preserves_granted_scopes():
    """`gog login --services` REPLACES the grant set. A re-login onto the configured client
    must re-request what the stray token already had, or fixing the client silently revokes
    scopes the agent uses."""
    from orchestrator.agent_doctor import check_auth_client
    from orchestrator.agent_email import EmailIdentity
    ident = EmailIdentity(slug="echo", account="echo@dimagi-ai.com", client="canopy")
    r = check_auth_client(ident, runner=_accounts_runner([
        {"email": "echo@dimagi-ai.com", "client": "echo",
         "services": ["gmail", "drive", "appscript"]}]))
    cmd = r.detail.split("--services ", 1)[1]
    for svc in ("appscript", "drive", "gmail"):
        assert svc in cmd


def test_auth_client_reports_no_token_at_all_distinctly():
    from orchestrator.agent_doctor import check_auth_client
    r = check_auth_client(_hal_identity(), runner=_accounts_runner([
        {"email": "someone-else@dimagi-ai.com", "client": "canopy", "services": ["gmail"]}]))
    assert not r.ok and "no gog token" in r.detail.lower()


def test_auth_client_skips_when_gog_cannot_be_introspected():
    from orchestrator.agent_doctor import check_auth_client
    r = check_auth_client(_hal_identity(), runner=_accounts_runner([], returncode=1))
    assert r.ok and "skipped" in r.detail


def test_auth_client_skipped_without_identity():
    from orchestrator.agent_doctor import check_auth_client
    r = check_auth_client(None)
    assert not r.ok and "identity" in r.detail


def test_auth_services_scores_the_client_that_actually_holds_the_token(tmp_path):
    """The regression that hid the cloud-box failure: with the pin pointing at a client that
    has no token, this used to report "not introspectable" and pass. It must instead score
    the mailbox's real grant set — here, missing `slides`."""
    from orchestrator.agent_doctor import check_auth_services
    ident = _identity_with_repo(tmp_path, ["gmail", "drive", "slides"])
    ident.client = "canopy"                      # repo pin
    r = check_auth_services(ident, runner=_accounts_runner([
        {"email": "echo@dimagi-ai.com", "client": "echo",   # token lives here
         "services": ["gmail", "drive"]}]))
    assert not r.ok and "slides" in r.detail


def test_auth_services_still_skips_when_the_mailbox_has_no_token_anywhere():
    """No token at all is check_auth_client's finding (and email-auth's) — not a second,
    duplicate red herring from the services check."""
    from orchestrator.agent_doctor import check_auth_services
    r = check_auth_services(_hal_identity(), runner=_accounts_runner([]))
    assert r.ok and "skipped" in r.detail


def test_agent_doctor_runs_the_auth_client_check(tmp_path):
    from orchestrator.agent_doctor import run_agent_doctor
    repo = _agent_repo(tmp_path)
    results, _ = run_agent_doctor(repo, runner=_accounts_runner([]),
                                  client_factory=_FakeClient)
    assert any(r.name == "Auth client" for r in results)


def test_auth_client_offers_both_directions_not_just_a_relogin():
    """Which side is stale is not knowable from here: the box may have missed the client
    migration, or the checkout may predate a pin that already moved. A one-way "go re-login"
    is wrong half the time, and wrong costs a browser round-trip and a scope-REPLACING grant.
    Both ace and echo flagged here on 2026-08-11 for the checkout reason, not the box one."""
    from orchestrator.agent_doctor import check_auth_client
    from orchestrator.agent_email import EmailIdentity
    ident = EmailIdentity(slug="echo", account="echo@dimagi-ai.com", client="echo")
    r = check_auth_client(ident, runner=_accounts_runner([
        {"email": "echo@dimagi-ai.com", "client": "canopy", "services": ["gmail"]}]))
    assert not r.ok
    assert "gog login echo@dimagi-ai.com --client echo" in r.detail   # migrate the box
    assert "gog_client" in r.detail and "`canopy`" in r.detail        # or move the pin
    assert "pull first" in r.detail
