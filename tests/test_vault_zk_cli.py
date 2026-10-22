"""Zero-knowledge agents end to end on the client (canopy-web#1550): the resolve client
opens a ZK answer with this device's ECDH key, and `canopy vault zk enable|set|grant|rotate`
act as this device against canopy-web's endpoints.

canopy-web is an in-memory fake at the transport boundary that keeps ONLY what the real
server may keep — wraps, `zk1:` values, public keys — and every assertion about access is
made with real crypto: a principal "has access" iff its own private key unwraps the vault
key and that key decrypts the values. No value, vault key or recovery code is printed.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric import ec

from orchestrator import canopy_web, cred_resolve, device_key, vault_cli, vault_zk_cli
from orchestrator import vault_zk as zk

SECRET = "kindora_SECRET_never_printed"
AGENT_ID = 42
ME = 5                  # this machine's device key id on canopy-web


def _new_key():
    k = ec.generate_private_key(ec.SECP256R1())
    return k, zk.jwk(k.public_key())


USER_KEY, USER_JWK = _new_key()            # the owner's browser keyset (user 7)
DEV9_KEY, DEV9_JWK = _new_key()            # another runner's approved device
DEV11_KEY, DEV11_JWK = _new_key()


class FakeZkServer:
    def __init__(self, values, *, keyset=True, pending=(), send_agent_id=False):
        self.custodial = dict(values)
        self.enabled, self.kv = False, 0
        self.wraps: list[dict] = []
        self.zk_values: dict = {}
        self.keys: dict = {}
        self.labels: dict = {}
        self.pending_targets = list(pending)
        self.keyset = keyset
        self.send_agent_id = send_agent_id
        self.calls: list = []
        self.inactive: set = set()

    # what canopy-web knows about a target: its PUBLIC key
    def register(self, kind, target, jwk, label=""):
        self.keys[(kind, str(target))] = jwk
        self.labels[(kind, str(target))] = label

    def _row(self, w):
        kind, target = w["kind"], w["target"]
        row = {"kind": kind, "target": target, "kv": w["kv"], "wrap": w["wrap"]}
        if kind == "device":
            row["ecdh_public_jwk"] = self.keys[(kind, str(target))]
            row["ecdh_jkt"] = zk.jwk_thumbprint(row["ecdh_public_jwk"])
        elif kind == "member":
            row["public_jwk"] = self.keys[(kind, str(target))]
        if (kind, str(target)) in self.inactive:
            row["active"] = False
        return row

    def state(self):
        have = {(w["kind"], str(w["target"])) for w in self.wraps if w["kv"] == self.kv}
        pending = []
        for kind, target in self.pending_targets:
            if (kind, str(target)) in have or (kind, str(target)) in self.inactive:
                continue
            jwk = self.keys[(kind, str(target))]
            row = {"kind": kind, "target": target, "label": self.labels[(kind, str(target))]}
            row["ecdh_public_jwk" if kind == "device" else "public_jwk"] = jwk
            pending.append(row)
        return {"enabled": self.enabled, "kv": self.kv,
                "wraps": [self._row(w) for w in self.wraps], "pending": pending}

    def __call__(self, method, url, headers, data):
        parts = urlsplit(url)
        path = parts.path
        body = json.loads(data) if data else None
        self.calls.append((method, path, body, dict(headers)))
        base = "/api/agents/eva"
        if (method, path) == ("GET", f"{base}/"):
            return 200, json.dumps({"id": AGENT_ID, "slug": "eva", "is_admin": True})
        if (method, path) == ("GET", "/api/vault/keyset"):
            if not self.keyset:
                return 404, '{"detail": "no keyset"}'
            return 200, json.dumps({"user_id": 7, "public_jwk": USER_JWK, "seals": []})
        if (method, path) == ("GET", f"{base}/vault/zk"):
            return 200, json.dumps(self.state())
        if (method, path) == ("POST", f"{base}/vault/zk/enable"):
            kinds = [w["kind"] for w in body["wraps"]]
            assert kinds.count("recovery") == 1 and body["kv"] == 1
            self.enabled, self.kv = True, 1
            self.wraps = [dict(w, kv=1) for w in body["wraps"] if w["kind"] != "recovery"]
            self.recovery = [w for w in body["wraps"] if w["kind"] == "recovery"][0]["wrap"]
            self.zk_values = {v["name"]: v["value_zk"] for v in body["values"]}
            for v in body["values"]:
                self.custodial.pop(v["name"], None)          # value_enc wiped
            return 200, json.dumps(self.state())
        if (method, path) == ("PUT", f"{base}/vault/zk/values"):
            if body["kv"] != self.kv:
                return 409, '{"error": "kv_mismatch"}'
            self.zk_values.update({v["name"]: v["value_zk"] for v in body["values"]})
            return 200, "{}"
        if (method, path) == ("POST", f"{base}/vault/zk/wraps"):
            assert body["kv"] == self.kv
            self.wraps += [dict(w, kv=self.kv) for w in body["wraps"]]
            return 200, "{}"
        if (method, path) == ("POST", f"{base}/vault/zk/rotate"):
            assert body["new_kv"] == self.kv + 1
            assert [w["kind"] for w in body["wraps"]].count("recovery") == 1
            self.kv = body["new_kv"]
            self.wraps = [dict(w, kv=self.kv) for w in body["wraps"] if w["kind"] != "recovery"]
            self.recovery = [w for w in body["wraps"] if w["kind"] == "recovery"][0]["wrap"]
            self.zk_values = {v["name"]: v["value_zk"] for v in body["values"]}
            return 200, "{}"
        if (method, path) == ("GET", f"{base}/credentials/resolve"):
            if not self.enabled:
                return 200, json.dumps({"values": self.custodial, "op_vault": ""})
            if "DPoP" not in headers:
                return 401, '{"error": "device_proof_required"}'
            if self.kv == 0:                         # born zero-knowledge, holding nothing
                return 200, json.dumps({"values": {}, "op_vault": ""})
            mine = [w for w in self.wraps if w["kind"] == "device"
                    and str(w["target"]) == str(ME) and w["kv"] == self.kv]
            if not mine:
                return 409, '{"error": "zk_wrap_missing"}'
            out = {"zk": True, "kv": self.kv, "values": self.zk_values, "wrap": mine[0]["wrap"]}
            if self.send_agent_id:
                out["agent_id"] = str(AGENT_ID)
            return 200, json.dumps(out)
        return 404, "{}"

    # -- access, decided by crypto alone --
    def vk_for(self, kind, target, priv):
        w = [w for w in self.wraps if w["kind"] == kind and str(w["target"]) == str(target)
             and w["kv"] == self.kv][0]["wrap"]
        info = (zk.device_info(AGENT_ID, self.kv, zk.jkt(priv.public_key())) if kind == "device"
                else zk.member_info(AGENT_ID, self.kv, target))
        return zk.unwrap_ecdh_es(w, priv, info)

    def plaintexts(self, vk):
        return {n: zk.decrypt_value(vk, AGENT_ID, n, self.kv, v) for n, v in self.zk_values.items()}


@pytest.fixture
def me():
    """This machine: a registered, active signing key (for DPoP) and its ECDH key."""
    signing, _ = device_key.ensure(allow_software=True, system=lambda: "Linux")
    ecdh, _ = device_key.ensure_ecdh("software")
    device_key.write_status({"id": ME, "jkt": signing.jkt, "status": "active",
                             "kind": "software", "ecdh_jkt": ecdh.jkt})
    return ecdh


@pytest.fixture
def server(me, monkeypatch):
    s = FakeZkServer({"KINDORA_SERVICE_KEY": SECRET, "CHANNEL": "eva@example.com"},
                     pending=[("device", 9)])
    s.register("device", ME, me.public_jwk, "this laptop")
    s.register("member", 7, USER_JWK, "jon")
    s.register("device", 9, DEV9_JWK, "cloud-ec2-1")
    s.register("device", 11, DEV11_JWK, "cloud-ec2-2")
    monkeypatch.setattr(canopy_web, "urllib_transport", s)
    monkeypatch.setattr(vault_cli, "_token", lambda: "person-pat")
    return s


def _vault(server):
    return vault_zk_cli.ZkVault("eva", token="person-pat", transport=server)


def _sent(server) -> str:
    return json.dumps([c[2] for c in server.calls])


def _enable(server):
    return vault_zk_cli.run_enable(_vault(server), confirm=lambda targets: True)


# ── enable ────────────────────────────────────────────────────────────────────

def test_enable_encrypts_here_and_wraps_for_every_member(server, me):
    res = _enable(server)
    assert server.enabled and server.kv == 1
    assert SECRET not in _sent(server)                       # canopy-web never saw it
    assert all(v.startswith("zk1:") for v in server.zk_values.values())
    assert server.custodial == {}
    targets = {(w["kind"], str(w["target"])) for w in server.wraps}
    assert targets == {("device", str(ME)), ("member", "7"), ("device", "9")}
    vk = server.vk_for("device", ME, _priv_of(me))
    assert server.vk_for("member", 7, USER_KEY) == vk == server.vk_for("device", 9, DEV9_KEY)
    assert server.plaintexts(vk)["KINDORA_SERVICE_KEY"] == SECRET
    r = zk.parse_recovery_code(res.recovery_code)
    assert zk.unwrap_recovery(server.recovery, AGENT_ID, 1, r) == vk
    assert res.recovery_code not in _sent(server) and res.recovery_code not in repr(res)


def _priv_of(ecdh_key):
    """The test's software ECDH key, read back from its 0600 PEM (hardware keys would
    never allow this — tests only)."""
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(device_key.ecdh_pem_path().read_bytes(), None)


def test_enable_without_a_keyset_still_wraps_for_the_device(server):
    server.keyset = False
    res = _enable(server)
    assert {w["kind"] for w in server.wraps} == {"device"}
    assert any("keyset" in n for n in res.notes)


def test_enable_refuses_twice_and_does_nothing_unconfirmed(server):
    with pytest.raises(vault_zk_cli.ZkCliError, match="not confirmed"):
        vault_zk_cli.run_enable(_vault(server), confirm=lambda t: False)
    assert not server.enabled
    assert not [c for c in server.calls if c[0] == "POST"]
    _enable(server)
    with pytest.raises(vault_zk_cli.ZkCliError, match="already zero-knowledge"):
        _enable(server)


def test_enable_creates_the_first_key_of_an_agent_born_zero_knowledge(server):
    server.enabled, server.kv = True, 0          # born zero-knowledge: no vault key yet
    _enable(server)
    assert server.enabled and server.kv == 1
    with pytest.raises(vault_zk_cli.ZkCliError, match="already zero-knowledge"):
        _enable(server)


# ── resolve: a ZK answer opens here, exactly like custodial values ───────────

def test_resolve_opens_a_zk_answer_with_this_devices_key(server):
    _enable(server)
    body = cred_resolve.resolve("eva", token="pat", turn_id="")
    assert body["values"] == {"KINDORA_SERVICE_KEY": SECRET, "CHANNEL": "eva@example.com"}
    assert body["zk"] is True and "wrap" not in body
    assert cred_resolve.resolve("eva", token="pat", refs=["CHANNEL"], turn_id="")["values"] \
        == {"CHANNEL": "eva@example.com"}


def test_resolve_takes_agent_id_from_the_answer_when_sent(server):
    _enable(server)
    server.send_agent_id = True
    before = len(server.calls)
    assert cred_resolve.resolve("eva", token="pat", turn_id="")["values"]["KINDORA_SERVICE_KEY"] \
        == SECRET
    assert not [c for c in server.calls[before:] if c[1] == "/api/agents/eva/"]


def test_the_cred_path_writes_a_zk_agents_env_like_any_other(server, tmp_path, monkeypatch):
    from orchestrator import cred_cli
    _enable(server)
    monkeypatch.setattr(cred_cli, "_human_token", lambda: "pat")
    out = tmp_path / ".env"
    note = cred_cli.write_env_from_values("eva", out, cred_cli.fetch_canopy_web_values("eva"))
    assert note[0] == 2 and f"KINDORA_SERVICE_KEY={SECRET}" in out.read_text()


def test_a_device_without_a_wrap_is_told_to_ask_for_a_grant(server):
    _enable(server)
    server.wraps = [w for w in server.wraps if str(w["target"]) != str(ME)]
    with pytest.raises(cred_resolve.ZkWrapMissing, match="grant this device"):
        cred_resolve.resolve("eva", token="pat", turn_id="")


def test_a_tampered_value_or_foreign_wrap_never_yields_plaintext(server):
    _enable(server)
    server.zk_values["CHANNEL"] = server.zk_values["KINDORA_SERVICE_KEY"]   # swapped names
    with pytest.raises(cred_resolve.ZkUnlockError, match="CHANNEL"):
        cred_resolve.resolve("eva", token="pat", turn_id="")
    other = [w for w in server.wraps if str(w["target"]) == "9"][0]
    mine = [w for w in server.wraps if str(w["target"]) == str(ME)][0]
    mine["wrap"] = other["wrap"]                                           # not ours
    with pytest.raises(cred_resolve.ZkUnlockError, match="could not unwrap"):
        cred_resolve.resolve("eva", token="pat", turn_id="")


def test_a_machine_without_an_ecdh_key_says_how_to_get_one(server):
    _enable(server)
    device_key.ecdh_pem_path().unlink()
    with pytest.raises(cred_resolve.ZkUnlockError, match="enroll"):
        cred_resolve.resolve("eva", token="pat", turn_id="")


# ── set / grant / rotate ──────────────────────────────────────────────────────

def test_set_stores_ciphertext_at_the_current_kv(server):
    _enable(server)
    vault_zk_cli.run_set(_vault(server), {"NEW_KEY": "new_SECRET"})
    assert "new_SECRET" not in _sent(server)
    vk = server.vk_for("member", 7, USER_KEY)
    assert server.plaintexts(vk)["NEW_KEY"] == "new_SECRET"


def test_grant_wraps_for_pending_targets_only(server):
    _enable(server)
    server.pending_targets.append(("device", 11))
    res = vault_zk_cli.run_grant(_vault(server), confirm=lambda t: [x.target for x in t] == [11])
    assert len(res.wrapped) == 1
    assert server.vk_for("device", 11, DEV11_KEY) == server.vk_for("member", 7, USER_KEY)
    again = vault_zk_cli.run_grant(_vault(server), confirm=lambda t: True)
    assert "nothing pending" in again.actions[0]


def test_grant_refuses_a_target_whose_thumbprint_does_not_match(server):
    _enable(server)
    row = {"kind": "device", "target": 11, "ecdh_public_jwk": DEV11_JWK, "ecdh_jkt": "forged"}
    t, why = vault_zk_cli.target_from_row(row)
    assert t is None and "not wrapping" in why
    t, why = vault_zk_cli.target_from_row({"kind": "member", "target": 8})
    assert t is None and "no public key" in why


def test_a_non_member_device_cannot_grant(server):
    _enable(server)
    server.wraps = [w for w in server.wraps if str(w["target"]) != str(ME)]
    server.pending_targets.append(("device", 11))
    with pytest.raises(vault_zk_cli.ZkCliError, match="not a member"):
        vault_zk_cli.run_grant(_vault(server), confirm=lambda t: True)


def test_rotate_removes_a_dropped_device_and_re_encrypts(server):
    first = _enable(server)
    old_vk = server.vk_for("member", 7, USER_KEY)
    res = vault_zk_cli.run_rotate(_vault(server), drop={("device", "9")},
                                  confirm=lambda t: True)
    assert server.kv == 2
    assert {(w["kind"], str(w["target"])) for w in server.wraps} == {("device", str(ME)),
                                                                     ("member", "7")}
    vk = server.vk_for("member", 7, USER_KEY)
    assert vk != old_vk and server.plaintexts(vk)["KINDORA_SERVICE_KEY"] == SECRET
    with pytest.raises(zk.ZkError):                    # the old key opens nothing now
        zk.decrypt_value(old_vk, AGENT_ID, "KINDORA_SERVICE_KEY", 2,
                         server.zk_values["KINDORA_SERVICE_KEY"])
    assert res.recovery_code != first.recovery_code
    assert zk.unwrap_recovery(server.recovery, AGENT_ID, 2,
                              zk.parse_recovery_code(res.recovery_code)) == vk
    assert SECRET not in _sent(server)
    assert cred_resolve.resolve("eva", token="pat", turn_id="")["values"]["KINDORA_SERVICE_KEY"] \
        == SECRET


def test_rotate_leaves_out_what_canopy_web_marks_inactive(server):
    _enable(server)
    server.inactive.add(("device", "9"))
    res = vault_zk_cli.run_rotate(_vault(server), confirm=lambda t: True)
    assert ("device", "9") not in {(w["kind"], str(w["target"])) for w in server.wraps}
    assert any("dropped" in n for n in res.notes)


# ── the CLI ───────────────────────────────────────────────────────────────────

def test_cli_enable_shows_the_recovery_code_exactly_once(server):
    r = CliRunner().invoke(vault_cli.vault_group, ["zk", "enable", "--agent", "eva", "--yes"])
    assert r.exit_code == 0, r.output
    code = [ln.strip() for ln in r.output.splitlines() if ln.strip().count("-") == 12]
    assert len(code) == 1 and r.output.count(code[0]) == 1
    assert SECRET not in r.output
    st = CliRunner().invoke(vault_cli.vault_group, ["zk", "status", "--agent", "eva", "--json"])
    data = json.loads(st.output)
    assert data["enabled"] and data["kv"] == 1 and "holds a wrap" in data["this_device"]
    assert code[0] not in st.output


def test_cli_enable_can_write_the_recovery_code_to_a_new_file_only(server, tmp_path):
    out = tmp_path / "recovery.txt"
    r = CliRunner().invoke(vault_cli.vault_group, ["zk", "enable", "--agent", "eva", "--yes",
                                                   "--recovery-file", str(out)])
    assert r.exit_code == 0, r.output
    code = out.read_text().strip()
    assert code.count("-") == 12 and code not in r.output and str(out) in r.output
    assert out.stat().st_mode & 0o777 == 0o600


def test_cli_refuses_an_existing_recovery_file_before_touching_the_vault(server, tmp_path):
    out = tmp_path / "recovery.txt"
    out.write_text("keep me\n")
    r = CliRunner().invoke(vault_cli.vault_group, ["zk", "enable", "--agent", "eva", "--yes",
                                                   "--recovery-file", str(out)])
    assert r.exit_code != 0 and "already exists" in r.output
    assert not server.enabled and out.read_text() == "keep me\n"


def test_cli_set_reads_the_value_from_stdin_never_argv(server):
    CliRunner().invoke(vault_cli.vault_group, ["zk", "enable", "--agent", "eva", "--yes"])
    r = CliRunner().invoke(vault_cli.vault_group, ["zk", "set", "--agent", "eva", "TOKEN"],
                           input="stdin_SECRET\n")
    assert r.exit_code == 0, r.output
    assert "stdin_SECRET" not in r.output
    vk = server.vk_for("member", 7, USER_KEY)
    assert server.plaintexts(vk)["TOKEN"] == "stdin_SECRET"


def test_cli_device_ecdh_jwk_prints_only_the_public_half(me):
    r = CliRunner().invoke(vault_cli.vault_group, ["device", "--ecdh-jwk"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["jwk"] == me.public_jwk and "d" not in out["jwk"] and out["jkt"] == me.jkt


# ── sealed values (zks1): what canopy-web mints for a ZK agent ────────────────

GMAIL = '{"email":"eva@example.com","client":"canopy","refresh_token":"rt_SECRET_sealed"}'


def _seal_for(server, name, plaintext, recipients):
    """What canopy-web stores when it mints a value for a ZK agent: sealed to PUBLIC keys
    (device targets by ECDH jkt), no vault key involved."""
    return zk.seal_value(AGENT_ID, name, server.kv, plaintext,
                         [(kind, target, jwk) for kind, target, jwk in recipients])


def test_a_sealed_value_opens_only_for_its_recipients():
    a, a_jwk = _new_key()
    b, b_jwk = _new_key()
    stored = zk.seal_value(AGENT_ID, "gog-token", 1, GMAIL,
                           [("device", zk.jkt(a.public_key()), a_jwk), ("member", 7, b_jwk)])
    assert stored.startswith("zks1:") and "rt_SECRET" not in stored
    assert zk.open_sealed(stored, AGENT_ID, "gog-token", "device", zk.jkt(a.public_key()), a) \
        == GMAIL
    assert zk.open_sealed(stored, AGENT_ID, "gog-token", "member", 7, b) == GMAIL
    with pytest.raises(zk.ZkError, match="none of them"):                    # not a recipient
        zk.open_sealed(stored, AGENT_ID, "gog-token", "device", zk.jkt(DEV9_KEY.public_key()),
                       DEV9_KEY)
    with pytest.raises(zk.ZkError, match="did not authenticate"):            # bound to name
        zk.open_sealed(stored, AGENT_ID, "other", "member", 7, b)
    with pytest.raises(zk.ZkError, match="did not authenticate"):            # key for b, not a
        entry = zk.parse_sealed(stored)["r"]
        forged = dict(zk.parse_sealed(stored), r=[dict(entry[1], kind="device",
                                                       target=zk.jkt(a.public_key()))])
        zk.open_sealed("zks1:" + zk.b64u(json.dumps(forged).encode()), AGENT_ID, "gog-token",
                       "device", zk.jkt(a.public_key()), a)


def test_resolve_opens_a_sealed_gmail_token_in_a_zk_answer(server, me, monkeypatch):
    from orchestrator import agent_bootstrap
    _enable(server)
    server.zk_values["gog-token"] = _seal_for(server, "gog-token", GMAIL,
                                              [("device", me.jkt, me.public_jwk)])
    body = cred_resolve.resolve("eva", token="pat", turn_id="")
    assert body["values"]["gog-token"] == GMAIL
    assert body["values"]["KINDORA_SERVICE_KEY"] == SECRET
    monkeypatch.setattr(agent_bootstrap, "_operator_token", lambda: "pat")
    assert agent_bootstrap.fetch_web_token("eva") == (GMAIL, "")


def test_the_bootstrap_reads_a_zk1_gmail_token_as_plaintext(server, monkeypatch):
    """`import --zk` stores the Gmail token as zk1; the runner's token step must get the
    JSON, not the ciphertext."""
    from orchestrator import agent_bootstrap
    server.custodial["gog-token"] = GMAIL
    _enable(server)
    assert server.zk_values["gog-token"].startswith("zk1:")
    monkeypatch.setattr(agent_bootstrap, "_operator_token", lambda: "pat")
    assert agent_bootstrap.fetch_web_token("eva") == (GMAIL, "")


def test_a_custodial_answer_carrying_a_sealed_value_opens_it(server, me):
    server.custodial["gog-token"] = zk.seal_value(AGENT_ID, "gog-token", 1, GMAIL,
                                                  [("device", me.jkt, me.public_jwk)])
    body = cred_resolve.resolve("eva", token="pat", turn_id="")
    assert body["values"]["gog-token"] == GMAIL and body["values"]["CHANNEL"] == "eva@example.com"


def test_raw_resolve_hands_back_the_ciphertext(server):
    _enable(server)
    body = cred_resolve.resolve("eva", token="pat", turn_id="", raw=True)
    assert body["values"]["KINDORA_SERVICE_KEY"].startswith("zk1:") and "wrap" in body


def test_settle_re_encrypts_sealed_values_under_the_vault_key(server, me):
    _enable(server)
    server.zk_values["gog-token"] = _seal_for(server, "gog-token", GMAIL,
                                              [("device", me.jkt, me.public_jwk)])
    res = vault_zk_cli.run_settle(_vault(server))
    assert server.zk_values["gog-token"].startswith("zk1:")
    assert "rt_SECRET" not in _sent(server)
    # now every member reads it — the owner's keyset never was a seal recipient
    assert server.plaintexts(server.vk_for("member", 7, USER_KEY))["gog-token"] == GMAIL
    assert any("settled 1" in a for a in res.actions)
    assert vault_zk_cli.run_settle(_vault(server)).actions == [
        "nothing to settle — no sealed (zks1) values"]


def test_settle_leaves_a_value_sealed_to_other_devices_alone(server):
    _enable(server)
    sealed = _seal_for(server, "gog-token", GMAIL,
                       [("device", zk.jkt(DEV9_KEY.public_key()), DEV9_JWK)])
    server.zk_values["gog-token"] = sealed
    res = vault_zk_cli.run_settle(_vault(server))
    assert server.zk_values["gog-token"] == sealed
    assert any("gog-token: left sealed" in n for n in res.notes)
    assert not [c for c in server.calls if c[0] == "PUT"]


def test_switch_settles_on_the_way(server, me, monkeypatch):
    _enable(server)
    server.zk_values["gog-token"] = _seal_for(server, "gog-token", GMAIL,
                                              [("device", me.jkt, me.public_jwk)])
    notes = vault_zk_cli.settle_quietly("eva", token="person-pat", transport=server)
    assert any("settled 1" in n for n in notes)
    assert server.zk_values["gog-token"].startswith("zk1:")
    server.enabled = False
    assert vault_zk_cli.settle_quietly("eva", token="person-pat", transport=server) == []


# ── set --sealed: give an agent a secret, sealed to ENDORSED readers (zk-v1 §6 + §7) ──
# canopy-web#1712's `GET …/vault/zk/recipients` is the server's CLAIM; the endorsements
# are a pinned person's signatures. These tests make the server lie in every way it can
# and check the value only ever reaches keys a trusted person vouched for.

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402
from urllib.parse import parse_qs  # noqa: E402

REQ_ID = "0b7c2a7e-1d1e-4b6a-9a51-6f0f3c1d2e3f"
SEALED_SECRET = "ghp_SEALED_never_printed"
JON_KEY, JON_JWK = USER_KEY, USER_JWK          # the admin's person key (user 7)
MALLORY_KEY, MALLORY_JWK = _new_key()          # a key the server would like to slip in


def _endorse(signer, agent_id, kind, target, key_jkt, by="7", email="jon@dimagi.com"):
    msg = zk.endorse_msg(agent_id, kind, target, key_jkt).encode()
    r, s = decode_dss_signature(signer.sign(msg, ec.ECDSA(hashes.SHA256())))
    return {"kind": kind, "target": target, "key_jkt": key_jkt, "by": by, "by_email": email,
            "by_jkt": zk.jkt(signer.public_key()), "by_public_jwk": zk.jwk(signer.public_key()),
            "sig": zk.b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}


def _recipient(kind, target, jwk, label=""):
    return {"kind": kind, "target": str(target), "label": label, "public_jwk": jwk,
            "key_jkt": zk.jwk_thumbprint(jwk)}


class FakeSealServer:
    """Only what canopy-web#1712 serves: public keys, endorsements, ciphertext."""

    def __init__(self, recipients, endorsements, *, request_name="GITHUB_TOKEN"):
        self.recipients, self.endorsements = recipients, endorsements
        self.request_name = request_name
        self.calls: list = []
        self.stored: dict = {}
        self.fulfilled = None

    def __call__(self, method, url, headers, data):
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)
        body = json.loads(data) if data else None
        self.calls.append((method, path, body, query))
        base = "/api/agents/bailey"
        if (method, path) == ("GET", f"{base}/vault/zk/recipients"):
            return 200, json.dumps({"agent": "bailey", "agent_id": str(AGENT_ID), "kv": 0,
                                    "recipients": self.recipients,
                                    "endorsements": self.endorsements,
                                    "declared": ["GITHUB_TOKEN"], "stored": []})
        if (method, path) == ("GET", f"{base}/secret-requests/{REQ_ID}"):
            return 200, json.dumps({"id": REQ_ID, "agent": "bailey", "name": self.request_name,
                                    "status": "open", "has_value": False})
        if (method, path) == ("PUT", f"{base}/vault/zk/values"):
            assert body["kv"] == 0
            self.stored.update({v["name"]: v["value_zk"] for v in body["values"]})
            return 200, "{}"
        if (method, path) == ("PUT", f"{base}/secret-requests/{REQ_ID}/value"):
            assert set(body) == {"kv", "value_zk"} and zk.is_sealed_value(body["value_zk"])
            self.fulfilled = body
            self.stored[self.request_name] = body["value_zk"]
            return 200, json.dumps({"id": REQ_ID, "status": "fulfilled"})
        return 404, "{}"

    def puts(self):
        return [c for c in self.calls if c[0] == "PUT"]


def _targets(sealed: str) -> set:
    return {(e["kind"], e["target"]) for e in zk.parse_sealed(sealed)["r"]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _pin_jon(home):
    (home / ".canopy").mkdir(exist_ok=True)
    (home / ".canopy" / "vault-pins.json").write_text(json.dumps(
        {"7": {"jkt": zk.jkt(JON_KEY.public_key()), "email": "jon@dimagi.com",
               "pinned_at": "2026-10-19T00:00:00+00:00"}}))


def _pins(home):
    p = home / ".canopy" / "vault-pins.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _seal_server(monkeypatch, recipients, endorsements, **kw):
    s = FakeSealServer(recipients, endorsements, **kw)
    monkeypatch.setattr(canopy_web, "urllib_transport", s)
    monkeypatch.setattr(vault_cli, "_token", lambda: "person-pat")
    return s


def _sealed_cli(args, **kw):
    return CliRunner().invoke(vault_cli.vault_group,
                              ["zk", "set", "--agent", "bailey", "--sealed", *args], **kw)


D9, D11 = zk.jwk_thumbprint(DEV9_JWK), zk.jwk_thumbprint(DEV11_JWK)


def test_seals_only_to_recipients_a_pinned_person_endorsed(home, monkeypatch):
    _pin_jon(home)
    mallory_jkt = zk.jwk_thumbprint(MALLORY_JWK)
    recipients = [_recipient("device", D9, DEV9_JWK, "cloud-ec2-1"),
                  _recipient("device", D11, DEV11_JWK, "cloud-ec2-2"),
                  _recipient("member", 7, JON_JWK, "jon@dimagi.com"),
                  _recipient("device", mallory_jkt, MALLORY_JWK, "totally a runner")]
    forged = dict(_endorse(JON_KEY, AGENT_ID, "device", D11, D11), sig=_endorse(
        JON_KEY, AGENT_ID, "device", D9, D9)["sig"])             # a real sig, wrong message
    endorsements = [_endorse(JON_KEY, AGENT_ID, "device", D9, D9),
                    _endorse(JON_KEY, AGENT_ID, "member", "7", zk.jwk_thumbprint(JON_JWK)),
                    forged,
                    # mallory vouches for herself as an unpinned "person" — declined below
                    _endorse(MALLORY_KEY, AGENT_ID, "device", mallory_jkt, mallory_jkt,
                             by="66", email="mallory@evil.test")]
    s = _seal_server(monkeypatch, recipients, endorsements)
    monkeypatch.setenv("TOKEN_SRC", SEALED_SECRET)
    r = _sealed_cli(["GITHUB_TOKEN", "--from-env", "TOKEN_SRC"], input="n\n")
    assert r.exit_code == 0, r.output
    assert "GITHUB_TOKEN stored for bailey (sealed to 2 readers)" in r.output
    assert SEALED_SECRET not in r.output and SEALED_SECRET not in json.dumps(
        [c[2] for c in s.calls])
    sealed = s.stored["GITHUB_TOKEN"]
    assert _targets(sealed) == {("device", D9), ("member", "7")}
    assert zk.open_sealed(sealed, AGENT_ID, "GITHUB_TOKEN", "device", D9,
                          DEV9_KEY) == SEALED_SECRET
    assert zk.open_sealed(sealed, AGENT_ID, "GITHUB_TOKEN", "member", "7",
                          JON_KEY) == SEALED_SECRET
    with pytest.raises(zk.ZkError):
        zk.open_sealed(sealed, AGENT_ID, "GITHUB_TOKEN", "device", D11, DEV11_KEY)
    assert "cloud-ec2-2" in r.output and "not confirmed" in r.output    # skipped, listed
    assert "66" not in _pins(home)


def test_a_server_swapped_key_is_not_sealed_to(home, monkeypatch):
    """The endorsement is for dev 9's real key; the server serves Mallory's key under
    dev 9's name (with its own thumbprint claim, or a lying one)."""
    _pin_jon(home)
    swapped = dict(_recipient("device", D9, MALLORY_JWK), key_jkt=D9)        # lying claim
    honest_claim = _recipient("device", D9, MALLORY_JWK)                      # true claim
    for row in (swapped, honest_claim):
        s = _seal_server(monkeypatch, [row, _recipient("member", 7, JON_JWK)],
                         [_endorse(JON_KEY, AGENT_ID, "device", D9, D9),
                          _endorse(JON_KEY, AGENT_ID, "member", "7", zk.jwk_thumbprint(JON_JWK))])
        plan, line = vault_zk_cli.run_set_sealed(
            vault_zk_cli.ZkVault("bailey", token="person-pat", transport=s), "GITHUB_TOKEN",
            lambda: SEALED_SECRET)
        assert _targets(s.stored["GITHUB_TOKEN"]) == {("member", "7")}
        assert line == "GITHUB_TOKEN stored for bailey (sealed to 1 reader)"


def test_an_unpinned_endorser_is_shown_then_pinned_on_yes(home, monkeypatch):
    s = _seal_server(monkeypatch, [_recipient("device", D9, DEV9_JWK, "cloud-ec2-1")],
                     [_endorse(JON_KEY, AGENT_ID, "device", D9, D9)])
    monkeypatch.setenv("TOKEN_SRC", SEALED_SECRET)
    r = _sealed_cli(["GITHUB_TOKEN", "--from-env", "TOKEN_SRC"], input="y\n")
    assert r.exit_code == 0, r.output
    jkt = zk.jkt(JON_KEY.public_key())
    assert "jon@dimagi.com" in r.output and vault_zk_cli.fingerprint(jkt) in r.output
    assert "cloud-ec2-1" in r.output and "First contact" in r.output
    pins = _pins(home)
    assert pins["7"]["jkt"] == jkt and pins["7"]["email"] == "jon@dimagi.com"
    assert _targets(s.stored["GITHUB_TOKEN"]) == {("device", D9)}
    # the second time, jon is pinned: no question asked
    r2 = _sealed_cli(["GITHUB_TOKEN", "--from-env", "TOKEN_SRC"])
    assert r2.exit_code == 0 and "First contact" not in r2.output


def test_an_unpinned_endorser_declined_seals_nothing(home, monkeypatch):
    s = _seal_server(monkeypatch, [_recipient("device", D9, DEV9_JWK)],
                     [_endorse(JON_KEY, AGENT_ID, "device", D9, D9)])
    monkeypatch.setenv("TOKEN_SRC", SEALED_SECRET)
    r = _sealed_cli(["GITHUB_TOKEN", "--from-env", "TOKEN_SRC"], input="n\n")
    assert r.exit_code != 0 and "no recipient is endorsed" in r.output
    assert not s.puts() and _pins(home) == {}


def test_yes_pins_without_asking_and_piped_stdin_never_answers_the_question(home, monkeypatch):
    s = _seal_server(monkeypatch, [_recipient("device", D9, DEV9_JWK)],
                     [_endorse(JON_KEY, AGENT_ID, "device", D9, D9)])
    # the value on stdin must not be consumed as the confirmation
    r = _sealed_cli(["GITHUB_TOKEN"], input=SEALED_SECRET + "\n")
    assert r.exit_code != 0 and "needs a confirmation" in r.output
    assert SEALED_SECRET not in r.output and not s.puts()
    r = _sealed_cli(["GITHUB_TOKEN", "--yes"], input=SEALED_SECRET + "\n")
    assert r.exit_code == 0, r.output
    assert SEALED_SECRET not in r.output
    assert zk.open_sealed(s.stored["GITHUB_TOKEN"], AGENT_ID, "GITHUB_TOKEN", "device", D9,
                          DEV9_KEY) == SEALED_SECRET
    assert "7" in _pins(home)


def test_a_pinned_persons_changed_key_refuses(home, monkeypatch):
    _pin_jon(home)
    before = _pins(home)
    # the server now says "user 7" signs with Mallory's key — a valid signature, wrong key
    s = _seal_server(monkeypatch, [_recipient("device", D9, DEV9_JWK)],
                     [_endorse(MALLORY_KEY, AGENT_ID, "device", D9, D9)])
    r = _sealed_cli(["GITHUB_TOKEN", "--yes"], input=SEALED_SECRET + "\n")
    assert r.exit_code != 0
    assert "jon@dimagi.com's vault key changed" in r.output and "Nothing sealed" in r.output
    assert not s.puts() and _pins(home) == before          # never silently re-pinned


def test_no_endorsed_recipient_is_an_error_never_a_fallback(home, monkeypatch):
    _pin_jon(home)
    s = _seal_server(monkeypatch, [_recipient("device", D9, DEV9_JWK),
                                   _recipient("member", 7, JON_JWK)], [])
    r = _sealed_cli(["GITHUB_TOKEN", "--yes"], input=SEALED_SECRET + "\n")
    assert r.exit_code != 0 and "no recipient is endorsed" in r.output
    assert not s.puts()


def test_request_answers_through_the_request_route(home, monkeypatch):
    _pin_jon(home)
    s = _seal_server(monkeypatch, [_recipient("device", D9, DEV9_JWK)],
                     [_endorse(JON_KEY, AGENT_ID, "device", D9, D9)])
    r = CliRunner().invoke(vault_cli.vault_group,
                           ["zk", "set", "--agent", "bailey", "--request", REQ_ID],
                           input=SEALED_SECRET + "\n")
    assert r.exit_code == 0, r.output
    assert "GITHUB_TOKEN stored for bailey (sealed to 1 reader)" in r.output
    rec = [c for c in s.calls if c[1].endswith("/vault/zk/recipients")]
    assert rec and rec[0][3] == {"secret_request": [REQ_ID]}
    assert [c[1] for c in s.puts()] == [f"/api/agents/bailey/secret-requests/{REQ_ID}/value"]
    assert s.fulfilled["kv"] == 0
    assert zk.open_sealed(s.fulfilled["value_zk"], AGENT_ID, "GITHUB_TOKEN", "device", D9,
                          DEV9_KEY) == SEALED_SECRET
    # a NAME that is not what the request asks for is refused before anything is read
    r = CliRunner().invoke(vault_cli.vault_group,
                           ["zk", "set", "--agent", "bailey", "--request", REQ_ID, "OTHER"],
                           input=SEALED_SECRET + "\n")
    assert r.exit_code != 0 and "asks for GITHUB_TOKEN, not OTHER" in r.output
