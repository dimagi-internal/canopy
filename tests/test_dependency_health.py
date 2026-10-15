"""Tests for the external-dependency checks (canopy #614).

The keychain case these cover cost a fleet box ~85 minutes of blocked inbox
polls and ~50 futile "Always Allow" clicks on 2026-09-07.
"""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import dependency_health as dh


def _ident(account="ada@dimagi-ai.com", client="canopy"):
    return SimpleNamespace(account=account, client=client)


def _runner_for(mapping):
    """Fake subprocess.run dispatching on the first two argv tokens."""
    def run(cmd, **kw):
        for prefix, result in mapping.items():
            if list(cmd[:len(prefix)]) == list(prefix):
                return result
        return SimpleNamespace(returncode=1, stdout="", stderr="no match")
    return run


# ---------------------------------------------------------------- upgrades

def test_upgrade_check_reports_only_fleet_dependencies(monkeypatch):
    """A dev laptop has hundreds of outdated formulae; only ours are news."""
    monkeypatch.setattr(dh.shutil, "which", lambda n: "/opt/homebrew/bin/brew")
    payload = {"formulae": [
        {"name": "abseil", "installed_versions": ["1"], "current_version": "2"},
        {"name": "gogcli", "installed_versions": ["0.38.1"], "current_version": "0.39.0"},
    ]}
    r = _runner_for({("brew", "outdated"): SimpleNamespace(
        returncode=0, stdout=json.dumps(payload), stderr="")})
    res = dh.check_dependency_upgrades(runner=r)
    assert "gogcli 0.38.1 -> 0.39.0" in res.detail
    assert "abseil" not in res.detail


def test_upgrade_check_attaches_the_keychain_warning_to_gog(monkeypatch):
    """The upgrade and its consequence must arrive together, not separately."""
    monkeypatch.setattr(dh.shutil, "which", lambda n: "/opt/homebrew/bin/brew")
    payload = {"formulae": [
        {"name": "gogcli", "installed_versions": ["0.38.1"], "current_version": "0.39.0"}]}
    r = _runner_for({("brew", "outdated"): SimpleNamespace(
        returncode=0, stdout=json.dumps(payload), stderr="")})
    res = dh.check_dependency_upgrades(runner=r)
    assert "re-mint" in res.detail.lower()
    assert "Always Allow" not in res.detail or "cannot" in res.detail


def test_upgrade_check_is_never_a_failure(monkeypatch):
    """A waiting upgrade is news, not unreadiness — red here would be ignored."""
    monkeypatch.setattr(dh.shutil, "which", lambda n: "/opt/homebrew/bin/brew")
    payload = {"formulae": [
        {"name": "gogcli", "installed_versions": ["0.38.1"], "current_version": "0.39.0"}]}
    r = _runner_for({("brew", "outdated"): SimpleNamespace(
        returncode=0, stdout=json.dumps(payload), stderr="")})
    assert dh.check_dependency_upgrades(runner=r).ok is True


def test_upgrade_check_skips_cleanly_without_brew(monkeypatch):
    monkeypatch.setattr(dh.shutil, "which", lambda n: None)
    res = dh.check_dependency_upgrades(runner=_runner_for({}))
    assert res.ok and "skipped" in res.detail


# ---------------------------------------------------- keychain trust

def _kc(stamp):
    return SimpleNamespace(
        returncode=0,
        stdout=f'    "cdat"<timedate>=0x00  "{stamp}\\000"\n', stderr="")


def _bin(tmp_path, mtime_epoch):
    import os
    b = tmp_path / "gog"; b.write_text("#!/bin/sh\n")
    os.utime(b, (mtime_epoch, mtime_epoch))
    return b


def test_flags_the_2026_09_07_state(tmp_path):
    """The real incident: token minted 2026-07-14, gog upgraded 2026-08-28."""
    binary = _bin(tmp_path, datetime(2026, 8, 28, tzinfo=timezone.utc).timestamp())
    r = _runner_for({("security", "find-generic-password"): _kc("20260714145255Z")})
    res = dh.check_gog_keychain_trust(_ident(), runner=r, binary_resolver=lambda: binary)
    assert res.ok is False
    assert "upgraded after this token was minted" in res.detail
    assert "gog auth import" in res.detail          # the actual repair
    assert "Always Allow" in res.detail             # and the trap, named


def test_clears_once_the_token_is_re_minted(tmp_path):
    """After the repair the token is newer than the binary — the check must go green."""
    binary = _bin(tmp_path, datetime(2026, 8, 28, tzinfo=timezone.utc).timestamp())
    r = _runner_for({("security", "find-generic-password"): _kc("20260907140441Z")})
    res = dh.check_gog_keychain_trust(_ident(), runner=r, binary_resolver=lambda: binary)
    assert res.ok is True


def test_never_reads_the_secret(tmp_path):
    """`-w` would dump the token AND raise the very prompt we are diagnosing."""
    seen = []
    binary = _bin(tmp_path, datetime(2026, 8, 28, tzinfo=timezone.utc).timestamp())

    def run(cmd, **kw):
        seen.append(list(cmd))
        return _kc("20260907140441Z")

    dh.check_gog_keychain_trust(_ident(), runner=run, binary_resolver=lambda: binary)
    assert seen, "expected a keychain lookup"
    assert all("-w" not in c for c in seen)


def test_skips_when_no_token_exists(tmp_path):
    binary = _bin(tmp_path, datetime(2026, 8, 28, tzinfo=timezone.utc).timestamp())
    r = _runner_for({("security", "find-generic-password"):
                     SimpleNamespace(returncode=44, stdout="", stderr="not found")})
    res = dh.check_gog_keychain_trust(_ident(), runner=r, binary_resolver=lambda: binary)
    assert res.ok and "no gogcli keychain token" in res.detail


def test_skips_when_gog_absent(tmp_path):
    res = dh.check_gog_keychain_trust(
        _ident(), runner=_runner_for({}), binary_resolver=lambda: None)
    assert res.ok and "not installed" in res.detail


# ------------------------------------- keychain trust: prompt-log confirmation
#
# 2026-10-14: the date heuristic fired on jjackson's Mac while gog polled five
# mailboxes with zero keychain prompts. The prompt log is the ground truth.

_LOG_HEADER = "Timestamp               Ty Process[PID:TID]\n"
_PROMPT_LINE = ("2026-10-14 16:50:01.000 Df securityd[123:abc] [com.apple.securityd:] "
                "displaying keychain prompt for /opt/homebrew/Cellar/gogcli/0.43.0/bin/gog\n")
_LOG_SELF_LINE = ("2026-10-14 16:54:22.361 Df log[4533:26e6fb3] [com.apple.log:] log run "
                  "noninteractively, args: '/usr/bin/log' 'show' '--predicate' "
                  "'eventMessage CONTAINS \"displaying keychain prompt\"'\n")


def _tripped(tmp_path, log_result):
    """Binary newer than the token (heuristic trips), with the given `log show` result."""
    binary = _bin(tmp_path, datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    calls = []
    kc = _kc("20260907140441Z")

    def run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[0] == "security":
            return kc
        if cmd[0] == "/usr/bin/log":
            if isinstance(log_result, Exception):
                raise log_result
            return log_result
        return SimpleNamespace(returncode=1, stdout="", stderr="no match")
    return binary, run, calls


def _log(stdout, rc=0):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr="")


def test_prompts_seen_keeps_the_fail(tmp_path):
    binary, run, _ = _tripped(tmp_path, _log(_LOG_HEADER + _PROMPT_LINE * 3))
    res = dh.check_gog_keychain_trust(
        _ident(), runner=run, binary_resolver=lambda: binary, gog_used=True)
    assert res.ok is False and not res.warn
    assert "3 keychain prompt" in res.detail
    assert "gog auth import" in res.detail          # re-mint instructions kept


def test_zero_prompts_while_gog_worked_is_a_warn(tmp_path):
    binary, run, calls = _tripped(tmp_path, _log(_LOG_HEADER))
    res = dh.check_gog_keychain_trust(
        _ident(), runner=run, binary_resolver=lambda: binary, gog_used=True)
    assert res.ok is True and res.warn is True
    assert "no keychain prompts in the last hour" in res.detail
    assert "gog is trusted" in res.detail
    log_cmd = next(c for c in calls if c[0] == "/usr/bin/log")
    assert log_cmd[:5] == ["/usr/bin/log", "show", "--last", "1h", "--style"]


def test_log_echoing_its_own_args_is_not_a_prompt(tmp_path):
    binary, run, _ = _tripped(tmp_path, _log(_LOG_HEADER + _LOG_SELF_LINE))
    res = dh.check_gog_keychain_trust(
        _ident(), runner=run, binary_resolver=lambda: binary, gog_used=True)
    assert res.ok is True and res.warn is True


def test_zero_prompts_without_gog_use_proves_nothing(tmp_path):
    binary, run, _ = _tripped(tmp_path, _log(_LOG_HEADER))
    res = dh.check_gog_keychain_trust(
        _ident(), runner=run, binary_resolver=lambda: binary, gog_used=False)
    assert res.ok is False
    assert "does not confirm trust" in res.detail


@pytest.mark.parametrize("log_result", [
    _log("", rc=1),                                           # unpermitted / failed
    subprocess.TimeoutExpired(cmd="log", timeout=20),         # hung
    FileNotFoundError("/usr/bin/log"),                        # unavailable
])
def test_unreadable_log_keeps_the_fail_with_a_caveat(tmp_path, log_result):
    binary, run, _ = _tripped(tmp_path, log_result)
    res = dh.check_gog_keychain_trust(
        _ident(), runner=run, binary_resolver=lambda: binary, gog_used=True)
    assert res.ok is False
    assert "(could not confirm against the keychain prompt log)" in res.detail
    assert "gog auth import" in res.detail


def test_heuristic_not_tripped_never_reads_the_log(tmp_path):
    binary = _bin(tmp_path, datetime(2026, 8, 28, tzinfo=timezone.utc).timestamp())
    calls = []

    def run(cmd, **kw):
        calls.append(list(cmd))
        return _kc("20260907140441Z")

    res = dh.check_gog_keychain_trust(
        _ident(), runner=run, binary_resolver=lambda: binary, gog_used=True)
    assert res.ok is True and not res.warn
    assert not any(c[0] == "/usr/bin/log" for c in calls)
