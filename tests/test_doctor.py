"""Tests for the doctor health-check module and the `canopy doctor` CLI."""
import json
from pathlib import Path

from click.testing import CliRunner

from orchestrator import doctor
from orchestrator.cli import main


def _make_healthy_home(home: Path, canopy_dir: Path) -> None:
    """Populate a tmp home + canopy dir so every check passes."""
    claude = home / ".claude"
    (claude / "plugins").mkdir(parents=True)
    canopy_dir.mkdir(parents=True, exist_ok=True)

    # Hook registration
    (claude / "settings.json").write_text(json.dumps({
        "hooks": {
            "PostToolUse": [
                {"hooks": [{"command": "python3 /path/to/hooks/post_tool_use.py"}]}
            ]
        }
    }))
    # Session log
    (canopy_dir / "session-log.jsonl").write_text('{"a": 1}\n{"b": 2}\n')
    # Repo map
    (canopy_dir / "repo-map.json").write_text(json.dumps({"proj": "owner/repo"}))
    # Workbench token (mode 600, non-empty)
    token = canopy_dir / "workbench-token"
    token.write_text("a-secret-token-value")
    token.chmod(0o600)
    # Installed plugins
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps({
        "plugins": {"canopy@canopy": [{"version": "0.2.119"}]}
    }))
    # CLI deployed from the marketplace clone, at the clone's version
    _make_cli_install(home, version="0.2.119")


def _make_cli_install(home: Path, version: str, source: Path | None = None) -> Path:
    """Lay down a marketplace clone + a uv tool install of the canopy CLI."""
    clone = home / ".claude" / "plugins" / "marketplaces" / "canopy"
    clone.mkdir(parents=True, exist_ok=True)
    (clone / "VERSION").write_text(version + "\n")

    receipt = home / ".local/share/uv/tools/canopy/uv-receipt.toml"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    directory = source if source is not None else clone
    receipt.write_text(
        "[tool]\n"
        f'requirements = [{{ name = "canopy", directory = "{directory}" }}]\n'
    )

    site = home / ".local/share/uv/tools/canopy/lib/python3.14/site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (site / f"canopy-{version}.dist-info").mkdir(exist_ok=True)
    return clone


def _make_plugin_hook_registration(home: Path, *, register: bool = True) -> Path:
    """Lay down an installed canopy plugin whose hooks.json registers PostToolUse.

    This is the CURRENT registration path: the capture hook is plugin-managed
    (``plugins/canopy/hooks/hooks.json``), and ``/canopy:setup`` deliberately
    REMOVES the legacy ~/.claude/settings.json entry.
    """
    install = home / ".claude" / "plugins" / "cache" / "canopy" / "canopy" / "0.2.347"
    (install / "hooks").mkdir(parents=True, exist_ok=True)
    hooks: dict = {"hooks": {"SessionStart": [{"matcher": "*", "hooks": []}]}}
    if register:
        hooks["hooks"]["PostToolUse"] = [
            {"matcher": "*", "hooks": [
                {"type": "command",
                 "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/post_tool_use.py"'},
            ]}
        ]
    (install / "hooks" / "hooks.json").write_text(json.dumps(hooks))

    registry = home / ".claude" / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({
        "plugins": {"canopy@canopy": [{"version": "0.2.347", "installPath": str(install)}]}
    }))
    return install


class TestCheckHookRegistered:
    def test_plugin_managed_registration_passes_without_settings_json(self, tmp_path):
        """The regression: a correctly-configured machine has NO settings.json entry.

        /canopy:setup removes the legacy registration once the plugin manages the
        hook, so requiring settings.json made this check fail forever — and its
        remediation ("run /canopy:setup") could never clear it.
        """
        _make_plugin_hook_registration(tmp_path)
        r = doctor.check_hook_registered(home=tmp_path)
        assert r.ok is True
        assert "plugin" in r.detail.lower()

    def test_plugin_installed_but_hook_absent_fails(self, tmp_path):
        _make_plugin_hook_registration(tmp_path, register=False)
        r = doctor.check_hook_registered(home=tmp_path)
        assert r.ok is False

    def test_legacy_settings_registration_still_passes(self, tmp_path):
        """Legacy registration remains valid — the plugin copy defers to it."""
        claude = tmp_path / ".claude"
        claude.mkdir(parents=True)
        (claude / "settings.json").write_text(json.dumps({
            "hooks": {"PostToolUse": [{"hooks": [{"command": "x post_tool_use.py"}]}]}
        }))
        r = doctor.check_hook_registered(home=tmp_path)
        assert r.ok is True

    def test_malformed_registry_falls_back_to_settings(self, tmp_path):
        plugins = tmp_path / ".claude" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "installed_plugins.json").write_text("{not json")
        (tmp_path / ".claude" / "settings.json").write_text(json.dumps({
            "hooks": {"PostToolUse": [{"hooks": [{"command": "x post_tool_use.py"}]}]}
        }))
        r = doctor.check_hook_registered(home=tmp_path)
        assert r.ok is True

    def test_neither_registration_fails(self, tmp_path):
        r = doctor.check_hook_registered(home=tmp_path)
        assert r.ok is False
        assert "not registered" in r.detail

    def test_registered_passes(self, tmp_path):
        claude = tmp_path / ".claude"
        claude.mkdir(parents=True)
        (claude / "settings.json").write_text(json.dumps({
            "hooks": {"PostToolUse": [{"hooks": [{"command": "x post_tool_use.py"}]}]}
        }))
        r = doctor.check_hook_registered(home=tmp_path)
        assert r.ok is True

    def test_present_but_unregistered_fails(self, tmp_path):
        claude = tmp_path / ".claude"
        claude.mkdir(parents=True)
        (claude / "settings.json").write_text(json.dumps({"hooks": {}}))
        r = doctor.check_hook_registered(home=tmp_path)
        assert r.ok is False

    def test_malformed_json_fails_gracefully(self, tmp_path):
        claude = tmp_path / ".claude"
        claude.mkdir(parents=True)
        (claude / "settings.json").write_text("{not json")
        r = doctor.check_hook_registered(home=tmp_path)
        assert r.ok is False


class TestCheckSessionLog:
    def test_missing_fails(self, tmp_path):
        r = doctor.check_session_log(canopy_dir=tmp_path)
        assert r.ok is False

    def test_empty_fails(self, tmp_path):
        (tmp_path / "session-log.jsonl").write_text("\n  \n")
        r = doctor.check_session_log(canopy_dir=tmp_path)
        assert r.ok is False

    def test_populated_passes(self, tmp_path):
        (tmp_path / "session-log.jsonl").write_text('{"a":1}\n{"b":2}\n')
        r = doctor.check_session_log(canopy_dir=tmp_path)
        assert r.ok is True
        assert "2 entries" in r.detail


class TestCheckRepoMap:
    def test_missing_fails(self, tmp_path):
        r = doctor.check_repo_map(canopy_dir=tmp_path)
        assert r.ok is False

    def test_valid_passes(self, tmp_path):
        (tmp_path / "repo-map.json").write_text(json.dumps({"a": "b", "c": "d"}))
        r = doctor.check_repo_map(canopy_dir=tmp_path)
        assert r.ok is True
        assert "2 project mappings" in r.detail

    def test_malformed_fails(self, tmp_path):
        (tmp_path / "repo-map.json").write_text("[not, valid")
        r = doctor.check_repo_map(canopy_dir=tmp_path)
        assert r.ok is False


class TestCheckWorkbenchToken:
    def test_missing_fails(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        r = doctor.check_workbench_token(home=tmp_path, canopy_dir=tmp_path)
        assert r.ok is False

    def test_empty_fails(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        token = tmp_path / "workbench-token"
        token.write_text("   ")
        token.chmod(0o600)
        r = doctor.check_workbench_token(home=tmp_path, canopy_dir=tmp_path)
        assert r.ok is False
        assert "empty" in r.detail

    def test_wrong_perms_fails(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        token = tmp_path / "workbench-token"
        token.write_text("secret")
        token.chmod(0o644)
        r = doctor.check_workbench_token(home=tmp_path, canopy_dir=tmp_path)
        assert r.ok is False
        assert "600" in r.detail

    def test_valid_passes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        token = tmp_path / "workbench-token"
        token.write_text("secret")
        token.chmod(0o600)
        r = doctor.check_workbench_token(home=tmp_path, canopy_dir=tmp_path)
        assert r.ok is True

    def test_plugin_data_env_takes_precedence(self, tmp_path, monkeypatch):
        plugin_data = tmp_path / "plugin_data"
        plugin_data.mkdir()
        token = plugin_data / "workbench-token"
        token.write_text("env-token")
        token.chmod(0o600)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
        canopy_dir = tmp_path / "canopy"
        canopy_dir.mkdir()
        r = doctor.check_workbench_token(home=tmp_path, canopy_dir=canopy_dir)
        assert r.ok is True


class TestCheckPluginVersion:
    def test_missing_fails(self, tmp_path):
        r = doctor.check_plugin_version(home=tmp_path)
        assert r.ok is False

    def test_valid_passes(self, tmp_path):
        plugins = tmp_path / ".claude" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "installed_plugins.json").write_text(json.dumps({
            "plugins": {"canopy@canopy": [{"version": "0.2.119"}]}
        }))
        r = doctor.check_plugin_version(home=tmp_path)
        assert r.ok is True
        assert "0.2.119" in r.detail


class TestRunDoctor:
    def test_all_pass(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        home = tmp_path / "home"
        canopy_dir = home / ".claude" / "canopy"
        home.mkdir()
        _make_healthy_home(home, canopy_dir)
        results, overall_ok = doctor.run_doctor(home=home, canopy_dir=canopy_dir)
        assert overall_ok is True
        assert all(r.ok for r in results)
        assert len(results) == len(doctor._CHECKS)

    def test_one_failure_flips_overall(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        home = tmp_path / "home"
        canopy_dir = home / ".claude" / "canopy"
        home.mkdir()
        _make_healthy_home(home, canopy_dir)
        # Break one check.
        (canopy_dir / "repo-map.json").unlink()
        results, overall_ok = doctor.run_doctor(home=home, canopy_dir=canopy_dir)
        assert overall_ok is False
        assert any(not r.ok for r in results)


class TestDoctorCLI:
    def test_all_pass_exit_zero(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        home = tmp_path / "home"
        canopy_dir = home / ".claude" / "canopy"
        home.mkdir()
        _make_healthy_home(home, canopy_dir)
        monkeypatch.setattr(doctor.Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(doctor, "CANOPY_DIR", canopy_dir)

        result = CliRunner().invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "All checks passed" in result.output

    def test_failure_exit_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        home = tmp_path / "home"
        canopy_dir = home / ".claude" / "canopy"
        home.mkdir()
        _make_healthy_home(home, canopy_dir)
        (canopy_dir / "session-log.jsonl").unlink()
        monkeypatch.setattr(doctor.Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(doctor, "CANOPY_DIR", canopy_dir)

        result = CliRunner().invoke(main, ["doctor"])
        assert result.exit_code != 0
        assert "FAIL" in result.output

    def test_json_output_is_valid(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        home = tmp_path / "home"
        canopy_dir = home / ".claude" / "canopy"
        home.mkdir()
        _make_healthy_home(home, canopy_dir)
        monkeypatch.setattr(doctor.Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(doctor, "CANOPY_DIR", canopy_dir)

        result = CliRunner().invoke(main, ["doctor", "--json-output"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert len(payload["checks"]) == len(doctor._CHECKS)
        assert all("name" in c and "ok" in c and "detail" in c for c in payload["checks"])


class TestCheckCliInstallSource:
    def test_no_receipt_fails(self, tmp_path):
        r = doctor.check_cli_install_source(home=tmp_path)
        assert r.ok is False
        assert "not installed via" in r.detail
        assert doctor.CLI_REMEDY in r.detail

    def test_marketplace_clone_passes(self, tmp_path):
        _make_cli_install(tmp_path, version="0.2.342")
        r = doctor.check_cli_install_source(home=tmp_path)
        assert r.ok is True, r.detail

    def test_dev_checkout_fails(self, tmp_path):
        """The real 2026-07-24 failure: receipt pointing at a dev checkout."""
        dev = tmp_path / "emdash-projects" / "canopy"
        dev.mkdir(parents=True)
        _make_cli_install(tmp_path, version="0.2.342", source=dev)

        r = doctor.check_cli_install_source(home=tmp_path)
        assert r.ok is False
        assert str(dev) in r.detail
        assert "drifts with whatever branch" in r.detail
        assert doctor.CLI_REMEDY in r.detail

    def test_malformed_receipt_fails_gracefully(self, tmp_path):
        receipt = tmp_path / ".local/share/uv/tools/canopy/uv-receipt.toml"
        receipt.parent.mkdir(parents=True)
        receipt.write_text("this is not = valid toml [[[")
        r = doctor.check_cli_install_source(home=tmp_path)
        assert r.ok is False
        assert doctor.CLI_REMEDY in r.detail


class TestCheckCliVersionSync:
    def test_in_sync_passes(self, tmp_path):
        _make_cli_install(tmp_path, version="0.2.342")
        r = doctor.check_cli_version_sync(home=tmp_path)
        assert r.ok is True, r.detail
        assert "0.2.342" in r.detail

    def test_stale_cli_fails(self, tmp_path):
        """Installed from the right place, but never reinstalled after a pull."""
        _make_cli_install(tmp_path, version="0.2.340")
        # Clone advances; the installed CLI does not.
        (tmp_path / ".claude/plugins/marketplaces/canopy/VERSION").write_text("0.2.342\n")

        r = doctor.check_cli_version_sync(home=tmp_path)
        assert r.ok is False
        assert "0.2.340" in r.detail and "0.2.342" in r.detail
        assert doctor.CLI_REMEDY in r.detail

    def test_missing_clone_fails(self, tmp_path):
        r = doctor.check_cli_version_sync(home=tmp_path)
        assert r.ok is False

    def test_no_dist_info_fails(self, tmp_path):
        _make_cli_install(tmp_path, version="0.2.342")
        import shutil
        shutil.rmtree(
            tmp_path / ".local/share/uv/tools/canopy/lib/python3.14/site-packages/canopy-0.2.342.dist-info"
        )
        r = doctor.check_cli_version_sync(home=tmp_path)
        assert r.ok is False
        assert doctor.CLI_REMEDY in r.detail
