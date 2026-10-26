"""`canopy agent use <slug>` — use an agent yourself in Claude Code, no runner needed
(Jonathan, 2026-10-21). Every step is faked through `agent_use.Deps`; each branch says
ok / done / NEEDS YOU with the exact URL, command or person."""
from __future__ import annotations

from types import SimpleNamespace

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
        self.access = {"credential_source": "canopy-web", "may_resolve": False,
                       "via": None, "error": ""}
        self.device = {"id": "dk-1", "jkt": "abcdef0123456789xyz", "status": "active",
                       "kind": "secure-enclave", "runner": "rid-1"}
        self.runner_id = "rid-1"
        self.enrolled = []
        self.zk = {"enabled": True, "kv": 1, "wraps": [], "pending": [],
                   "this_device": "holds a wrap at the current kv"}
        self.zk_error = None
        self.verdict = SimpleNamespace(allowed=True, reason="1Password (user) can read vault 'V'",
                                       message="")
        self.installed = {"eva", "canopy"}
        self.known = {"canopy-agents", "canopy"}
        self.install_result = (True, "installed")
        self.installs = []
        self.agent_set = []
        self.agent_changed = True
        self.synced = []
        self.sync_result = "re-injected from canopy-web"
        self.sync_error = None

    def web(self, method, path, body=None):
        if path in self.web_fail:
            raise self.web_fail[path]
        if path == "/api/me/":
            return self.me
        if path == "/api/agents/eva/":
            return self.agent
        if path == "/api/agents/eva/licenses":
            return self.licenses
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

        return au.Deps(
            base_url=lambda: BASE, web=self.web,
            read_registration=lambda: self.registration, register_computer=register,
            access=lambda slug: self.access, device_status=lambda: dict(self.device),
            machine_runner_id=lambda: self.runner_id, enroll_device=enroll,
            refresh_device=lambda rid: dict(self.device), zk_status=zk_status,
            decide_1password=lambda slug: self.verdict,
            installed_plugins=lambda: self.installed, known_marketplaces=lambda: self.known,
            install_plugin=install, set_session_agent=set_agent, sync_env=sync)


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
                                           "this project", "env"]
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


def test_a_revoked_device_key_says_reenroll(w, tmp_path):
    w.device["status"] = "revoked"
    s = _step(_run(w, tmp_path), "device key")
    assert s.status == au.NEEDS and "--reenroll" in s.fix


# ── vault access ──────────────────────────────────────────────────────────────

def test_no_grant_asks_the_owner_to_press_grant_pending(w, tmp_path):
    w.zk["this_device"] = "this device holds no wrap of 'eva's vault key at kv 1"
    rep = _run(w, tmp_path)
    s = _step(rep, "vault access")
    assert s.status == au.NEEDS
    assert s.fix == f"ask jj@dimagi.com (its owner) to press Grant pending on {BASE}/agents/eva"
    assert _step(rep, "env").status == au.SKIP and w.synced == []


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


# ── legacy 1Password ──────────────────────────────────────────────────────────

def test_a_1password_agent_is_marked_legacy_with_the_move_line(w, tmp_path):
    w.access = {"credential_source": "1password", "may_resolve": False, "via": None}
    rep = _run(w, tmp_path)
    s = _step(rep, "secrets")
    assert "LEGACY" in s.detail and "canopy vault import --agent eva --zk --switch" in s.detail
    assert _step(rep, "vault access").status == au.OK
    assert "device key" not in [x.name for x in rep.steps]
    assert w.synced == []


def test_a_1password_agent_you_cannot_read_needs_you(w, tmp_path):
    w.access = {"credential_source": "1password"}
    w.verdict = SimpleNamespace(allowed=False, message="ask the vault owner to share 'V'")
    s = _step(_run(w, tmp_path), "vault access")
    assert s.status == au.NEEDS and s.fix == "ask the vault owner to share 'V'"


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
