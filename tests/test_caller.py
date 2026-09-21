"""`canopy caller tier`: act needs an allowlisted address AND a verified message."""
import json

import pytest
from click.testing import CliRunner

from orchestrator.caller import (ACT, BLOCKED, SYSTEM, UNLISTED, UNVERIFIED, allowlisted,
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
