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


def test_a_read_mode_grant_is_a_confined_caller_even_if_allowlisted():
    env = {**_env("beth@dimagi.com", verified=False), "granted_by": "capability:ask"}
    assert resolve(env, RULES)["tier"] == CALLER


def test_no_interface_falls_back_to_the_allowlist():
    env = {**_env(), "granted_by": "no-interface"}
    assert resolve(env, RULES)["tier"] == ACT
    assert resolve({**env, "verified": False}, RULES)["tier"] == UNVERIFIED


def test_blocked_still_wins_over_a_grant():
    env = {**_env(blocked=True), "granted_by": "full:contact@dimagi.com:verified"}
    assert resolve(env, RULES)["tier"] == BLOCKED


# --- envelope VERSION 2 words -------------------------------

@pytest.mark.parametrize("rel", ["caller", "contact"])
def test_relationship_is_reported_in_todays_word(rel):
    assert resolve({**_env(), "relationship": rel}, RULES)["relationship"] == "contact"


def test_a_legacy_workspace_editor_is_act_and_manual_is_a_separate_axis():
    env = {**_env(kind="user"), "relationship": "member", "granted_by": "editor"}
    got = resolve(env, [])
    assert got["tier"] == ACT and got["manual"] is False
    assert resolve({**env, "turn_mode": {"mode": "manual"}}, [])["manual"] is True
    assert resolve({**env, "manual_only": True}, [])["manual"] is True


def test_a_confined_envelope_is_the_caller_tier_in_either_version():
    for prof in ("confined", "restricted"):
        env = {**_env(), "profile": prof, "granted_by": "capability:ask"}
        assert resolve(env, RULES)["tier"] == CALLER


def test_an_alarm_trigger_is_the_system_tier_and_is_named_as_automated():
    """Alarm mail fires one of the agent's own triggers: no person is asking. The
    reason must say so, name who set it up, and say not to reply."""
    trigger = {"id": 22, "name": "AWS CloudWatch alarms", "kind": "inbound_mail",
               "set_up_by": "ada@dimagi-ai.com"}
    env = {"version": 3, "relationship": "system", "verified": True,
           "who": {"kind": "system", "via": "email", "trigger": trigger},
           "trigger": {"origin": "email", "agent_trigger": trigger}}
    got = resolve(env, RULES)
    assert got["tier"] == SYSTEM
    assert got["agent_trigger"] == trigger
    assert "automated mail" in got["reason"] and "do not reply" in got["reason"]
    assert "ada@dimagi-ai.com" in got["reason"]


def test_a_person_carries_no_agent_trigger():
    assert resolve(_env(), RULES)["agent_trigger"] is None


# --- unproven_member: a member whose mail can't be tied to them ----

_UNPROVEN = {"email": "member@example.org", "role": "editor",
             "this_message_grade": "contact", "needs": ["dmarc", "dkim_aligned"],
             "note": "domain lacks aligned DKIM/DMARC"}


def test_an_unproven_member_in_read_mode_is_still_a_caller_and_says_why():
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


# --- permissions: canopy-web's decision, read in its own vocabulary -------------------

#: The bug: a verified connect-workspace editor, given `write` by an access-list line.
_GRANT_LINE = "grant:workspace:connect::dimagi-associate.com"


def _web(email="nlesh@dimagi-associate.com", **kw):
    return {**_env(email, kind="user"), "relationship": "member", **kw}


def test_write_via_an_access_list_line_is_act_not_unlisted():
    env = _web(role="editor", profile="full", granted_by=_GRANT_LINE,
               turn_mode={"mode": "manual", "basis": "editor: manual"})
    got = resolve(env, [])
    assert got["tier"] == ACT
    assert got["role"] == "editor" and got["manual"] is True and got["granted_by"] == _GRANT_LINE


def test_permissions_with_write_is_act_and_is_reported():
    env = _web(role="editor", permissions=["agent_edit", "read", "write"], granted_by=_GRANT_LINE)
    got = resolve(env, [])
    assert got["tier"] == ACT and got["permissions"] == ["agent_edit", "read", "write"]


def test_read_only_in_read_mode_is_caller():
    env = _web(role="read_only", permissions=["read", "use:ask"], profile="confined",
               granted_by="capability:ask", capability={"name": "ask"})
    got = resolve(env, [])
    assert got["tier"] == CALLER and "read mode" in got["reason"] and got["role"] == "read_only"


def test_a_scoped_use_permission_alone_is_caller_even_without_profile():
    assert resolve(_web(permissions=["read", "use:ask"], granted_by=_GRANT_LINE), [])["tier"] == CALLER


def test_a_host_site_is_named_in_the_reason():
    got = resolve(_web(permissions=["read", "use:connect"], profile="confined",
                       capability={"name": "connect"}), [])
    assert got["tier"] == CALLER and "the host site 'connect'" in got["reason"]


def test_read_without_a_confined_profile_is_unlisted():
    assert resolve(_web(permissions=["read"], profile="confined"), [])["tier"] == CALLER
    assert resolve(_web(permissions=["read"], granted_by=_GRANT_LINE), [])["tier"] == UNLISTED


def test_permissions_beat_granted_by_and_profile():
    # an old word that says act, permissions that say no write
    env = _web(permissions=["read", "use:ask"], granted_by="admin", profile="full",
               capability={"name": "ask"})
    assert resolve(env, [])["tier"] == CALLER
    # an old word that says read mode, permissions that hold write
    assert resolve(_web(permissions=["read", "write"], granted_by="capability:ask"), [])["tier"] == ACT


def test_permissions_never_act_on_a_refused_or_sensitive_basis():
    for g in ("refused", "sensitive:ask"):
        assert resolve(_web(permissions=["write"], profile="full", granted_by=g), [])["tier"] == UNLISTED


def test_access_refused_is_never_an_act_whatever_else_the_envelope_says():
    # canopy-web's Turn Context word. A refused turn's `profile` stays `full` for older runners,
    # and its `granted_by` may describe the whole agent: `access` decides.
    env = _web(access="refused", permissions=["read", "write"], profile="full", granted_by="owner")
    got = resolve(env, [])
    assert got["tier"] == UNLISTED and "access: refused" in got["reason"]
    assert resolve({**_env(), "access": "refused", "granted_by": "no-interface"}, RULES)["tier"] == UNLISTED


def test_access_full_or_confined_changes_nothing():
    assert resolve(_web(access="full", permissions=["write"], profile="full"), [])["tier"] == ACT
    assert resolve(_web(access="confined", permissions=["read", "use:ask"], profile="confined",
                        capability={"name": "ask"}), [])["tier"] == CALLER


def test_the_allowlist_does_not_override_a_refusal():
    env = {**_env(), "granted_by": "refused"}
    assert resolve(env, RULES)["tier"] == UNLISTED


def test_a_profile_alone_decides_when_permissions_are_absent():
    assert resolve(_web(profile="full", granted_by="whatever-new"), [])["tier"] == ACT
    assert resolve(_web(profile="confined", granted_by="whatever-new"), [])["tier"] == CALLER


def test_no_interface_ignores_permissions_and_keeps_the_allowlist():
    env = {**_env(), "granted_by": "no-interface", "profile": "full", "permissions": ["write"]}
    assert resolve(env, RULES)["tier"] == ACT
    assert resolve({**env, "verified": False}, RULES)["tier"] == UNVERIFIED
    assert resolve(env, [])["tier"] == UNLISTED


def test_blocked_and_system_still_win_over_permissions():
    env = _web(permissions=["write"], profile="full")
    assert resolve({**env, "contact": {"is_blocked": True}}, [])["tier"] == BLOCKED
    assert resolve({**env, "who": {"kind": "system", "via": "schedule"}}, [])["tier"] == SYSTEM


# Every `granted_by` canopy-web emits, with NO `permissions` and NO `profile` (an older canopy-web).
# A new value must be named here: it cannot silently become `unlisted`.
_LEGACY_ONLY = [
    ("owner", {}, ACT),
    ("admin", {}, ACT),
    ("system", {}, UNLISTED),  # kind=system is caught earlier; as a bare word it grants nothing
    ("editor", {}, ACT),
    ("full:contact@dimagi.com:verified", {}, ACT),
    ("session:owner", {}, ACT),
    ("session:admin", {}, ACT),
    ("session:editor", {}, ACT),
    ("session:read_write", {}, ACT),
    ("session:read_only", {}, UNLISTED),
    (_GRANT_LINE, {"role": "editor"}, ACT),
    (_GRANT_LINE, {"role": "read_write"}, ACT),
    (_GRANT_LINE, {"role": "read_only"}, UNLISTED),
    (_GRANT_LINE, {}, UNLISTED),
    ("grant:agent:ask::beth@x.org", {"role": "read_only", "capability": {"name": "ask"}}, CALLER),
    ("capability:ask", {}, CALLER),
    ("sensitive:ask", {}, UNLISTED),
    ("refused", {}, UNLISTED),
    ("no-interface", {}, UNLISTED),  # no allowlist match: the address below is not listed
    ("brand-new-word", {}, UNLISTED),
]


@pytest.mark.parametrize("granted_by,extra,tier", _LEGACY_ONLY, ids=[g for g, _, _ in _LEGACY_ONLY])
def test_every_granted_by_value_has_a_named_tier(granted_by, extra, tier):
    env = {**_web("x@elsewhere.org"), "granted_by": granted_by, **extra}
    assert resolve(env, [])["tier"] == tier


def test_legacy_only_envelopes_report_no_permissions_key():
    got = resolve({**_web(), "granted_by": "owner"}, [])
    assert "permissions" not in got and got["role"] is None and got["manual"] is False
