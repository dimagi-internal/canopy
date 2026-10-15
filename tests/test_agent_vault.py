"""`canopy agent vault` — one command that gives an agent its vault, filled, and its key.

WHY (2026-10-15). Lex stalled after being created in the browser: no checklist step named a
vault, the old bootstrap script made four EMPTY items, the key was minted with different
permissions by two tools, and the agent's own canopy token could only be minted on the
server. What is pinned here:
- what a PERSON types vs what the system produces (`KINDS`): the canopy token is minted, the
  Gmail token is linked to Connect Google mailbox, unknown items are asked for;
- `--check` writes nothing anywhere;
- a re-run on a finished vault changes nothing;
- no secret reaches the printed report, and `op` never runs as an agent's service account.

Run: uv run pytest tests/test_agent_vault.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import agent_vault as av

SECRET_PAT, SECRET_KEY, SECRET_TYPED = "cpat_SECRET_1", "ops_SECRET_2", "typed-SECRET-3"

TEMPLATE = """\
# lex's env template
# lex_SOME_SECRET=op://${AGENT_VAULT}/<item>/<field>
lex_PRIMARY_CHANNEL=lex@dimagi-ai.com
CANOPY_WEB_PAT=op://${AGENT_VAULT}/canopy-pat/credential
KC_API_KEY=op://${AGENT_VAULT}/kc-api-key/credential
GDRIVE_ROOT_FOLDER=op://${AGENT_VAULT}/gdrive-root-folder/credential
GOG_TOKEN=op://${AGENT_VAULT}/gog-token/credential
OLD=op://${AGENT_VAULT}/claude-oauth-token/credential
"""


class FakeOp:
    account = "dimagi.1password.com"

    def __init__(self, vaults=None, signed_in=None):
        self.vaults = vaults if vaults is not None else {}   # vault -> {title: {field: value}}
        self.users: dict[str, set] = {v: set() for v in self.vaults}
        self.writes: list[str] = []
        self._signed = signed_in

    def signed_in(self):
        return self._signed

    def vault_exists(self, v):
        return v in self.vaults

    def create_vault(self, v, description):
        self.writes.append(f"vault {v}")
        self.vaults[v], self.users[v] = {}, set()

    def vault_user_emails(self, v):
        return self.users.get(v, set())

    def grant_vault_admin(self, v, email):
        self.writes.append(f"grant {email}")
        self.users[v].add(email)

    def get_item(self, v, title):
        fields = self.vaults.get(v, {}).get(title)
        return None if fields is None else {
            "id": title, "title": title,
            "fields": [{"id": k, "label": k, "value": val} for k, val in fields.items()]}

    def create_item(self, v, title, field):
        self.writes.append(f"item {title}")
        self.vaults[v][title] = {field: ""}
        return self.get_item(v, title)

    def set_field(self, v, item, field, value):
        self.writes.append(f"set {item['title']}")
        self.vaults[v][item["title"]][field] = value

    def mint_service_account(self, name, v):
        self.writes.append(f"sa {name}")
        return SECRET_KEY


class FakeWeb:
    """canopy-web at the transport boundary (method, url, headers, data) -> (status, text)."""

    def __init__(self, *, is_admin=True, vault="", key_set=False, put_status=200):
        self.agent = {"slug": "lex", "workspace": "connect", "is_admin": is_admin,
                      "email": "lex@dimagi-ai.com",
                      "owner": {"email": "lbagnoli@dimagi.com"},
                      "definition": {"repo_url": "https://github.com/dimagi-internal/lex",
                                     "repo_ref": "main"}}
        self.vault = {"vault": vault, "key_set": key_set}
        self.put_status = put_status
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, url, headers, data):
        body = json.loads(data) if data else None
        path = url.split("://", 1)[-1].split("/", 1)[1]
        self.calls.append((method, "/" + path, body))
        if method == "GET" and path.endswith("agents/lex/"):
            return 200, json.dumps(self.agent)
        if method == "GET" and path.endswith("agents/lex/vault"):
            return 200, json.dumps(self.vault)
        if method == "POST" and path.endswith("agents/lex/canopy-token"):
            return 201, json.dumps({"raw": SECRET_PAT, "token_id": 7, "label": "x",
                                    "user_email": "lex@dimagi-ai.com", "created_user": True})
        if method == "PUT" and path.endswith("agents/lex/vault"):
            if self.put_status != 200:
                return self.put_status, '{"detail":"nope"}'
            self.vault = {"vault": body["vault"], "key_set": True}
            return 200, json.dumps(self.vault)
        return 404, "{}"

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != "GET"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".env.tpl").write_text(TEMPLATE)
    return tmp_path


def _run(web, op, repo, **kw):
    if not web.vault["vault"]:
        kw.setdefault("vault", "Agent-Lex")
    return av.run("lex", token="person-pat", transport=web, op=op, repo=repo, **kw)


def test_refs_skip_comments_dedupe_and_flag_a_literal_vault():
    refs, literal = av.parse_refs(TEMPLATE + "X=op://Agent-Lex/canopy-pat/credential\n"
                                  "Y=op://${AGENT_VAULT}/kc-api-key/credential\n")
    assert refs[0] == ("canopy-pat", "credential")
    assert ("<item>", "<field>") not in refs           # the scaffold's commented example
    assert [r for r in refs if r[0] == "kc-api-key"] == [("kc-api-key", "credential")]
    assert literal == ["Agent-Lex"]


def test_op_never_runs_as_an_agents_service_account():
    env = av.person_op_env({"OP_SERVICE_ACCOUNT_TOKEN": "ops_agent", "PATH": "/bin"})
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in env
    assert env["OP_BIOMETRIC_UNLOCK_ENABLED"] == "true" and env["PATH"] == "/bin"


def test_a_new_agent_gets_everything_and_a_person_types_only_what_only_they_know(repo):
    op, web = FakeOp(), FakeWeb()
    asked: list[str] = []

    def ask(label):
        asked.append(label)
        return SECRET_TYPED if label.startswith("kc-api-key") else ""

    rep = _run(web, op, repo, ask=ask)
    assert rep.vault == "Agent-Lex"
    assert op.writes[:2] == ["vault Agent-Lex", "grant lbagnoli@dimagi.com"]
    rows = {r.item: (r.kind, r.state) for r in rep.rows}
    assert rows == {
        "canopy-pat": ("minted", "minted"),
        "kc-api-key": ("asked", "entered"),
        "gdrive-root-folder": ("asked", "created-empty"),
        "gog-token": ("linked", "elsewhere"),
        "claude-oauth-token": ("unused", "skipped"),
    }
    # Only person-only values were asked for — never the minted or linked ones.
    assert [a.split(" ", 1)[0] for a in asked] == ["kc-api-key", "gdrive-root-folder"]
    assert op.vaults["Agent-Lex"]["canopy-pat"]["credential"] == SECRET_PAT
    assert op.vaults["Agent-Lex"]["kc-api-key"]["credential"] == SECRET_TYPED
    assert "gog-token" not in op.vaults["Agent-Lex"]   # Connect Google mailbox makes it
    assert web.writes() == [("POST", "/api/agents/lex/canopy-token"), ("PUT", "/api/agents/lex/vault")]
    assert web.vault == {"vault": "Agent-Lex", "key_set": True}
    assert rep.key == "minted and stored"
    assert [r.item for r in rep.missing] == ["gdrive-root-folder"]
    printed = av.render(rep)
    for secret in (SECRET_PAT, SECRET_KEY, SECRET_TYPED):
        assert secret not in printed


def test_check_writes_nothing_anywhere(repo):
    op = FakeOp({"Agent-Lex": {"canopy-pat": {"credential": ""}}})
    web = FakeWeb(vault="Agent-Lex", key_set=False)
    rep = _run(web, op, repo, check=True, ask=lambda _: pytest.fail("--check never asks"))
    assert op.writes == [] and web.writes() == []
    assert rep.key == "not stored"
    assert {r.item for r in rep.missing} == {"canopy-pat", "kc-api-key", "gdrive-root-folder"}


def test_a_rerun_on_a_finished_vault_changes_nothing(repo):
    full = {"canopy-pat": {"credential": "x"}, "kc-api-key": {"credential": "y"},
            "gdrive-root-folder": {"credential": "z"}}
    op = FakeOp({"Agent-Lex": full})
    op.users["Agent-Lex"] = {"lbagnoli@dimagi.com"}
    web = FakeWeb(vault="Agent-Lex", key_set=True)
    rep = _run(web, op, repo, ask=lambda _: pytest.fail("nothing is empty"))
    assert op.writes == [] and web.writes() == [] and rep.actions == []
    assert rep.key == "stored" and rep.missing == []


def test_the_agents_own_token_is_minted_even_when_its_template_forgot_it(tmp_path):
    (tmp_path / ".env.tpl").write_text("lex_PRIMARY_CHANNEL=lex@dimagi-ai.com\n")
    op, web = FakeOp(), FakeWeb()
    rep = _run(web, op, tmp_path)
    assert [(r.item, r.state) for r in rep.rows] == [("canopy-pat", "minted")]
    assert any(av.PAT_ENV_LINE in n for n in rep.notes)


def test_with_no_vault_recorded_the_person_names_it(repo):
    """Never derived from the slug (2026-10-14)."""
    with pytest.raises(av.VaultError, match="pass --vault <name>"):
        av.run("lex", token="t", transport=FakeWeb(), op=FakeOp(), repo=repo)


def test_someone_who_is_not_its_admin_is_stopped_before_anything_is_made(repo):
    op, web = FakeOp(), FakeWeb(is_admin=False)
    with pytest.raises(av.VaultError, match="owner or an admin"):
        _run(web, op, repo)
    assert op.writes == []


def test_a_recorded_vault_is_never_silently_switched(repo):
    with pytest.raises(av.VaultError, match="refusing to use Agent-Other"):
        _run(FakeWeb(vault="Agent-Lex", key_set=True), FakeOp(), repo, vault="Agent-Other")


def test_op_signed_in_as_nobody_says_how_to_sign_in(repo):
    with pytest.raises(av.VaultError, match="Integrate with 1Password CLI"):
        _run(FakeWeb(), FakeOp(signed_in="You are not currently signed in"), repo)


def test_a_refused_key_upload_says_the_minted_account_must_be_deleted(repo):
    op = FakeOp()
    with pytest.raises(av.VaultError, match="stored NOWHERE: delete canopy-web-lex") as e:
        _run(FakeWeb(put_status=403), op, repo)
    assert SECRET_KEY not in str(e.value)


def test_a_canopy_web_without_the_route_says_deploy_it_not_a_traceback(repo):
    class Old(FakeWeb):
        def __call__(self, method, url, headers, data):
            if "canopy-token" in url:
                self.calls.append((method, url, None))
                return 404, "<!doctype html><html><title>Not Found</title></html>"
            return super().__call__(method, url, headers, data)

    with pytest.raises(av.VaultError, match="older version than this canopy; deploy it"):
        _run(Old(vault="Agent-Lex", key_set=True), FakeOp({"Agent-Lex": {}}), repo)


def test_the_report_says_how_each_empty_value_gets_filled(repo):
    rep = _run(FakeWeb(vault="Agent-Lex", key_set=True), FakeOp({"Agent-Lex": {}}), repo,
               check=True)
    printed = av.render(rep)
    assert "canopy-pat — re-run without --check to mint" in printed
    assert "kc-api-key, gdrive-root-folder — re-run in a terminal to be asked" in printed


def test_a_blank_canopy_web_email_falls_back_to_the_repos_identity(repo):
    """lex, 2026-10-15: created in the browser, its record had no email while its repo's
    config/agent.json named lex@dimagi-ai.com — the mint 422'd for want of an address."""
    (repo / "config").mkdir()
    (repo / "config" / "agent.json").write_text('{"email": "lex@dimagi-ai.com"}')
    web = FakeWeb()
    web.agent["email"] = ""
    _run(web, FakeOp(), repo)
    body = next(b for m, p, b in web.calls if p.endswith("canopy-token"))
    assert body["email"] == "lex@dimagi-ai.com"


def test_values_are_written_through_stdin_never_argv():
    """argv is visible to every process on the box (`ps`); stdin is not."""
    seen = []

    def runner(argv, **kw):
        seen.append((argv, kw.get("input")))
        class P:  # noqa: D401
            returncode, stdout, stderr = 0, "", ""
        return P()

    op = av.Op(runner=runner, env={"OP_SERVICE_ACCOUNT_TOKEN": "ops_agent"})
    op.set_field("Agent-Lex", {"id": "abc", "title": "kc-api-key",
                               "fields": [{"id": "credential", "value": ""}]},
                 "credential", SECRET_TYPED)
    argv, stdin = seen[0]
    assert SECRET_TYPED not in " ".join(argv)
    assert json.loads(stdin)["fields"][0]["value"] == SECRET_TYPED
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in op.env


ZERO_DATES = [{"id": "expires", "label": "expires", "type": "DATE", "value": "0"},
              {"id": "validFrom", "label": "valid from", "type": "DATE", "value": "0"}]


def test_items_never_carry_the_categorys_zero_dates():
    """op item create gives an API Credential item `expires` = 0, which the 1Password
    app shows as "Expired item" (Agent-Lex, 2026-10-15). Both the create and every
    write drop those dates; nothing else about the item changes."""
    seen = []

    def runner(argv, **kw):
        seen.append((argv, kw.get("input")))
        class P:  # noqa: D401
            returncode, stderr = 0, ""
            stdout = json.dumps({"id": "abc", "title": "kc-api-key", "fields": [
                {"id": "credential", "type": "CONCEALED", "value": ""}, *ZERO_DATES]})
        return P()

    op = av.Op(runner=runner, env={})
    op.create_item("Agent-Lex", "kc-api-key", "credential")
    assert "create" in seen[0][0]
    edit = json.loads(seen[1][1])
    assert [f["id"] for f in edit["fields"]] == ["credential"]

    seen.clear()
    op.set_field("Agent-Lex", {"id": "abc", "title": "kc-api-key", "fields": [
        {"id": "credential", "type": "CONCEALED", "value": ""}, *ZERO_DATES,
        {"id": "kept", "type": "DATE", "value": "1792000000"}]}, "credential", SECRET_TYPED)
    fields = json.loads(seen[0][1])["fields"]
    assert [f["id"] for f in fields] == ["credential", "kept"]
