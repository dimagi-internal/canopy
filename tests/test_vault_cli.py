"""`canopy vault` (canopy#946): move an agent's credentials between 1Password and canopy-web.

What is pinned:
- the agent's refs come from its .env.tpl (op refs AND literals) plus runtime_sources, and
  items another flow owns (gog-token, the shared OAuth client) are left alone as refs — but
  the vault's Gmail token itself is carried to canopy-web's `gog-token` when canopy-web has
  none (encrypted here under `--zk`), and a switch refuses a mailbox agent without it (its
  Gmail would stop);
- import reads with the PERSON's op (a fake `op` on PATH), writes canopy-web, never
  touches the vault, and `--switch` refuses while anything is missing;
- export writes back only what differs; status reports drift by hash;
- no value ever reaches stdout/stderr or an `op` argv.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from urllib.parse import parse_qs, urlsplit

import pytest
from click.testing import CliRunner

from orchestrator import agent_vault as av
from orchestrator import canopy_web, cred_cli, vault_cli

SECRETS = {"canopy-pat": "cpat_SECRET_A", "kindora-service-key": "kindora_SECRET_B",
           "gdrive-root-folder": "folder_SECRET_C"}

TEMPLATE = textwrap.dedent("""\
    # Eva configuration — op://${AGENT_VAULT}/commented/out is not a ref
    eva_PRIMARY_CHANNEL=eva@example.com
    GDRIVE_ROOT_FOLDER=op://${AGENT_VAULT}/gdrive-root-folder/credential
    CANOPY_WEB_PAT=op://${AGENT_VAULT}/canopy-pat/credential
    KINDORA_SERVICE_KEY=op://${AGENT_VAULT}/kindora-service-key/credential
    GOG_TOKEN=op://${AGENT_VAULT}/gog-token/credential
    MIXED=prefix-op://${AGENT_VAULT}/x/y
    EMPTY=
""")

FAKE_OP = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    state_path = os.environ["FAKE_OP_STATE"]
    st = json.load(open(state_path))
    argv = sys.argv[1:]
    st.setdefault("argv", []).append(argv)
    if "--account" in argv:
        i = argv.index("--account"); argv = argv[:i] + argv[i + 2:]
    def save():
        json.dump(st, open(state_path, "w"))
    def item_json(vault, title):
        fields = st["vaults"][vault][title]
        return {{"id": title, "title": title,
                 "fields": [{{"id": k, "label": k, "value": v}} for k, v in fields.items()]}}
    if os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        save(); print("[ERROR] an agent key must never be used here", file=sys.stderr); sys.exit(9)
    if not st.get("signed_in", True):
        save(); print("[ERROR] You are not currently signed in.", file=sys.stderr); sys.exit(1)
    if argv[:2] == ["vault", "list"]:
        save(); print("[]"); sys.exit(0)
    if argv[0] == "read":
        ref = argv[-1][len("op://"):].split("/")
        vault, item, field = ref[0], ref[1], ref[-1]
        try:
            value = st["vaults"][vault][item][field]
        except KeyError:
            save(); print(f'[ERROR] could not read secret "{{argv[-1]}}"', file=sys.stderr); sys.exit(1)
        save(); sys.stdout.write(value); sys.exit(0)
    if argv[:2] == ["item", "get"]:
        vault = argv[argv.index("--vault") + 1]
        if argv[2] not in st["vaults"].get(vault, {{}}):
            save(); print("[ERROR] isn't an item", file=sys.stderr); sys.exit(1)
        save(); print(json.dumps(item_json(vault, argv[2]))); sys.exit(0)
    if argv[:2] == ["item", "create"]:
        vault = argv[argv.index("--vault") + 1]; title = argv[argv.index("--title") + 1]
        field = [a for a in argv if a.endswith("[password]=")][0].split("[")[0]
        st["vaults"].setdefault(vault, {{}})[title] = {{field: ""}}
        save(); print(json.dumps(item_json(vault, title))); sys.exit(0)
    if argv[:2] == ["item", "edit"]:
        vault = argv[argv.index("--vault") + 1]
        doc = json.loads(sys.stdin.read())
        st["vaults"][vault][doc["title"]] = {{f["id"]: f.get("value", "") for f in doc["fields"]}}
        save(); print(json.dumps(item_json(vault, doc["title"]))); sys.exit(0)
    save(); print("unexpected " + " ".join(argv), file=sys.stderr); sys.exit(2)
''')


@pytest.fixture
def fake_op(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "op").write_text(FAKE_OP.format(python=sys.executable))
    (bindir / "op").chmod(0o755)
    state = tmp_path / "op-state.json"
    state.write_text(json.dumps({"vaults": {"Agent-Eva": {
        k: {"credential": v} for k, v in SECRETS.items()}}}))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_OP_STATE", str(state))
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_AGENT_KEY_must_be_dropped")

    class Handle:
        def read(self):
            return json.loads(state.read_text())

        def set(self, **kw):
            d = self.read()
            d.update(kw)
            state.write_text(json.dumps(d))

    return Handle()


class FakeWeb:
    def __init__(self, *, is_admin=True, source="1password", stored=None, sources=None,
                 email=""):
        self.is_admin, self.source, self.email = is_admin, source, email
        self.stored = dict(stored or {})
        self.sources = sources or {}
        self.calls = []

    def __call__(self, method, url, headers, data):
        parts = urlsplit(url)
        path, q = parts.path, parse_qs(parts.query)
        body = json.loads(data) if data else None
        self.calls.append((method, path, body))
        base = "/api/agents/eva"
        if method == "GET" and path == f"{base}/":
            return 200, json.dumps({"slug": "eva", "is_admin": self.is_admin, "email": self.email,
                                    "op_vault": "Agent-Eva", "runtime_sources": self.sources})
        if method == "GET" and path == f"{base}/credentials/status":
            return 200, json.dumps([{"name": n, "declared": False, "set": True,
                                     "source": "canopy-web", "updated_at": "2026-10-16T00:00:00Z",
                                     "last_read_at": "2026-10-16T01:00:00Z"}
                                    for n in sorted(self.stored)])
        if method == "PUT" and path == f"{base}/credentials":
            if not self.is_admin:
                return 403, '{"detail":"admin only"}'
            self.stored.update(body["values"])
            return 200, "[]"
        if method == "PATCH" and path == f"{base}/credential-source":
            self.source = body["credential_source"]
            return 200, json.dumps({"slug": "eva", "credential_source": self.source})
        if method == "GET" and path == f"{base}/credentials/access":
            return 200, json.dumps({"agent": "eva", "credential_source": self.source,
                                    "may_resolve": True, "via": "admin", "reason": ""})
        if method == "GET" and path == f"{base}/credentials/resolve":
            want = q.get("refs", [""])[0].split(",") if "refs" in q else None
            vals = {k: v for k, v in self.stored.items() if want is None or k in want}
            return 200, json.dumps({"values": vals})
        return 404, "{}"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "eva"
    r.mkdir()
    (r / ".env.tpl").write_text(TEMPLATE)
    monkeypatch.setattr(cred_cli, "agent_vault_name", lambda slug: "Agent-Eva")
    monkeypatch.setattr(cred_cli, "_agent_repo", lambda slug: r)
    monkeypatch.setattr(cred_cli, "CACHE_FILE", tmp_path / "cred-cache.json")
    return r


@pytest.fixture
def web(monkeypatch):
    w = FakeWeb()
    monkeypatch.setattr(canopy_web, "urllib_transport", w)
    monkeypatch.setattr(vault_cli, "_token", lambda: "person-pat")
    return w


def _no_secret(text: str) -> None:
    for v in SECRETS.values():
        assert v not in text


# ── the refs ──────────────────────────────────────────────────────────────────

def test_template_refs_literals_and_what_is_left_alone():
    refs, notes = vault_cli.parse_template(TEMPLATE, vault="Agent-Eva", slug="eva")
    by = {r.name: r for r in refs}
    assert by["eva_PRIMARY_CHANNEL"].literal == "eva@example.com"
    assert by["CANOPY_WEB_PAT"].op == "op://Agent-Eva/canopy-pat/credential"
    assert "EMPTY" not in by and "MIXED" not in by
    assert any("MIXED" in n for n in notes)
    assert vault_cli.owned_elsewhere(by["GOG_TOKEN"])          # Connect Google mailbox owns it
    assert not vault_cli.owned_elsewhere(by["KINDORA_SERVICE_KEY"])


def test_runtime_sources_add_what_the_template_does_not_name():
    refs = vault_cli.refs_from_sources(
        {"CANOPY_WEB_PAT": {"op": "op://Other/x/y"}, "API_BASE": {"value": "https://x"},
         "LOCAL": {"local_only": True}, "KEY": {"op": "op://Agent-Eva/key/credential"}},
        known={"CANOPY_WEB_PAT"})
    assert [(r.name, r.op or r.literal) for r in refs] == [
        ("API_BASE", "https://x"), ("KEY", "op://Agent-Eva/key/credential")]


# ── import ────────────────────────────────────────────────────────────────────

def test_import_copies_to_canopy_web_and_leaves_1password_alone(fake_op, repo, web):
    before = fake_op.read()["vaults"]
    res = vault_cli.run_import("eva", token="person-pat", op=av.Op())
    assert res.ok
    assert web.stored == {"eva_PRIMARY_CHANNEL": "eva@example.com",
                          "GDRIVE_ROOT_FOLDER": SECRETS["gdrive-root-folder"],
                          "CANOPY_WEB_PAT": SECRETS["canopy-pat"],
                          "KINDORA_SERVICE_KEY": SECRETS["kindora-service-key"]}
    assert "GOG_TOKEN" not in web.stored
    assert fake_op.read()["vaults"] == before
    argv = json.dumps(fake_op.read()["argv"])
    _no_secret(argv)                                              # values never in argv
    assert web.source == "1password"                              # no --switch, no switch


def test_import_zk_encrypts_here_and_never_puts_plaintext(fake_op, repo, web, monkeypatch):
    """`--zk` (canopy-web#1550): the values go to the ZK store through the device's vault
    key — never to PUT /credentials, the custodial path."""
    from orchestrator import vault_zk_cli
    got = {}

    def run_set(v, values):
        got.update(values)
        web.stored.update({n: "zk1:x" for n in values})   # what canopy-web would report set
        return vault_zk_cli.ZkResult("eva", actions=["stored encrypted"])

    monkeypatch.setattr(vault_zk_cli, "run_set", run_set)
    res = vault_cli.run_import("eva", token="person-pat", op=av.Op(), zk=True)
    assert res.ok and "stored encrypted" in res.actions
    assert got["KINDORA_SERVICE_KEY"] == SECRETS["kindora-service-key"]
    assert not [c for c in web.calls if c[0] == "PUT" and c[1].endswith("/credentials")]


def test_import_switch_moves_the_backend_only_when_complete(fake_op, repo, web):
    res = vault_cli.run_import("eva", token="person-pat", op=av.Op(), switch=True)
    assert res.ok and web.source == "canopy-web"
    assert ("PATCH", "/api/agents/eva/credential-source",
            {"credential_source": "canopy-web"}) in web.calls


def test_import_switch_refuses_when_a_ref_cannot_be_read(fake_op, repo, web):
    st = fake_op.read()
    del st["vaults"]["Agent-Eva"]["kindora-service-key"]
    fake_op.set(vaults=st["vaults"])
    res = vault_cli.run_import("eva", token="person-pat", op=av.Op(), switch=True)
    assert not res.ok and web.source == "1password"
    assert [r.state for r in res.rows if r.name == "KINDORA_SERVICE_KEY"] == ["unreadable"]
    assert "KINDORA_SERVICE_KEY" not in web.stored
    assert any("NOT switched" in n for n in res.notes)


def test_import_needs_a_person_signed_in(fake_op, repo, web):
    fake_op.set(signed_in=False)
    with pytest.raises(vault_cli.VaultCliError, match="not answering as a person"):
        vault_cli.run_import("eva", token="person-pat", op=av.Op())
    assert web.stored == {}


def test_import_refused_for_a_non_admin(fake_op, repo, monkeypatch):
    w = FakeWeb(is_admin=False)
    monkeypatch.setattr(canopy_web, "urllib_transport", w)
    with pytest.raises(vault_cli.VaultCliError, match="owner or an admin"):
        vault_cli.run_import("eva", token="person-pat", op=av.Op())


def test_import_cli_never_prints_a_value(fake_op, repo, web, monkeypatch):
    from orchestrator.cli import main
    r = CliRunner().invoke(main, ["vault", "import", "--agent", "eva", "--switch"])
    assert r.exit_code == 0, r.output
    _no_secret(r.output)
    assert "switched eva to canopy-web" in r.output


# ── the Gmail token (found piloting Hal, 2026-10-17) ──────────────────────────

GMAIL = '{"email":"eva@example.com","client":"canopy","refresh_token":"rt_SECRET_G"}'


def _with_gmail_token(fake_op):
    st = fake_op.read()
    st["vaults"]["Agent-Eva"]["gog-token"] = {"credential": GMAIL}
    fake_op.set(vaults=st["vaults"])


def test_import_carries_the_gmail_token_the_runner_reads(fake_op, repo, web):
    """The runner bootstrap reads op://<vault>/gog-token directly, not via .env.tpl; after a
    switch its runners get no 1Password key, so the token must reach canopy-web."""
    _with_gmail_token(fake_op)
    res = vault_cli.run_import("eva", token="person-pat", op=av.Op())
    assert res.ok and web.stored["gog-token"] == GMAIL
    assert "GOG_TOKEN" not in web.stored                    # the .env.tpl ref stays left alone
    assert "rt_SECRET_G" not in json.dumps(fake_op.read()["argv"])


def test_import_never_overwrites_a_gmail_token_canopy_web_already_holds(fake_op, repo, web):
    _with_gmail_token(fake_op)
    web.stored["gog-token"] = "minted-by-connect-google-mailbox"
    vault_cli.run_import("eva", token="person-pat", op=av.Op())
    assert web.stored["gog-token"] == "minted-by-connect-google-mailbox"


def test_import_zk_encrypts_the_gmail_token_too_and_switches(fake_op, repo, web, monkeypatch):
    """The runner opens zero-knowledge values on the device before the bootstrap reads the
    Gmail token, so `--zk` carries it like any other value — encrypted here."""
    from orchestrator import vault_zk_cli
    got = {}

    def run_set(v, values):
        got.update(values)
        web.stored.update({n: "zk1:x" for n in values})
        return vault_zk_cli.ZkResult("eva", actions=["stored encrypted"])

    monkeypatch.setattr(vault_zk_cli, "run_set", run_set)
    _with_gmail_token(fake_op)
    web.email = "eva@example.com"
    res = vault_cli.run_import("eva", token="person-pat", op=av.Op(), zk=True, switch=True)
    assert got["gog-token"] == GMAIL and web.stored["gog-token"] == "zk1:x"
    assert res.ok and web.source == "canopy-web"
    assert not [c for c in web.calls if c[0] == "PUT" and c[1].endswith("/credentials")]


def test_switch_refuses_an_agent_whose_gmail_token_is_only_in_1password(repo, web):
    web.email = "eva@example.com"
    web.stored = {"eva_PRIMARY_CHANNEL": "x", "GDRIVE_ROOT_FOLDER": "x", "CANOPY_WEB_PAT": "x",
                  "KINDORA_SERVICE_KEY": "x"}
    with pytest.raises(vault_cli.VaultCliError, match="mailbox"):
        vault_cli.run_switch("eva", "canopy-web", token="person-pat")
    assert web.source == "1password"
    web.stored["gog-token"] = GMAIL
    vault_cli.run_switch("eva", "canopy-web", token="person-pat")
    assert web.source == "canopy-web"


# ── switch ────────────────────────────────────────────────────────────────────

def test_switch_to_canopy_web_refuses_when_refs_are_missing(repo, web):
    with pytest.raises(vault_cli.VaultCliError, match="vault import"):
        vault_cli.run_switch("eva", "canopy-web", token="person-pat")
    assert web.source == "1password"


def test_switch_back_to_1password_needs_no_check(repo, web):
    web.source = "canopy-web"
    res = vault_cli.run_switch("eva", "1password", token="person-pat")
    assert web.source == "1password" and any("export" in n for n in res.notes)


# ── export ────────────────────────────────────────────────────────────────────

def test_export_writes_back_only_what_changed(fake_op, repo, web):
    web.stored = {"CANOPY_WEB_PAT": SECRETS["canopy-pat"],
                  "KINDORA_SERVICE_KEY": "kindora_ROTATED", "GDRIVE_ROOT_FOLDER": "new-folder"}
    st = fake_op.read()
    del st["vaults"]["Agent-Eva"]["gdrive-root-folder"]
    fake_op.set(vaults=st["vaults"])
    res = vault_cli.run_export("eva", token="person-pat", op=av.Op())
    states = {r.name: r.state for r in res.rows}
    assert states == {"CANOPY_WEB_PAT": "same", "KINDORA_SERVICE_KEY": "written",
                      "GDRIVE_ROOT_FOLDER": "written"}
    vault = fake_op.read()["vaults"]["Agent-Eva"]
    assert vault["kindora-service-key"]["credential"] == "kindora_ROTATED"
    assert vault["gdrive-root-folder"]["credential"] == "new-folder"
    assert "kindora_ROTATED" not in json.dumps(fake_op.read()["argv"])


def test_export_check_writes_nothing(fake_op, repo, web):
    web.stored = {"KINDORA_SERVICE_KEY": "kindora_ROTATED"}
    before = fake_op.read()["vaults"]
    res = vault_cli.run_export("eva", token="person-pat", op=av.Op(), check=True)
    assert {r.name: r.state for r in res.rows}["KINDORA_SERVICE_KEY"] == "would-write"
    assert fake_op.read()["vaults"] == before


# ── status ────────────────────────────────────────────────────────────────────

def test_status_reports_presence_and_drift_by_hash(fake_op, repo, web):
    web.stored = {"CANOPY_WEB_PAT": SECRETS["canopy-pat"], "KINDORA_SERVICE_KEY": "rotated",
                  "eva_PRIMARY_CHANNEL": "eva@example.com", "ORPHAN": "x"}
    st = vault_cli.run_status("eva", token="person-pat", op=av.Op())
    rows = {r["name"]: r for r in st["refs"]}
    assert rows["CANOPY_WEB_PAT"]["drift"] == "same"
    assert rows["KINDORA_SERVICE_KEY"]["drift"] == "DRIFT"
    assert rows["GDRIVE_ROOT_FOLDER"] == {**rows["GDRIVE_ROOT_FOLDER"], "present": False,
                                          "drift": "only-1password"}
    assert rows["eva_PRIMARY_CHANNEL"]["drift"] == "same"
    assert rows["CANOPY_WEB_PAT"]["last_read"] == "2026-10-16T01:00:00Z"
    assert st["stored_not_declared"] == ["ORPHAN"]
    text = vault_cli.render_status(st) + json.dumps(st, default=str)
    _no_secret(text)
    assert "rotated" not in text


def test_status_without_drift_reads_no_values(repo, web):
    st = vault_cli.run_status("eva", token="person-pat", op=None, drift=False)
    assert all(r["drift"] == "" for r in st["refs"])
    assert not any(p.endswith("/credentials/resolve") for _, p, _ in web.calls)


def test_status_shows_a_pending_device_key_approve_url(repo, web):
    from orchestrator import device_key
    device_key.write_status({"jkt": "J" * 43, "status": "pending", "kind": "secure-enclave",
                             "approve_url": "https://canopy.example/approve/3", "runner": "r"})
    st = vault_cli.run_status("eva", token="person-pat", op=None, drift=False)
    assert "https://canopy.example/approve/3" in vault_cli.render_status(st)


# ── device ────────────────────────────────────────────────────────────────────

def test_device_enroll_cli(monkeypatch, tmp_path):
    from orchestrator import device_key
    from orchestrator.cli import main
    monkeypatch.setattr(vault_cli, "_token", lambda: "runner-pat")
    calls = []

    def transport(method, url, headers, data):
        body = json.loads(data) if data else None
        calls.append((method, urlsplit(url).path, body))
        return 201, json.dumps({"id": 5, "jkt": device_key.jwk_thumbprint(body["public_jwk"]),
                                "status": "pending", "approve_url": "https://c/approve/5"})

    monkeypatch.setattr(canopy_web, "urllib_transport", transport)
    r = CliRunner().invoke(main, ["vault", "device", "--enroll", "--runner", "r-9",
                                  "--software", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output.strip().splitlines()[-1])
    assert out["status"] == "pending" and out["approve_url"] == "https://c/approve/5"
    assert calls[0][:2] == ("POST", "/api/runner-device-keys/") and calls[0][2]["runner"] == "r-9"
    shown = CliRunner().invoke(main, ["vault", "device"])
    assert "pending" in shown.output and "https://c/approve/5" in shown.output


def test_device_kind_and_software_must_agree(monkeypatch):
    from orchestrator.cli import main
    monkeypatch.setattr(vault_cli, "_token", lambda: "runner-pat")
    r = CliRunner().invoke(main, ["vault", "device", "--enroll", "--runner", "r-9",
                                  "--software", "--kind", "tpm"])
    assert r.exit_code != 0 and "disagree" in r.output


def test_device_kind_tpm_off_windows_is_a_clear_error(monkeypatch):
    from orchestrator.cli import main
    monkeypatch.setattr(vault_cli, "_token", lambda: "runner-pat")
    r = CliRunner().invoke(main, ["vault", "device", "--enroll", "--runner", "r-9",
                                  "--kind", "tpm"])
    assert r.exit_code != 0 and "no TPM here" in r.output


# ── binding ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("state,want", [("on", True), ("off", False)])
def test_binding_patches_device_binding(monkeypatch, state, want):
    from orchestrator.cli import main
    monkeypatch.setattr(vault_cli, "_token", lambda: "owner-pat")
    calls = []

    def transport(method, url, headers, data):
        calls.append((method, urlsplit(url).path, json.loads(data) if data else None))
        return 200, json.dumps({"require_device_binding": want})

    monkeypatch.setattr(canopy_web, "urllib_transport", transport)
    r = CliRunner().invoke(main, ["vault", "binding", "--agent", "eva", state])
    assert r.exit_code == 0, r.output
    assert calls == [("PATCH", "/api/agents/eva/device-binding",
                      {"require_device_binding": want})]
    assert f"require_device_binding={state}" in r.output


def test_binding_refuses_a_contradicting_answer(monkeypatch):
    monkeypatch.setattr(canopy_web, "urllib_transport",
                        lambda *a: (200, json.dumps({"require_device_binding": False})))
    with pytest.raises(vault_cli.VaultCliError, match="expected True"):
        vault_cli.run_binding("eva", True, token="t")
