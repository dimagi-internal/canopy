"""`canopy agent use <slug>` — use an agent yourself in Claude Code, no runner needed
(Jonathan, 2026-10-21). Every step is faked through `agent_use.Deps`; each branch says
ok / done / NEEDS YOU with the exact URL, command or person."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from orchestrator import agent_use as au
from orchestrator.canopy_web import CanopyError

BASE = "https://canopy.example"


class World:
    """A fake world with everything in place; tests knock one thing out."""

    def __init__(self):
        self.me = {"id": 7, "email": "aath@dimagi.com"}
        self.agent = {"slug": "eva", "name": "Eva", "plugin": {"name": "eva"},
                      "workspace": {"slug": "ooceo"}, "owner": {"email": "jj@dimagi.com"}}
        self.web_fail: dict = {}
        self.licenses = [{"user_id": 7, "email": "aath@dimagi.com", "status": "active",
                          "basis": "full:members"}]
        self.registration = {"id": "pr-1", "name": "aath-mbp"}
        self.registered = []
        self.access = {"may_resolve": False, "via": None, "error": ""}
        self.device = {"id": "dk-1", "jkt": "abcdef0123456789xyz", "status": "active",
                       "kind": "secure-enclave", "runner": "rid-1", "ecdh_jkt": "ec-1"}
        self.runner_id = "rid-1"
        self.enrolled = []
        self.zk = {"enabled": True, "kv": 1, "wraps": [], "pending": [],
                   "this_device": "holds a wrap at the current kv"}
        self.zk_error = None
        self.holders = {"has_key": True, "people": [], "keepers": []}
        self.installed = {"eva", "canopy"}
        self.known = {"canopy-agents", "canopy"}
        self.install_result = (True, "installed")
        self.installs = []
        self.agent_set = []
        self.agent_changed = True
        self.synced = []
        self.sync_result = "re-injected from canopy-web"
        self.sync_error = None
        self.google = {"repo_error": "", "hosted": "", "gog": True,
                       "install": "brew install gogcli"}
        self.google_result = {"ok": True, "imported": False, "wrote_client": False,
                              "summary": "canopy-web canopy 3d (present), gmail LIVE (canopy)",
                              "notes": []}
        self.google_logins = []

    def web(self, method, path, body=None):
        if path in self.web_fail:
            raise self.web_fail[path]
        if path == "/api/me/":
            return self.me
        if path == "/api/agents/eva/":
            return self.agent
        if path == "/api/agents/eva/licenses":
            return self.licenses
        if path == "/api/agents/eva/vault/members":
            return {"holders": self.holders}
        raise AssertionError(path)

    def deps(self) -> au.Deps:
        def zk_status(slug):
            if self.zk_error:
                raise self.zk_error
            return self.zk

        def register():
            self.registered.append(True)
            return {"id": "pr-2", "name": "new-mbp"}

        def enroll(rid):
            self.enrolled.append(rid)
            return {"id": "dk-2", "jkt": "fedcba", "status": "pending",
                    "approve_url": f"{BASE}/runners/approve/dk-2"}

        def install(p):
            self.installs.append(p)
            return self.install_result

        def set_agent(slug, d):
            self.agent_set.append((slug, d))
            return self.agent_changed

        def sync(slug):
            self.synced.append(slug)
            if self.sync_error:
                raise self.sync_error
            return self.sync_result

        def google_login(slug):
            self.google_logins.append(slug)
            if isinstance(self.google_result, Exception):
                raise self.google_result
            return self.google_result

        return au.Deps(
            base_url=lambda: BASE, web=self.web,
            read_registration=lambda: self.registration, register_computer=register,
            access=lambda slug: self.access, device_status=lambda: dict(self.device),
            machine_runner_id=lambda: self.runner_id, enroll_device=enroll,
            refresh_device=lambda rid: dict(self.device), zk_status=zk_status,
            installed_plugins=lambda: self.installed, known_marketplaces=lambda: self.known,
            install_plugin=install, set_session_agent=set_agent, sync_env=sync,
            google_check=lambda slug: dict(self.google), google_login=google_login)


@pytest.fixture
def w():
    return World()


def _run(w, tmp_path):
    return au.run("eva", str(tmp_path), w.deps())


def _step(rep, name):
    return next(s for s in rep.steps if s.name == name)


# ── the happy path ───────────────────────────────────────────────────────────

def test_everything_in_place_ends_with_the_start_line(w, tmp_path):
    rep = _run(w, tmp_path)
    assert not rep.needs
    assert [s.name for s in rep.steps] == ["signed in", "agent", "this computer", "secrets",
                                           "device key", "vault access", "licence", "plugin",
                                           "this project", "env", "google login"]
    assert rep.end_line() == f"Start a new Claude Code session in {tmp_path.resolve()} and run /eva:turn"
    assert _step(rep, "secrets").detail == "canopy vault (zero-knowledge, kv 1)"
    assert w.agent_set == [("eva", str(tmp_path.resolve()))]
    assert w.registered == [] and w.installs == []


def test_render_says_ok_done_needs_you(w, tmp_path):
    w.agent_changed = True
    out = _run(w, tmp_path).render()
    assert "[ok       ] signed in" in out and "[done     ] this project" in out
    assert "run /eva:turn" in out


# ── signed in / agent ─────────────────────────────────────────────────────────

def test_not_signed_in_stops_with_setup(w, tmp_path):
    w.web_fail["/api/me/"] = CanopyError("GET /api/me/ -> 401: no token")
    rep = _run(w, tmp_path)
    assert [s.name for s in rep.steps] == ["signed in"]
    assert rep.needs[0].fix.startswith("run /canopy:setup")
    assert "NEEDS YOU:" in rep.end_line()


def test_an_agent_you_cannot_see_says_ask_for_an_invite(w, tmp_path):
    w.web_fail["/api/agents/eva/"] = CanopyError("GET /api/agents/eva/ -> 404: not found")
    rep = _run(w, tmp_path)
    assert rep.needs[0].name == "agent" and "workspace admin" in rep.needs[0].fix


# ── this computer ─────────────────────────────────────────────────────────────

def test_an_unregistered_computer_is_registered(w, tmp_path):
    w.registration = {}
    s = _step(_run(w, tmp_path), "this computer")
    assert s.status == au.DONE and "new-mbp" in s.detail and w.registered == [True]
    assert "private runner" not in s.detail          # an internal word, never shown


# ── device key ────────────────────────────────────────────────────────────────

def test_no_device_key_and_a_runner_enrolls_and_points_at_approval(w, tmp_path):
    w.device = {}
    rep = _run(w, tmp_path)
    s = _step(rep, "device key")
    assert w.enrolled == ["rid-1"]
    assert s.status == au.NEEDS and s.fix.endswith(f"{BASE}/runners/approve/dk-2")


def test_no_runner_app_and_an_old_server_says_update_pending(w, tmp_path):
    # canopy-web gives a computer with no runner app its own runner id at registration;
    # until it does, there is nothing to enroll against — say so, no workaround.
    w.device, w.runner_id = {}, ""
    rep = _run(w, tmp_path)
    s = _step(rep, "device key")
    assert s.status == au.NEEDS and "can't hold a vault key yet — canopy-web update pending" in s.fix
    assert w.enrolled == [] and w.registered == [True]     # asked once more for a runner id


def test_no_runner_app_enrolls_against_the_computers_own_runner_id(w, tmp_path):
    w.device, w.runner_id = {}, ""
    w.registration = {"id": "pr-1", "name": "aath-mbp", "runner_id": "comp-9"}
    rep = _run(w, tmp_path)
    assert w.enrolled == ["comp-9"] and w.registered == []
    assert _step(rep, "device key").fix.endswith(f"{BASE}/runners/approve/dk-2")


def test_an_old_registration_picks_up_the_computers_runner_id(w, tmp_path):
    w.device, w.runner_id = {}, ""
    regs = iter([{"id": "pr-1", "name": "aath-mbp"},
                 {"id": "pr-1", "name": "aath-mbp", "runner_id": "comp-9"}])
    state = {"reg": next(regs)}

    def register():
        w.registered.append(True)
        state["reg"] = next(regs)
        return state["reg"]

    d = w.deps()
    d.read_registration = lambda: state["reg"]
    d.register_computer = register
    au.run("eva", str(tmp_path), d)
    assert w.registered == [True] and w.enrolled == ["comp-9"]


def test_a_pending_device_key_points_at_its_approve_url(w, tmp_path):
    w.device.update(status="pending", approve_url=f"{BASE}/approve/dk-1")
    s = _step(_run(w, tmp_path), "device key")
    assert s.status == au.NEEDS and s.fix.endswith(f"{BASE}/approve/dk-1")


def test_an_active_key_with_no_ecdh_key_enrolls_again_to_add_one(w, tmp_path):
    # A Windows TPM that would not make an ECDH key enrolled without one (DIM-SI-W021,
    # 2026-10-26): approved, but no vault key could ever be wrapped to it.
    del w.device["ecdh_jkt"]
    d = w.deps()
    d.enroll_device = lambda rid: (w.enrolled.append(rid) or
                                   {**w.device, "ecdh_jkt": "ec-new",
                                    "approve_url": f"{BASE}/runners/approve/dk-1"})
    rep = au.run("eva", str(tmp_path), d)
    s = _step(rep, "device key")
    assert w.enrolled == ["rid-1"]
    assert s.status == au.NEEDS and s.fix.endswith(f"{BASE}/runners/approve/dk-1")


def test_an_active_key_that_still_gets_no_ecdh_key_says_reenroll_software(w, tmp_path):
    del w.device["ecdh_jkt"]
    d = w.deps()
    d.enroll_device = lambda rid: {**w.device, "ecdh_note": "the TPM would not make one"}
    s = _step(au.run("eva", str(tmp_path), d), "device key")
    assert s.status == au.NEEDS and "the TPM would not make one" in s.detail
    assert "--reenroll --kind software" in s.fix


def test_a_member_whose_device_is_not_ready_is_not_sent_to_grant_pending_yet(w, tmp_path):
    del w.device["ecdh_jkt"]
    w.zk, w.zk_error = None, CanopyError("GET /api/agents/eva/vault/zk -> 403: forbidden")
    d = w.deps()
    d.enroll_device = lambda rid: {**w.device, "ecdh_note": "no"}
    s = _step(au.run("eva", str(tmp_path), d), "vault access")
    assert "once the device key above is approved" in s.detail


def test_a_revoked_device_key_says_reenroll(w, tmp_path):
    w.device["status"] = "revoked"
    s = _step(_run(w, tmp_path), "device key")
    assert s.status == au.NEEDS and "--reenroll" in s.fix


# ── vault access ──────────────────────────────────────────────────────────────

def test_no_grant_and_nobody_to_pass_it_on_asks_the_owner_to_press_grant_pending(w, tmp_path):
    w.zk["this_device"] = "this device holds no wrap of 'eva's vault key at kv 1"
    rep = _run(w, tmp_path)
    s = _step(rep, "vault access")
    assert s.status == au.NEEDS
    assert s.fix == f"ask jj@dimagi.com (its owner) to press Grant pending on {BASE}/agents/eva"
    assert _step(rep, "env").status == au.SKIP and w.synced == []


def test_no_grant_with_a_keeper_asks_nobody(w, tmp_path):
    w.zk["this_device"] = "no wrap"
    w.holders = {"has_key": True, "people": ["jj@dimagi.com"], "keepers": ["cloud-ec2-live"]}
    s = _step(_run(w, tmp_path), "vault access")
    assert s.fix.startswith("nothing to do: a keeper (cloud-ec2-live) passes eva's key")
    assert "ask " not in s.fix and "Grant pending" not in s.fix


def test_no_grant_without_a_keeper_says_the_admins_are_emailed(w, tmp_path):
    w.zk["this_device"] = "no wrap"
    w.holders = {"has_key": True, "people": ["jj@dimagi.com"], "keepers": []}
    s = _step(_run(w, tmp_path), "vault access")
    assert "an admin who holds it grants it (jj@dimagi.com, emailed after 15 minutes)" in s.fix


def test_an_older_canopy_web_without_the_members_list_falls_back_to_asking(w, tmp_path):
    w.zk["this_device"] = "no wrap"
    w.web_fail["/api/agents/eva/vault/members"] = CanopyError("404")
    s = _step(_run(w, tmp_path), "vault access")
    assert s.fix == f"ask jj@dimagi.com (its owner) to press Grant pending on {BASE}/agents/eva"


def test_no_grant_but_you_hold_one_says_press_it_yourself(w, tmp_path):
    w.zk["this_device"] = "no wrap"
    w.zk["wraps"] = [{"kind": "member", "target": 7, "kv": 1}]
    s = _step(_run(w, tmp_path), "vault access")
    assert s.fix.startswith(f"press Grant pending on {BASE}/agents/eva (you hold a grant")


def test_no_grant_before_the_device_is_approved_says_so(w, tmp_path):
    w.zk["this_device"] = "no wrap"
    w.device.update(status="pending", approve_url="u")
    s = _step(_run(w, tmp_path), "vault access")
    assert s.fix.startswith("once the device key is approved, ")


def test_a_device_with_a_grant_offers_to_grant_the_others(w, tmp_path):
    w.zk["pending"] = ["device dk-9 (bob's mac)"]
    s = _step(_run(w, tmp_path), "vault access")
    assert s.status == au.OK and "`canopy vault zk grant --agent eva`" in s.detail


def test_a_non_admin_cannot_see_the_vault_state(w, tmp_path):
    w.zk_error = CanopyError("canopy-web refused GET /api/agents/eva/vault/zk: -> 403: admins only")
    rep = _run(w, tmp_path)
    s = _step(rep, "vault access")
    assert s.status == au.SKIP and "Grant pending" in s.detail
    assert _step(rep, "secrets").detail == "canopy vault"


def test_a_non_admin_with_a_keeper_is_told_to_wait_not_to_ask(w, tmp_path):
    w.zk_error = CanopyError("canopy-web refused GET /api/agents/eva/vault/zk: -> 403: admins only")
    w.holders = {"has_key": True, "people": ["jj@dimagi.com"], "keepers": ["cloud-ec2-live"]}
    s = _step(_run(w, tmp_path), "vault access")
    assert "nothing to do: a keeper (cloud-ec2-live)" in s.detail and "Grant pending" not in s.detail


def test_born_zero_knowledge_has_nothing_to_grant(w, tmp_path):
    w.zk.update(kv=0, this_device="no wrap")
    s = _step(_run(w, tmp_path), "vault access")
    assert s.status == au.OK and "kv 0" in s.detail


def test_a_custodial_canopy_vault_needs_no_device_key(w, tmp_path):
    w.zk = {"enabled": False, "kv": 0}
    w.device = {}
    rep = _run(w, tmp_path)
    assert _step(rep, "device key").status == au.SKIP and w.enrolled == []
    assert _step(rep, "vault access").status == au.OK


# ── an older server still sends credential_source ─────────────────────────────

def test_an_old_servers_credential_source_field_is_ignored(w, tmp_path):
    w.access = {"credential_source": "legacy-backend", "may_resolve": False, "via": None}
    rep = _run(w, tmp_path)
    assert _step(rep, "vault access").status == au.OK
    assert "legacy" not in " ".join(x.detail for x in rep.steps).lower()


# ── licence ───────────────────────────────────────────────────────────────────

def test_an_admin_needs_no_licence_and_syncs_now(w, tmp_path):
    w.access.update(may_resolve=True, via="admin")
    w.licenses = []
    rep = _run(w, tmp_path)
    assert "admin" in _step(rep, "licence").detail
    assert w.synced == ["eva"] and _step(rep, "env").status == au.DONE


@pytest.mark.parametrize("row,status,text", [
    (None, au.OK, "requested automatically when your first session opens"),
    ({"user_id": 7, "status": "requested"}, au.NEEDS, "approve your licence"),
    ({"email": "AATH@dimagi.com", "status": "revoked", "revoked_by_email": "jj@dimagi.com"},
     au.NEEDS, "restore your licence"),
    ({"user_id": 7, "status": "active", "basis": "editor"}, au.OK, "active (editor)"),
])
def test_licence_states(w, tmp_path, row, status, text):
    w.licenses = [{"user_id": 99, "email": "other@x", "status": "active"}] + ([row] if row else [])
    s = _step(_run(w, tmp_path), "licence")
    assert s.status == status and text in (s.fix or s.detail)


def test_a_licence_holder_syncs_inside_the_session_not_now(w, tmp_path):
    s = _step(_run(w, tmp_path), "env")
    assert s.status == au.OK and "when each session starts" in s.detail and w.synced == []


# ── plugin ────────────────────────────────────────────────────────────────────

def test_a_missing_plugin_is_installed_from_the_catalog(w, tmp_path):
    w.installed = {"canopy"}
    s = _step(_run(w, tmp_path), "plugin")
    assert s.status == au.DONE and w.installs == ["eva"]


def test_a_failed_install_prints_the_exact_line(w, tmp_path):
    w.installed = set()
    w.install_result = (False, "repository not found")
    s = _step(_run(w, tmp_path), "plugin")
    assert s.status == au.NEEDS and "`/plugin install eva@canopy-agents`" in s.fix


def test_no_catalog_says_run_setup_first(w, tmp_path):
    w.installed, w.known = set(), set()
    s = _step(_run(w, tmp_path), "plugin")
    assert s.status == au.NEEDS and s.fix.startswith("run /canopy:setup") and w.installs == []


def test_the_plugin_name_comes_from_the_agent_record(w, tmp_path):
    w.agent["plugin"] = {"name": "ace"}
    w.installed = {"ace"}
    rep = _run(w, tmp_path)
    assert rep.end_line().endswith("run /ace:turn")


# ── this project / env ────────────────────────────────────────────────────────

def test_an_already_set_project_is_ok(w, tmp_path):
    w.agent_changed = False
    assert _step(_run(w, tmp_path), "this project").status == au.OK


def test_a_sync_failure_needs_you_with_the_refresh_line(w, tmp_path):
    from orchestrator.cred_cli import CredError
    w.access.update(may_resolve=True, via="admin")
    w.sync_error = CredError("could not open the zero-knowledge answer")
    s = _step(_run(w, tmp_path), "env")
    assert s.status == au.NEEDS and "canopy cred refresh --agent eva" in s.fix


def test_needs_you_list_and_exit_code_from_the_cli(w, tmp_path, monkeypatch):
    from orchestrator import agent_cli
    w.licenses = [{"user_id": 7, "status": "requested"}]
    monkeypatch.setattr(au, "real_deps", w.deps)
    r = CliRunner().invoke(agent_cli.agent, ["use", "eva", "--dir", str(tmp_path)])
    assert r.exit_code == 1
    assert "NEEDS YOU:\n  - licence: ask jj@dimagi.com (its owner) to approve" in r.output
    assert "Then run `canopy agent use eva` again." in r.output


def test_cli_json(w, tmp_path, monkeypatch):
    import json

    from orchestrator import agent_cli
    monkeypatch.setattr(au, "real_deps", w.deps)
    r = CliRunner().invoke(agent_cli.agent, ["use", "eva", "--dir", str(tmp_path), "--json"])
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["ready"] is True and body["next"].endswith("/eva:turn")


# ── credentials someone delegated to the agent ───────────────────────────────

GITHUB_WITHHELD = {"name": "github-token", "provider": "github",
                   "label": "GitHub · delegated by Jonathan Jackson",
                   "delegated_by_email": "jjackson@dimagi.com"}


def test_a_delegated_github_token_is_said_to_be_someone_elses_and_yours_is_used(w, tmp_path):
    w.access = {**w.access, "delegated_withheld": [GITHUB_WITHHELD]}
    deps = w.deps()
    deps.github_login = lambda: "sam"
    rep = au.run("eva", str(tmp_path), deps)
    step = _step(rep, "GitHub")
    assert step.status == au.OK
    assert "jjackson@dimagi.com's and is not given to you" in step.detail and "@sam" in step.detail
    assert not rep.needs


def test_no_github_sign_in_here_is_a_needs_you_with_the_command(w, tmp_path):
    w.access = {**w.access, "delegated_withheld": [GITHUB_WITHHELD]}
    rep = _run(w, tmp_path)
    step = _step(rep, "GitHub")
    assert step.status == au.NEEDS and step.fix.startswith("run `gh auth login`")


def test_nothing_delegated_adds_no_step(w, tmp_path):
    assert "GitHub" not in [s.name for s in _run(w, tmp_path).steps]


# ── google login (2026-10-28, failure investigation 17, turn c10c9748) ─────────
# `canopy agent use` had no Google step: a licensee's first ace turn died on gog's missing
# OAuth client file, and the fix it printed was an admin's `canopy agent bootstrap`.

def test_an_admin_whose_google_login_is_in_place_is_ok(w, tmp_path):
    w.access.update(may_resolve=True, via="admin")
    s = _step(_run(w, tmp_path), "google login")
    assert s.status == au.OK and "(present)" in s.detail
    assert w.google_logins == ["eva"]


def test_an_admin_gets_the_client_file_and_token_imported(w, tmp_path):
    w.access.update(may_resolve=True, via="admin")
    w.google_result = {"ok": True, "imported": True, "wrote_client": True,
                       "summary": "canopy-web canopy 3d (imported), gmail LIVE (canopy)",
                       "notes": ["token: wrote credentials-canopy.json from the canopy vault"]}
    rep = _run(w, tmp_path)
    s = _step(rep, "google login")
    assert s.status == au.DONE and "(imported)" in s.detail and not rep.needs


def test_a_failed_import_needs_you_and_an_admin_sees_why(w, tmp_path):
    w.access.update(may_resolve=True, via="admin")
    w.google_result = {"ok": False, "summary": "canopy-web canopy 3d, gmail -",
                       "notes": ["token: cannot read OAuth client 'canopy' from the canopy vault"]}
    s = _step(_run(w, tmp_path), "google login")
    assert s.status == au.NEEDS and "cannot read OAuth client" in s.fix


def test_a_licence_holder_is_told_the_session_imports_it(w, tmp_path):
    # Outside a turn canopy-web will not resolve for a licence holder (may_resolve False).
    s = _step(_run(w, tmp_path), "google login")
    assert s.status == au.OK and "first session starts" in s.detail
    assert w.google_logins == []


def test_no_gog_needs_you_with_the_install_line_for_this_os(w, tmp_path):
    w.google.update(gog=False, install="download gogcli_<version>_windows_amd64.zip from …")
    rep = _run(w, tmp_path)
    s = _step(rep, "google login")
    assert s.status == au.NEEDS and "windows_amd64.zip" in s.fix and "brew" not in s.fix
    assert "google login" in rep.end_line()


def test_blocked_by_an_unapproved_device_is_skipped(w, tmp_path):
    w.device.update(status="pending", approve_url=f"{BASE}/approve/dk-1")
    w.access.update(may_resolve=True, via="admin")
    s = _step(_run(w, tmp_path), "google login")
    assert s.status == au.SKIP and "device key" in s.detail and w.google_logins == []


def test_a_canopy_hosted_agent_needs_no_google_login(w, tmp_path):
    w.access.update(may_resolve=True, via="admin")
    w.google.update(hosted="bailey@marshellis.com is a mailbox canopy-web hosts")
    s = _step(_run(w, tmp_path), "google login")
    assert s.status == au.SKIP and "canopy-web hosts" in s.detail and w.google_logins == []


def test_no_fix_ever_tells_a_non_admin_to_sign_in_as_the_agent(w, tmp_path):
    # Defensive: a non-admin reaching the import (a licence read) gets "ask the owner",
    # never `gog login <agent>` or "Connect Google mailbox".
    w.access.update(may_resolve=True, via="license")
    w.google_result = {"ok": False, "summary": "NONE",
                       "notes": ["token: no gog token on canopy-web for eva — connect it on "
                                 "canopy-web: … Connect Google mailbox"]}
    s = _step(_run(w, tmp_path), "google login")
    assert s.status == au.NEEDS and "ask jj@dimagi.com" in s.fix
    assert "Connect Google mailbox" not in s.fix and "gog login" not in s.fix


def test_the_real_google_check_names_the_os_install_line(monkeypatch, tmp_path):
    import shutil

    from orchestrator import agent_bootstrap, agent_email
    monkeypatch.setattr(agent_email, "find_agent_repo", lambda slug: tmp_path)
    monkeypatch.setattr(agent_bootstrap, "hosted_identity", lambda repo: None)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(agent_email.sys, "platform", "win32")
    out = au._google_check("ace")
    assert out["gog"] is False and "windows_amd64.zip" in out["install"]
    assert "brew" not in out["install"]


def test_google_login_cli_if_needed_touches_nothing_when_present(monkeypatch, tmp_path):
    from orchestrator import agent_bootstrap, agent_cli, agent_email
    monkeypatch.setattr(agent_email, "find_agent_repo", lambda slug: tmp_path)
    monkeypatch.setattr(agent_bootstrap, "google_login_present", lambda repo, gog_dir=None: "canopy")
    monkeypatch.setattr(agent_bootstrap.Bootstrapper, "google_login",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no import")))
    r = CliRunner().invoke(agent_cli.agent, ["google-login", "--slug", "ace", "--if-needed"])
    assert r.exit_code == 0 and "present here (client canopy)" in r.output


def test_google_login_cli_without_gog_names_the_install_line(monkeypatch, tmp_path):
    import shutil

    from orchestrator import agent_bootstrap, agent_cli, agent_email
    monkeypatch.setattr(agent_email, "find_agent_repo", lambda slug: tmp_path)
    monkeypatch.setattr(agent_bootstrap, "google_login_present", lambda repo, gog_dir=None: "")
    monkeypatch.setattr(agent_bootstrap, "hosted_identity", lambda repo: None)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    r = CliRunner().invoke(agent_cli.agent, ["google-login", "--slug", "ace", "--if-needed"])
    assert r.exit_code == 1 and "gog is not installed" in r.output
