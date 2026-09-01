"""Tests for the doctor health-check module and the `canopy doctor` CLI."""
import json
import subprocess
from pathlib import Path

import pytest
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
    @pytest.fixture(autouse=True)
    def _no_ambient_pat(self, monkeypatch):
        """The check now honors CANOPY_WEB_PAT / CANOPY_TOKEN, so a developer (or
        CI runner) with either exported would otherwise turn every file-based
        case below green and hide a real regression. Clear them by default; the
        env cases set them explicitly."""
        monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
        monkeypatch.delenv("CANOPY_TOKEN", raising=False)

    def test_env_pat_passes_without_file(self, tmp_path, monkeypatch):
        """A headless host (the EC2 cloud runner) gets its PAT from the
        environment and can never run the browser mint flow."""
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        monkeypatch.setenv("CANOPY_WEB_PAT", "pat-from-env")
        r = doctor.check_workbench_token(home=tmp_path, canopy_dir=tmp_path)
        assert r.ok is True
        assert "CANOPY_WEB_PAT" in r.detail

    def test_canopy_token_env_also_accepted(self, tmp_path, monkeypatch):
        """CANOPY_TOKEN is what cloud_runner.py already exports into agent turns."""
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        monkeypatch.setenv("CANOPY_TOKEN", "pat-from-runner")
        r = doctor.check_workbench_token(home=tmp_path, canopy_dir=tmp_path)
        assert r.ok is True

    def test_blank_env_pat_falls_through_to_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        monkeypatch.setenv("CANOPY_WEB_PAT", "   ")
        r = doctor.check_workbench_token(home=tmp_path, canopy_dir=tmp_path)
        assert r.ok is False

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


class TestExternalTools:
    """Warn-only version drift check for third-party CLIs canopy shells out to."""

    def _runner(self, code=0, out="", err=""):
        return lambda: (code, out, err)

    def _payload(self, *formulae):
        return json.dumps({"formulae": list(formulae), "casks": []})

    def test_up_to_date_passes_without_warning(self):
        r = doctor.check_external_tools(runner=self._runner(0, self._payload()))
        assert r.ok and not r.warn
        assert "up to date" in r.detail

    def test_stale_tracked_tool_warns_but_does_not_fail(self):
        r = doctor.check_external_tools(runner=self._runner(0, self._payload(
            {"name": "gogcli", "installed_versions": ["0.12.0"], "current_version": "0.38.1"},
        )))
        assert r.ok, "a new upstream release must never fail CI"
        assert r.warn
        assert "gogcli 0.12.0 -> 0.38.1" in r.detail
        assert "brew upgrade gogcli" in r.detail

    def test_untracked_outdated_formula_is_ignored(self):
        r = doctor.check_external_tools(runner=self._runner(0, self._payload(
            {"name": "ffmpeg", "installed_versions": ["1.0"], "current_version": "2.0"},
        )))
        assert r.ok and not r.warn

    def test_empty_stdout_reports_could_not_check_not_all_clear(self):
        """brew exits 1 with EMPTY stdout and the reason only on stderr.

        Parsing stdout alone would raise, or report 'everything current'
        having looked at nothing. This is the real failure seen on a machine
        whose Homebrew Cellar held one root-only directory.
        """
        r = doctor.check_external_tools(runner=self._runner(
            1, "", "Error: Permission denied @ dir_initialize - /opt/homebrew/Cellar/coreutils"))
        assert r.ok and r.warn
        assert "could not check" in r.detail
        assert "Permission denied" in r.detail
        assert "up to date" not in r.detail

    def test_malformed_json_warns(self):
        r = doctor.check_external_tools(runner=self._runner(0, "not json"))
        assert r.ok and r.warn
        assert "could not parse" in r.detail

    def test_brew_absent_warns(self):
        def boom():
            raise FileNotFoundError("brew")
        r = doctor.check_external_tools(runner=boom)
        assert r.ok and r.warn
        assert "brew not installed" in r.detail

    def test_timeout_warns(self):
        def boom():
            raise subprocess.TimeoutExpired(cmd="brew", timeout=20)
        r = doctor.check_external_tools(runner=boom)
        assert r.ok and r.warn
        assert "could not run" in r.detail

    def test_warn_does_not_gate_overall_ok(self):
        results = [
            doctor.CheckResult("a", True, "fine"),
            doctor.CheckResult("b", True, "stale", warn=True),
        ]
        assert all(r.ok for r in results)

    def test_checkresult_defaults_to_no_warn(self):
        assert doctor.CheckResult("x", True, "d").warn is False


# ---------------------------------------------------------------------------
# Windows bring-up: two checks that reported FALSE failures, with a remediation
# that looped (@smazumdar, 2026-09-01).
#
# There is no Windows machine in the fleet, so these drive the platform branch
# through monkeypatched sys.platform / env rather than the real host —
# otherwise the win32 paths are untestable and stay broken.
# ---------------------------------------------------------------------------


class TestUvToolDirIsPlatformAware:
    def test_posix_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert doctor._uv_tool_dir(tmp_path) == tmp_path / ".local" / "share" / "uv" / "tools"

    def test_windows_uses_appdata_roaming(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "win32")
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("APPDATA", raising=False)
        # NOT ~/.local/share — uv installs to %APPDATA%\uv\tools on Windows,
        # so doctor looked in a directory that never exists there.
        assert doctor._uv_tool_dir(tmp_path) == tmp_path / "AppData" / "Roaming" / "uv" / "tools"

    def test_windows_honours_appdata_env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "win32")
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roam"))
        assert doctor._uv_tool_dir(tmp_path) == tmp_path / "Roam" / "uv" / "tools"

    def test_uv_tool_dir_override_wins_everywhere(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "custom"))
        for plat in ("win32", "darwin", "linux"):
            monkeypatch.setattr(doctor.sys, "platform", plat)
            assert doctor._uv_tool_dir(tmp_path) == tmp_path / "custom"


class TestDistInfoFoundInBothLayouts:
    def test_finds_windows_lib_site_packages_layout(self, tmp_path, monkeypatch):
        """Windows has Lib/site-packages with no interpreter-version segment.

        The old glob was `lib/python*/site-packages/...`, which matched nothing
        on Windows even though the dist-info was present and correct — and the
        suggested fix (reinstall) put it back in that same place, so the check
        failed forever.
        """
        monkeypatch.setattr(doctor.sys, "platform", "win32")
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
        site = tmp_path / "AppData/Roaming/uv/tools/canopy/Lib/site-packages"
        site.mkdir(parents=True)
        (site / "canopy-0.2.450.dist-info").mkdir()

        found = doctor._canopy_dist_infos(tmp_path)
        assert [p.name for p in found] == ["canopy-0.2.450.dist-info"]

    def test_still_finds_the_posix_layout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "linux")
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        site = tmp_path / ".local/share/uv/tools/canopy/lib/python3.14/site-packages"
        site.mkdir(parents=True)
        (site / "canopy-0.2.450.dist-info").mkdir()

        found = doctor._canopy_dist_infos(tmp_path)
        assert [p.name for p in found] == ["canopy-0.2.450.dist-info"]


class TestTokenPermissionCheckOnWindows:
    def _token(self, tmp_path, monkeypatch):
        # the env-var branch short-circuits the file check entirely
        for var in ("CANOPY_WEB_PAT", "CANOPY_TOKEN", "CLAUDE_PLUGIN_DATA"):
            monkeypatch.delenv(var, raising=False)
        token = tmp_path / "workbench-token"
        token.write_text("a-secret-token-value")
        return token

    def test_windows_does_not_fail_on_unrepresentable_mode_bits(self, tmp_path, monkeypatch):
        """os.stat on Windows can only ever return 666 or 444.

        So `perms != "600"` can never pass there however tightly the file is
        locked down with icacls, and the remediation (chmod 600) does nothing.
        """
        token = self._token(tmp_path, monkeypatch)
        token.chmod(0o666)
        monkeypatch.setattr(doctor.sys, "platform", "win32")

        result = doctor.check_workbench_token(home=tmp_path, canopy_dir=tmp_path)
        assert result.ok is True
        assert "not meaningful on Windows" in result.detail

    def test_posix_still_rejects_a_world_readable_token(self, tmp_path, monkeypatch):
        token = self._token(tmp_path, monkeypatch)
        token.chmod(0o644)
        monkeypatch.setattr(doctor.sys, "platform", "linux")

        result = doctor.check_workbench_token(home=tmp_path, canopy_dir=tmp_path)
        assert result.ok is False
        assert "600" in result.detail
