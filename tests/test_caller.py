"""`canopy caller tier`: act needs an allowlisted address AND a verified message."""
import json

import pytest
from click.testing import CliRunner

from orchestrator.caller import (ACT, BLOCKED, CALLER, SYSTEM, UNLISTED, UNVERIFIED, allowlisted,
                                 caller_group, load_allowlist, resolve)

RULES = ["@dimagi.com", "partner@llo-foo.org"]


def _env(email="beth@dimagi.com", *, kind="contact", verified=True, blocked=False,
         assurance="dmarc"):
    person_key = "contact" if kind == "contact" else "user"
    return {"who": {"kind": kind, "assurance": assurance, "via": "email",
                    person_key: {"email": email}},
            "verified": verified, "relationship": "caller",
            "contact": {"is_blocked": blocked} if kind == "contact" else None}


def test_allowlisted_and_verified_is_act():
    assert resolve(_env(), RULES)["tier"] == ACT


def test_a_spoofed_allowlisted_address_is_not_act():
    """The whole point: From: beth@dimagi.com with no DMARC alignment is a claim."""
    got = resolve(_env(verified=False, assurance="none"), RULES)
    assert got["tier"] == UNVERIFIED
    assert "can be forged" in got["reason"]


def test_an_exact_address_rule_matches_only_that_address():
    assert resolve(_env("partner@llo-foo.org"), RULES)["tier"] == ACT
    assert resolve(_env("other@llo-foo.org"), RULES)["tier"] == UNLISTED


def test_a_lookalike_domain_does_not_match():
    assert not allowlisted("x@evil-dimagi.com", RULES)
    assert not allowlisted("x@dimagi.com.evil.org", RULES)
    assert allowlisted("X@DIMAGI.COM", RULES)


def test_blocked_wins_over_everything():
    assert resolve(_env(blocked=True), RULES)["tier"] == BLOCKED


def test_a_schedule_is_system():
    env = {"who": {"kind": "system", "via": "schedule:4"}, "verified": True}
    assert resolve(env, RULES)["tier"] == SYSTEM


def test_a_signed_in_member_is_act_by_their_account():
    assert resolve(_env(kind="user", assurance="session"), RULES)["tier"] == ACT


def test_allowlist_file_parsing(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "allowlist.txt").write_text(
        "# header\n\n@Dimagi.com   # staff\nx@y.org\n")
    assert load_allowlist(tmp_path) == ["@dimagi.com", "x@y.org"]
    assert load_allowlist(tmp_path / "nope") == []


def test_cli_reads_the_envelope_file(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "allowlist.txt").write_text("@dimagi.com\n")
    f = tmp_path / "c.json"
    f.write_text(json.dumps(_env(verified=False)))
    r = CliRunner().invoke(caller_group, ["tier", "--caller", str(f), "--repo", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["tier"] == UNVERIFIED


@pytest.mark.parametrize("content", [None, "not json"])
def test_cli_exits_2_without_a_usable_envelope(tmp_path, content):
    f = tmp_path / "c.json"
    if content is not None:
        f.write_text(content)
    r = CliRunner().invoke(caller_group, ["tier", "--caller", str(f), "--repo", str(tmp_path)])
    assert r.exit_code == 2


# --- canopy's grant wins over the repo allowlist -------------------------------------

def test_a_domain_grant_is_act_without_any_allowlist():
    env = {**_env("nlesh@dimagi-associate.com"), "granted_by": "full:contact@dimagi-associate.com:verified"}
    got = resolve(env, [])
    assert got["tier"] == ACT and "dimagi-associate.com" in got["reason"]


def test_a_capability_grant_is_a_confined_caller_even_if_allowlisted():
    env = {**_env("beth@dimagi.com", verified=False), "granted_by": "capability:ask"}
    assert resolve(env, RULES)["tier"] == CALLER


def test_no_interface_falls_back_to_the_allowlist():
    env = {**_env(), "granted_by": "no-interface"}
    assert resolve(env, RULES)["tier"] == ACT
    assert resolve({**env, "verified": False}, RULES)["tier"] == UNVERIFIED


def test_blocked_still_wins_over_a_grant():
    env = {**_env(blocked=True), "granted_by": "full:contact@dimagi.com:verified"}
    assert resolve(env, RULES)["tier"] == BLOCKED


# --- envelope VERSION 2 words (canopy-web, 2026-10-04) -------------------------------

@pytest.mark.parametrize("rel", ["caller", "contact"])
def test_relationship_is_reported_in_todays_word(rel):
    assert resolve({**_env(), "relationship": rel}, RULES)["relationship"] == "contact"


def test_a_workspace_editor_is_act_but_told_it_is_manual():
    env = {**_env(kind="user"), "relationship": "member", "granted_by": "editor"}
    got = resolve(env, [])
    assert got["tier"] == ACT and "MANUAL" in got["reason"]


def test_a_confined_envelope_is_the_caller_tier_in_either_version():
    for prof in ("confined", "restricted"):
        env = {**_env(), "profile": prof, "granted_by": "capability:ask"}
        assert resolve(env, RULES)["tier"] == CALLER


def test_a_system_account_acts_in_the_editor_tier_and_is_named_as_automated():
    """canopy-web#1253: alarm mail resolves to a system account with an editor's
    standing. `act`, like any editor — but the reason must say nobody is there."""
    system = {"id": 7, "name": "AWS CloudWatch alarms", "workspace": "connect"}
    env = {"version": 2, "relationship": "member", "verified": True, "granted_by": "editor",
           "who": {"kind": "user", "via": "email", "assurance": "dkim_aligned",
                   "user": {"email": "connect.aws@system.canopy.invalid"},
                   "system_account": system},
           "system_account": system}
    got = resolve(env, RULES)
    assert got["tier"] == ACT
    assert got["system_account"] == system
    assert "automated sender" in got["reason"] and "do not reply" in got["reason"]


def test_a_person_carries_no_system_account():
    assert resolve(_env(), RULES)["system_account"] is None


# --- unproven_member (canopy-web#1265): a member whose mail can't be tied to them ----

_UNPROVEN = {"email": "member@example.org", "role": "editor",
             "this_message_grade": "contact", "needs": ["dmarc", "dkim_aligned"],
             "note": "domain lacks aligned DKIM/DMARC"}


def test_an_unproven_member_on_a_capability_grant_is_still_a_caller_and_says_why():
    env = {**_env("member@example.org"), "granted_by": "capability:ask",
           "unproven_member": _UNPROVEN}
    got = resolve(env, [])
    assert got["tier"] == CALLER
    assert got["unproven_member"] == _UNPROVEN
    assert "member of this workspace (editor)" in got["reason"]
    assert "mail authentication" in got["reason"] and "dmarc + dkim_aligned" in got["reason"]
    assert "Tell the owner" in got["reason"] and "do not escalate" in got["reason"]


def test_an_unproven_member_on_the_unlisted_tier_says_why():
    env = {**_env("member@example.org"), "unproven_member": _UNPROVEN}
    got = resolve(env, RULES)
    assert got["tier"] == UNLISTED and "not treat them as an outsider" in got["reason"]


def test_an_unproven_member_never_changes_an_act_tier_or_its_reason():
    env = {**_env(), "granted_by": "full:contact@dimagi.com:verified", "unproven_member": _UNPROVEN}
    got = resolve(env, RULES)
    assert got["tier"] == ACT and "mail authentication" not in got["reason"]
    assert got["unproven_member"] == _UNPROVEN


@pytest.mark.parametrize("value", ["absent", None, {}, "garbage"])
def test_no_unproven_member_is_identical_to_today(value):
    base = {**_env("x@elsewhere.org"), "granted_by": "capability:ask"}
    env = base if value == "absent" else {**base, "unproven_member": value}
    got = resolve(env, RULES)
    assert got == resolve(base, RULES)
    assert "unproven_member" not in got
