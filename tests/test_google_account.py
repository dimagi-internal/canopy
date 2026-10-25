"""`canopy google-account`: the provisioner signs in as itself (no impersonation), and the
ordering of Google and vault writes never leaves a password that exists only at Google.

Google is a fake at the HTTP boundary; the vault is a dict-recording store."""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from orchestrator import google_account as ga

PW = "generated-password-never-printed"
RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY = {"client_email": "provisioner@p.iam.gserviceaccount.com",
       "private_key": RSA.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()}


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakeGoogle:
    def __init__(self, users=None, fail=()):
        self.users = dict(users or {})
        self.fail = set(fail)
        self.calls: list = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url))
        if url == ga.TOKEN_URL:
            form = dict(p.split("=", 1) for p in body.decode().split("&"))
            h, c, s = form["assertion"].split(".")
            RSA.public_key().verify(_b64d(s), f"{h}.{c}".encode(), padding.PKCS1v15(),
                                    hashes.SHA256())
            self.claims = json.loads(_b64d(c))
            return 200, {"access_token": "tok"}
        assert headers["Authorization"] == "Bearer tok"
        if method in self.fail:
            return 403, {"error": {"message": "Not Authorized to access this resource/api"}}
        email = url.rsplit("/", 1)[1].replace("%40", "@")
        if method == "GET":
            return (200, self.users[email]) if email in self.users else (404, {})
        if method == "POST":
            u = json.loads(body)
            self.users[u["primaryEmail"]] = u
            return 200, u
        if method == "PATCH":
            self.users[email].update(json.loads(body))
            return 200, self.users[email]
        raise AssertionError(method)


class Vault:
    def __init__(self, fail=False):
        self.values: dict = {}
        self.fail = fail

    def __call__(self, values):
        if self.fail:
            raise RuntimeError("vault down")
        self.values.update(values)
        return f"stored {len(values)} value(s)"


def _dir(g):
    return ga.Directory(ga.access_token(KEY, http=g), http=g)


def test_token_is_the_service_account_itself_with_one_scope():
    g = FakeGoogle()
    assert ga.access_token(KEY, http=g, now=1000) == "tok"
    assert g.claims["iss"] == KEY["client_email"] and "sub" not in g.claims
    assert g.claims["scope"] == ga.SCOPE and g.claims["exp"] - g.claims["iat"] == 600


def test_create_stores_then_creates_in_the_ou():
    g, v = FakeGoogle(), Vault()
    lines = ga.run_create(_dir(g), v, email="lex@dimagi-ai.com", given="Lex", family="Agent",
                          ou="/Non-person/AI", recovery_email="lex@dimagi.ai",
                          password=lambda: PW)
    u = g.users["lex@dimagi-ai.com"]
    assert u["orgUnitPath"] == "/Non-person/AI" and u["password"] == PW
    assert u["recoveryEmail"] == "lex@dimagi.ai" and u["changePasswordAtNextLogin"] is False
    assert v.values == {"GOOGLE_ACCOUNT": "lex@dimagi-ai.com", "GOOGLE_PASSWORD": PW}
    assert PW not in "\n".join(lines)


def test_create_refuses_an_existing_address_before_touching_the_vault():
    g, v = FakeGoogle({"ada@dimagi-ai.com": {"orgUnitPath": "/Non-person/AI"}}), Vault()
    with pytest.raises(ga.GoogleAccountError, match="already exists"):
        ga.run_create(_dir(g), v, email="ada@dimagi-ai.com", given="Ada", family="Agent",
                      ou="/Non-person/AI", password=lambda: PW)
    assert v.values == {}


def test_create_reads_403_on_lookup_as_absent_because_google_says_403_for_no_such_user():
    """Measured 2026-10-25: the OU-fenced provisioner gets 403, not 404, for an address
    that does not exist — so a 403 that stopped `create` meant it could create nobody."""
    g, v = FakeGoogle(fail={"GET"}), Vault()
    ga.run_create(_dir(g), v, email="trellis@dimagi-ai.com", given="Trellis", family="Agent",
                  ou="/Non-person/AI", password=lambda: PW)
    assert g.users["trellis@dimagi-ai.com"]["orgUnitPath"] == "/Non-person/AI"


def test_create_of_an_address_taken_outside_the_ou_is_refused_by_googles_insert():
    class Taken(FakeGoogle):
        def __call__(self, method, url, body, headers):
            if method == "POST" and url == ga.USERS_URL:
                self.calls.append((method, url))
                return 409, {"error": {"message": "Entity already exists."}}
            return super().__call__(method, url, body, headers)

    g, v = Taken(fail={"GET"}), Vault()
    with pytest.raises(ga.GoogleAccountError, match="already exists outside"):
        ga.run_create(_dir(g), v, email="someone@dimagi-ai.com", given="S", family="P",
                      ou="/Non-person/AI", password=lambda: PW)


def test_reset_still_treats_403_as_an_error():
    g, v = FakeGoogle(fail={"GET"}), Vault()
    with pytest.raises(ga.GoogleAccountError, match="cannot read"):
        ga.run_reset(_dir(g), v, email="someone@dimagi.com", ou="/Non-person/AI",
                     password=lambda: PW)
    assert v.values == {}


def test_reset_sets_then_stores():
    g = FakeGoogle({"ada@dimagi-ai.com": {"orgUnitPath": "/Non-person/AI"}})
    v = Vault()
    ga.run_reset(_dir(g), v, email="ada@dimagi-ai.com", ou="/Non-person/AI",
                 password=lambda: PW)
    assert g.users["ada@dimagi-ai.com"]["password"] == PW
    assert v.values["GOOGLE_PASSWORD"] == PW


def test_reset_refuses_a_user_outside_the_ou():
    g = FakeGoogle({"jj@dimagi.com": {"orgUnitPath": "/Staff"}})
    with pytest.raises(ga.GoogleAccountError, match="refusing"):
        ga.run_reset(_dir(g), Vault(), email="jj@dimagi.com", ou="/Non-person/AI",
                     password=lambda: PW)
    assert "password" not in g.users["jj@dimagi.com"]


def test_reset_says_loudly_when_the_store_fails_after_google_changed():
    g = FakeGoogle({"ada@dimagi-ai.com": {"orgUnitPath": "/Non-person/AI/Sub"}})
    with pytest.raises(ga.GoogleAccountError, match="CHANGED") as e:
        ga.run_reset(_dir(g), Vault(fail=True), email="ada@dimagi-ai.com",
                     ou="/Non-person/AI", password=lambda: PW)
    assert PW not in str(e.value)


def test_load_key_order(tmp_path):
    f = tmp_path / "k.json"
    f.write_text(json.dumps({"from": "file"}))
    env = {ga.KEY_REF: json.dumps({"from": "env"})}
    vault = lambda slug, refs: json.dumps({"from": slug})  # noqa: E731
    assert ga.load_key(str(f), "ada", env=env, resolve=vault) == {"from": "file"}
    assert ga.load_key("", "ada", env=env, resolve=vault) == {"from": "env"}
    assert ga.load_key("", "ada", env={}, resolve=vault) == {"from": "ada"}
    with pytest.raises(ga.GoogleAccountError, match="no provisioner key"):
        ga.load_key("", "ada", env={}, resolve=lambda s, r: None)


class FakeZkVault:
    """The two calls `vault_store` makes of a `vault_zk_cli.ZkVault`."""

    vault_id = ""

    def __init__(self, state, wrap_error=None, slug="trellis"):
        self._state, self.wrap_error, self.puts, self.slug = state, wrap_error, [], slug

    def state(self):
        return self._state

    def web(self, method, path, body=None):
        self.puts.append((method, path, body))
        return []

    def my_wrap(self):
        if self.wrap_error:
            raise self.wrap_error


def test_a_new_agent_with_no_vault_key_is_stored_through_canopy_webs_sealing_write():
    v = FakeZkVault({"enabled": True, "kv": 0})
    line = ga.vault_store("trellis", vault=v)({"GOOGLE_ACCOUNT": "t@dimagi-ai.com", "GOOGLE_PASSWORD": PW})
    assert v.puts == [("PUT", "/api/agents/trellis/credentials",
                       {"values": {"GOOGLE_ACCOUNT": "t@dimagi-ai.com", "GOOGLE_PASSWORD": PW}})]
    assert "sealed" in line and PW not in line


def test_an_agent_with_a_vault_key_still_needs_this_devices_copy_before_google():
    from orchestrator import vault_zk_cli as zc

    v = FakeZkVault({"enabled": True, "kv": 1}, wrap_error=zc.ZkCliError("no wrap"))
    with pytest.raises(ga.GoogleAccountError, match="cannot write"):
        ga.vault_store("ada", vault=v)
    assert v.puts == []


def test_a_vault_this_caller_cannot_read_stops_before_google():
    from orchestrator import vault_zk_cli as zc

    class Refused(FakeZkVault):
        def state(self):
            raise zc.ZkCliError("canopy-web refused GET: 403 owner or admin")

    with pytest.raises(ga.GoogleAccountError, match="403"):
        ga.vault_store("trellis", vault=Refused({}))
