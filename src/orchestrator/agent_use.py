"""`canopy agent use <slug>` — use an agent yourself, in Claude Code, on this computer.

There are two ways to run an agent (docs/running-an-agent.md):

1. **Use it yourself in Claude Code** — no runner needed. Run this command once on your
   computer; then a Claude Code session in the project starts the agent's turn.
2. **Let it run on its own** — a runner: your own Mac's runner app (set Private, it does
   only your work) or a fleet cloud box.

This module is way 1, in ONE idempotent command. Each step is checked, fixed when canopy
can fix it, and otherwise reported as NEEDS YOU with the exact URL, command or person:

  signed in        your canopy-web token works (`GET /api/me/`)
  agent            you can see the agent (`GET /api/agents/<slug>/`)
  this computer    registered for local use (`local_cli.register_this_computer`; canopy-web
                   calls the record a "private runner" — an internal word, never shown)
  secrets          where the agent's secrets live: canopy vault, or 1Password (LEGACY)
  device key       canopy vault opens only on an approved device key (`canopy vault device`)
  vault access     this device holds a grant of the agent's zero-knowledge vault key
  licence          your licence to use the agent locally (`GET /api/agents/<slug>/licenses`)
  plugin           the agent's plugin is installed (`<plugin>@canopy-agents`)
  this project     sessions in --dir use the agent (`$CANOPY_AGENT`)
  env              the agent's env synced from its vault (`canopy cred sync`'s path)

and ends with one line: start a session and run `/<plugin>:turn`, or the NEEDS YOU list.

Everything that touches the world goes through `Deps`, so every branch is testable with
fakes (tests/test_agent_use.py). Nothing here prints a secret.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

OK, DONE, NEEDS, SKIP = "ok", "done", "NEEDS YOU", "skip"
CATALOG = "canopy-agents"


@dataclass
class Step:
    name: str
    status: str
    detail: str
    #: For NEEDS YOU: the one thing to do (a URL, a command, a person).
    fix: str = ""


@dataclass
class UseReport:
    slug: str
    directory: str
    plugin: str = ""
    steps: list[Step] = field(default_factory=list)

    @property
    def needs(self) -> list[Step]:
        return [s for s in self.steps if s.status == NEEDS]

    def add(self, name: str, status: str, detail: str, fix: str = "") -> Step:
        step = Step(name, status, detail, fix)
        self.steps.append(step)
        return step

    def end_line(self) -> str:
        if not self.needs:
            return (f"Start a new Claude Code session in {self.directory} and run "
                    f"/{self.plugin or self.slug}:turn")
        lines = ["NEEDS YOU:"]
        lines += [f"  - {s.name}: {s.fix or s.detail}" for s in self.needs]
        lines.append(f"Then run `canopy agent use {self.slug}` again.")
        return "\n".join(lines)

    def render(self) -> str:
        width = max([len(s.name) for s in self.steps] + [12])
        tag = {OK: "ok", DONE: "done", NEEDS: "NEEDS YOU", SKIP: "skip"}
        lines = [f"  [{tag.get(s.status, s.status):<9}] {s.name.ljust(width)}  {s.detail}"
                 for s in self.steps]
        return "\n".join(lines + ["", self.end_line()])

    def as_dict(self) -> dict:
        return {"agent": self.slug, "dir": self.directory, "plugin": self.plugin,
                "ready": not self.needs,
                "steps": [s.__dict__ for s in self.steps], "next": self.end_line()}


@dataclass
class Deps:
    """The world, injectable. Defaults are built by `real_deps()`."""
    base_url: Callable[[], str]
    web: Callable[..., object]                       # (method, path) -> body; raises
    read_registration: Callable[[], dict]
    register_computer: Callable[[], dict]
    access: Callable[[str], dict]                    # cred_cli.fetch_access
    device_status: Callable[[], dict]                # device_key.read_status
    machine_runner_id: Callable[[], str]
    enroll_device: Callable[[str], dict]             # runner_id -> status
    refresh_device: Callable[[str], dict]            # runner_id -> status
    zk_status: Callable[[str], dict]                 # vault_zk_cli.run_status
    decide_1password: Callable[[str], object]        # cred_cli.decide -> Verdict
    installed_plugins: Callable[[], Optional[set]]
    known_marketplaces: Callable[[], set]
    install_plugin: Callable[[str], tuple]           # plugin -> (ok, last line)
    set_session_agent: Callable[[str, str], bool]
    sync_env: Callable[[str], str]


def _first_line(e: BaseException) -> str:
    text = str(e).strip()
    return (text.splitlines()[0] if text else type(e).__name__)[:300]


def _is_denied(e: BaseException) -> bool:
    text = str(e)
    return any(f"-> {c}" in text or f" {c}:" in text for c in ("403", "404"))


def _owner(agent: dict) -> str:
    o = agent.get("owner")
    if isinstance(o, dict):
        return str(o.get("email") or o.get("name") or "")
    return str(agent.get("owner_email") or o or "")


def run(slug: str, directory: str, deps: Deps) -> UseReport:
    directory = str(Path(directory).expanduser().resolve())
    rep = UseReport(slug=slug, directory=directory)
    base = deps.base_url().rstrip("/")
    page = f"{base}/agents/{slug}"

    # 1. signed in ────────────────────────────────────────────────────────────────
    try:
        me = deps.web("GET", "/api/me/") or {}
    except Exception as e:  # noqa: BLE001 — every failure here is "not signed in"
        rep.add("signed in", NEEDS, f"no working canopy-web sign-in here ({_first_line(e)})",
                "run /canopy:setup in Claude Code (it signs you in to canopy-web in the browser)")
        return rep
    my_email = str(me.get("email") or "")
    my_id = str(me.get("id") or me.get("user_id") or "")
    rep.add("signed in", OK, f"as {my_email or 'you'} on {base}")

    # 2. the agent ────────────────────────────────────────────────────────────────
    try:
        agent = deps.web("GET", f"/api/agents/{slug}/") or {}
    except Exception as e:  # noqa: BLE001
        if _is_denied(e):
            rep.add("agent", NEEDS, f"you cannot see agent '{slug}' on {base}",
                    f"ask a workspace admin to add you to {slug}'s workspace (or check the slug)")
        else:
            rep.add("agent", NEEDS, f"could not read agent '{slug}' ({_first_line(e)})",
                    "check your connection to canopy-web, then re-run")
        return rep
    plugin = str(((agent.get("plugin") or {}) if isinstance(agent.get("plugin"), dict) else {})
                 .get("name") or slug)
    rep.plugin = plugin
    owner = _owner(agent)
    ask = f"{owner} (its owner)" if owner else f"an admin of {slug}"
    ws = agent.get("workspace")
    ws = ws.get("slug") if isinstance(ws, dict) else ws
    rep.add("agent", OK, f"{agent.get('name') or slug}" + (f" in workspace {ws}" if ws else ""))

    # 3. this computer ────────────────────────────────────────────────────────────
    reg = deps.read_registration()
    if reg and not reg.get("runner_id") and not deps.machine_runner_id():
        # Registering is idempotent; a newer canopy-web answers with the computer's own
        # runner id (a "computer" runner: private, never claims), which its device key
        # enrolls against. Ask again so an older registration picks it up.
        try:
            reg = deps.register_computer() or reg
        except Exception:  # noqa: BLE001 — the old registration still stands
            pass
    if reg:
        rep.add("this computer", OK, f"registered for local use as '{reg.get('name')}'")
    else:
        try:
            reg = deps.register_computer()
            rep.add("this computer", DONE, f"registered for local use as '{reg.get('name')}'")
        except Exception as e:  # noqa: BLE001
            rep.add("this computer", NEEDS, f"could not register this computer ({_first_line(e)})",
                    "re-run once canopy-web is reachable")

    # 4. where the secrets live ───────────────────────────────────────────────────
    access = deps.access(slug) or {}
    source = access.get("credential_source") or "canopy-web"
    via = access.get("via")
    is_admin = via in ("admin", "runner")
    if source == "canopy-web":
        _canopy_vault_steps(rep, slug, deps, access, page=page, ask=ask, my_id=my_id,
                            is_admin=is_admin)
    else:
        rep.add("secrets", OK, f"1Password (LEGACY) — an admin with 1Password access moves it "
                               f"to canopy vault: `canopy vault import --agent {slug} --zk --switch`")
        try:
            v = deps.decide_1password(slug)
            if getattr(v, "allowed", False):
                rep.add("vault access", OK, getattr(v, "reason", "") or "your 1Password can read it")
            else:
                rep.add("vault access", NEEDS, "your 1Password cannot read its vault",
                        getattr(v, "message", "") or f"ask {ask} for access")
        except Exception as e:  # noqa: BLE001
            rep.add("vault access", NEEDS, f"could not check 1Password ({_first_line(e)})",
                    "sign in to the 1Password CLI (`op signin`), then re-run")

    # 5. licence ──────────────────────────────────────────────────────────────────
    if is_admin:
        rep.add("licence", OK, f"you are an admin of {slug} — no licence needed")
    else:
        _licence_step(rep, slug, deps, page=page, ask=ask, my_email=my_email, my_id=my_id)

    # 6. plugin ───────────────────────────────────────────────────────────────────
    _plugin_step(rep, plugin, deps)

    # 7. this project ─────────────────────────────────────────────────────────────
    try:
        changed = deps.set_session_agent(slug, directory)
        rep.add("this project", DONE if changed else OK,
                f"sessions in {directory} use {slug} ($CANOPY_AGENT)")
    except Exception as e:  # noqa: BLE001
        rep.add("this project", NEEDS, f"could not set $CANOPY_AGENT ({_first_line(e)})",
                f"add {{\"env\": {{\"CANOPY_AGENT\": \"{slug}\"}}}} to "
                f"{directory}/.claude/settings.local.json")

    # 8. env ──────────────────────────────────────────────────────────────────────
    _env_step(rep, slug, deps, source=source, may_resolve=bool(access.get("may_resolve")))
    return rep


def _canopy_vault_steps(rep: UseReport, slug: str, deps: Deps, access: dict, *, page: str,
                        ask: str, my_id: str, is_admin: bool) -> None:
    zk: Optional[dict] = None
    zk_err: Optional[BaseException] = None
    try:
        zk = deps.zk_status(slug)
    except Exception as e:  # noqa: BLE001 — a non-admin may not read the vault state
        zk_err = e
    if zk is not None and zk.get("enabled"):
        rep.add("secrets", OK, f"canopy vault (zero-knowledge, kv {zk.get('kv')})")
    elif zk is not None:
        rep.add("secrets", OK, "canopy vault (canopy-web holds the values)")
    else:
        rep.add("secrets", OK, "canopy vault")

    # device key ───────────────────────────────────────────────────────────────
    if zk is not None and not zk.get("enabled"):
        rep.add("device key", SKIP, "not needed (the values are not zero-knowledge)")
        rep.add("vault access", OK, "nothing to grant (not zero-knowledge)")
        return
    st = deps.device_status() or {}
    device_ok = False
    # The runner app's id (~/.canopy/runner.json), else this computer's own runner id from
    # its registration (canopy-web gives a computer with no runner app one, for its key).
    runner_id = (st.get("runner") or deps.machine_runner_id()
                 or str(deps.read_registration().get("runner_id") or ""))
    if st.get("jkt") and st.get("status") == "pending" and runner_id:
        try:
            st = deps.refresh_device(runner_id) or st
        except Exception:  # noqa: BLE001 — the cached status is still an answer
            pass
    if not st.get("jkt") or not st.get("id"):
        if not runner_id:
            rep.add("device key", NEEDS,
                    "this computer can't hold a vault key yet — canopy-web update pending "
                    "(registering it for local use returned no runner id to enroll a "
                    "device key against)",
                    "this computer can't hold a vault key yet — canopy-web update pending; "
                    "re-run `canopy agent use` after it ships")
        else:
            try:
                st = deps.enroll_device(runner_id) or {}
                if st.get("status") == "active":
                    device_ok = True
                    rep.add("device key", DONE, f"enrolled ({st.get('kind') or 'key'}, active)")
                else:
                    rep.add("device key", NEEDS, "enrolled; waiting for approval",
                            f"approve it with your passkey: {st.get('approve_url') or page}")
            except Exception as e:  # noqa: BLE001
                rep.add("device key", NEEDS, f"could not enroll ({_first_line(e)})",
                        "`canopy vault device --enroll`")
    elif st.get("status") == "active":
        device_ok = True
        rep.add("device key", OK, f"{st.get('kind') or 'key'} {str(st['jkt'])[:12]}…, approved")
    elif st.get("status") == "pending":
        rep.add("device key", NEEDS, "enrolled; waiting for approval",
                f"approve it with your passkey: {st.get('approve_url') or page}")
    else:
        rep.add("device key", NEEDS, f"this computer's device key is '{st.get('status')}'",
                "`canopy vault device --reenroll`, then approve it")

    # vault access ─────────────────────────────────────────────────────────────
    if zk is None:
        if zk_err is not None and _is_denied(zk_err):
            rep.add("vault access", SKIP,
                    f"only an admin of {slug} can see which devices hold a grant — if your "
                    f"first session says this device has none, ask {ask} to press "
                    f"Grant pending on {page}")
        else:
            rep.add("vault access", SKIP, f"could not read {slug}'s vault state "
                                          f"({_first_line(zk_err) if zk_err else 'no answer'})")
        return
    if str(zk.get("kv")) == "0":
        rep.add("vault access", OK, "zero-knowledge from birth (kv 0): each value is sealed to "
                                    "every approved device as it is set")
        return
    pending = zk.get("pending") or []
    if str(zk.get("this_device") or "").startswith("holds a wrap"):
        detail = f"this device holds a grant (kv {zk.get('kv')})"
        if pending:
            detail += (f"; {len(pending)} other device(s)/people wait for one — grant them: "
                       f"`canopy vault zk grant --agent {slug}`")
        rep.add("vault access", OK, detail)
        return
    i_hold_member_wrap = bool(my_id) and any(
        w.get("kind") == "member" and str(w.get("target")) == my_id for w in zk.get("wraps") or [])
    who = ("press Grant pending on " + page + " (you hold a grant; your passkey unlocks it)"
           if i_hold_member_wrap else f"ask {ask} to press Grant pending on {page}")
    if not device_ok:
        who = "once the device key is approved, " + who
    rep.add("vault access", NEEDS, "this device holds no grant of the vault key yet", who)


def _licence_step(rep: UseReport, slug: str, deps: Deps, *, page: str, ask: str,
                  my_email: str, my_id: str) -> None:
    try:
        rows = deps.web("GET", f"/api/agents/{slug}/licenses") or []
    except Exception as e:  # noqa: BLE001
        rep.add("licence", SKIP, f"could not read licences ({_first_line(e)})")
        return
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    mine = next((r for r in rows if isinstance(r, dict) and (
        (my_id and str(r.get("user_id")) == my_id)
        or (my_email and str(r.get("email") or "").lower() == my_email.lower()))), None)
    if mine is None:
        rep.add("licence", OK, f"requested automatically when your first session opens — "
                               f"granted at once if {slug}'s caller list gives you the whole "
                               f"agent, otherwise an admin approves it on {page}")
    elif mine.get("status") == "active":
        rep.add("licence", OK, "active" + (f" ({mine['basis']})" if mine.get("basis") else ""))
    elif mine.get("status") == "requested":
        rep.add("licence", NEEDS, "requested, not approved yet",
                f"ask {ask} to approve your licence on {page}")
    else:
        by = mine.get("revoked_by_email")
        rep.add("licence", NEEDS, "revoked" + (f" by {by}" if by else ""),
                f"ask {ask} to restore your licence on {page}")


def _plugin_step(rep: UseReport, plugin: str, deps: Deps) -> None:
    line = f"/plugin install {plugin}@{CATALOG}"
    installed = deps.installed_plugins()
    if installed is not None and plugin in installed:
        rep.add("plugin", OK, f"{plugin} installed")
        return
    try:
        known = deps.known_marketplaces()
    except Exception:  # noqa: BLE001
        known = set()
    if CATALOG not in known:
        rep.add("plugin", NEEDS, f"{plugin} is not installed and your agent catalog is not set up",
                f"run /canopy:setup in Claude Code, then `{line}`")
        return
    try:
        ok, last = deps.install_plugin(plugin)
    except Exception as e:  # noqa: BLE001 — no `claude` on PATH, a timeout
        ok, last = False, _first_line(e)
    if ok:
        rep.add("plugin", DONE, f"installed {plugin}@{CATALOG}")
    else:
        rep.add("plugin", NEEDS, f"could not install {plugin} ({last or 'no output'})",
                f"run `{line}` in Claude Code (it needs your GitHub read access to "
                f"{plugin}'s repo)")


def _env_step(rep: UseReport, slug: str, deps: Deps, *, source: str, may_resolve: bool) -> None:
    blocked = [s.name for s in rep.needs if s.name in ("device key", "vault access", "licence")]
    if source != "canopy-web":
        rep.add("env", OK, "filled from 1Password when each session starts (LEGACY)")
        return
    if blocked:
        rep.add("env", SKIP, f"after {', '.join(blocked)}")
        return
    if not may_resolve:
        rep.add("env", OK, "synced from canopy vault when each session starts "
                           "(inside your local turn)")
        return
    try:
        what = deps.sync_env(slug)
    except Exception as e:  # noqa: BLE001
        rep.add("env", NEEDS, f"could not sync from canopy vault ({_first_line(e)})",
                f"`canopy cred refresh --agent {slug}` once the steps above are done")
        return
    changed = what.startswith("re-injected")
    rep.add("env", DONE if changed else OK, f"~/.{slug}/.env from canopy vault ({what})")


# ──────────────────────────────────────────────────────────────────────────────
# the real world
# ──────────────────────────────────────────────────────────────────────────────

def real_deps() -> Deps:
    import subprocess

    from orchestrator import canopy_web, cred_cli, device_key, local_cli, workspace_cli
    from orchestrator.agent_doctor import installed_plugin_names
    from orchestrator.runner_pin import _machine_runner_id

    def web(method, path, body=None):
        return canopy_web.call(method, path, body, token=local_cli.person_token())

    def dev_call(method, path, body):
        return canopy_web.call(method, path, body, token=local_cli.person_token())

    def zk_status(slug):
        from orchestrator import vault_zk_cli
        return vault_zk_cli.run_status(
            vault_zk_cli.ZkVault(slug, token=local_cli.person_token()))

    def sync_env(slug):
        return cred_cli._sync_from_canopy_web(slug, ttl=0, now=None)

    return Deps(
        base_url=lambda: canopy_web.resolve_base_url(None),
        web=web,
        read_registration=local_cli.read_private_runner,
        register_computer=lambda: local_cli.register_this_computer(),
        access=lambda slug: cred_cli.fetch_access(slug, refresh=True),
        device_status=device_key.read_status,
        machine_runner_id=_machine_runner_id,
        enroll_device=lambda rid: device_key.enroll(runner_id=rid, call=dev_call,
                                                    owner_token=local_cli.person_token()),
        refresh_device=lambda rid: device_key.refresh(runner_id=rid, call=dev_call),
        zk_status=zk_status,
        decide_1password=lambda slug: cred_cli.decide(slug, local_env_counts=False),
        installed_plugins=installed_plugin_names,
        known_marketplaces=lambda: workspace_cli._known(subprocess.run),
        install_plugin=lambda p: workspace_cli.install_plugins(CATALOG, [p], subprocess.run)[0][1:],
        set_session_agent=local_cli.set_session_agent,
        sync_env=sync_env,
    )
