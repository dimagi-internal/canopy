"""`canopy agent prepare --runner` — the one per-agent preparation every runner calls.

Each test pins a lesson canopy-web's runner/ec2/bootstrap_agents.sh learned the hard way, so
moving the logic here (2026-10-15, agent-setup-lifecycle.md phase 2) keeps all of them:
- every `op` call carries exactly ONE key: the agent's for its vault, the workspace's for the
  shared vault (2026-09-07: a per-agent key cannot read the shared gog client);
- the GitHub token rides one git command's environment, never argv or the box;
- a clone whose pull failed is still provisioned (#965); no clone at all fails the agent but
  still reports;
- required plugins are pulled every pass, get node deps when the lockfile moved, and get their
  key files from the two vaults this box reads (canopy-web#1237);
- the mailbox is the INSTANCE's (canopy-web), never the repo's (#984);
- readiness is tri-state: "did not check" is never reported as broken (2026-09-08);
- canopy-web unreachable is not "unregistered".
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import agent_prepare as ap
from orchestrator.agent_prepare import RunnerCredentials, RunnerPreparer, turn_readiness
from tests import sealed_resolve


def _sealed_answer(values: dict, **extra) -> str:
    """A resolve answer as canopy-web sends it: values sealed to this machine."""
    return json.dumps({"values": sealed_resolve.sealed_values(values), "zk": True,
                       "agent_id": sealed_resolve.AGENT_ID, **extra})

AGENT_KEY, SHARED_KEY, GH = "ops_AGENT", "ops_SHARED", "ghp_OWNER"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every path under ~ (~/.<slug>/.env, its stamp, gog, plugins) lands in tmp — a test
    must never touch the real machine's agent state (2026-10-15: one did, a stamp)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # GH_TOKEN too: a cloud runner exports its own, and op_env inherits os.environ, so the
    # box's token would read as the preparer leaking the agent's into a non-git call.
    for k in ("OP_SERVICE_ACCOUNT_TOKEN", "AGENT_VAULT", "AGENT_SLUG", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)


def _token(client="canopy", email="ace@dimagi-ai.com", created="2026-10-01T00:00:00Z"):
    return json.dumps({"client": client, "email": email, "created_at": created,
                       "refresh_token": "r"})


class Box:
    """A fake machine: records every command, answers op/git/claude/npm/gog."""

    def __init__(self, tmp: Path, *, vault_items=None, pull_fails=False, clone_fails=False,
                 gog_live=True, accounts=None):
        self.tmp, self.calls = tmp, []
        self.items = vault_items or {}        # "vault/item/field" -> value
        self.pull_fails, self.clone_fails = pull_fails, clone_fails
        self.gog_live, self.accounts = gog_live, accounts or []
        self.markets, self.plugins = set(), set()
        self.envs = []               # (argv, full env) per call
        self.setup_result = None     # what `bash bin/<slug>-setup` returns (default: ok)

    def __call__(self, argv, **kw):
        env = kw.get("env") or {}
        self.calls.append((list(argv), env.get("OP_SERVICE_ACCOUNT_TOKEN"), env.get("GH_TOKEN"),
                           kw.get("cwd")))
        self.envs.append((list(argv), dict(env)))
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        bad = lambda e: SimpleNamespace(returncode=1, stdout="", stderr=e)  # noqa: E731
        cmd = argv[0]
        if cmd == "op" and argv[1] == "read":
            ref = argv[2][len("op://"):]
            key_ok = {"Agent-Ace": AGENT_KEY, "Canopy-Shared": SHARED_KEY}.get(ref.split("/")[0])
            if env.get("OP_SERVICE_ACCOUNT_TOKEN") != key_ok:
                return bad(f'"{ref.split("/")[0]}" isn\'t a vault in this account')
            v = self.items.get(ref) or self.items.get(ref + "/credential")
            return SimpleNamespace(returncode=0, stdout=v, stderr="") if v else bad("isn't an item")
        if cmd == "op" and argv[1:3] == ["vault", "get"]:
            if env.get("OP_SERVICE_ACCOUNT_TOKEN") != AGENT_KEY:
                return bad("isn't a vault in this account")
            return SimpleNamespace(returncode=0, stdout='{"content_version": 7}', stderr="")
        if cmd == "op" and argv[1] == "inject":
            if env.get("OP_SERVICE_ACCOUNT_TOKEN") != AGENT_KEY or env.get("AGENT_VAULT") != "Agent-Ace":
                return bad("could not resolve op://${AGENT_VAULT}/canopy-pat/credential")
            out = Path(argv[argv.index("-o") + 1])
            out.write_text("CANOPY_WEB_PAT=x\n")
            return ok
        if cmd == "git":
            sub = [a for a in argv if not a.startswith("credential.") and a != "-c"]
            if "clone" in sub:
                if self.clone_fails:
                    return bad("fatal: repository not found")
                dest = Path(sub[-1])
                (dest / ".git").mkdir(parents=True, exist_ok=True)
                (dest / ".claude-plugin").mkdir(exist_ok=True)
                (dest / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": dest.name}))
                return ok
            return bad("fatal: could not read Username") if self.pull_fails else ok
        if cmd == "claude":
            if argv[2:4] == ["marketplace", "list"]:
                return SimpleNamespace(returncode=0, stdout="\n".join(self.markets), stderr="")
            if argv[2] == "list":
                return SimpleNamespace(returncode=0, stdout="\n".join(self.plugins), stderr="")
            if argv[2:4] == ["marketplace", "add"]:
                self.markets.add(Path(argv[4]).name)
                return ok
            if argv[2] == "install":
                self.plugins.add(argv[3])
                return ok
        if cmd == "npm":
            nm = Path(kw["cwd"]) / "node_modules"
            nm.mkdir(exist_ok=True)
            (nm / ".package-lock.json").write_text("{}")
            return ok
        if cmd == "gog":
            if argv[1:3] == ["auth", "list"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({"accounts": self.accounts}),
                                       stderr="")
            return ok
        if cmd == "bash":
            return self.setup_result or ok
        return ok

    def ran(self, head):
        return [c for c in self.calls if c[0][:len(head)] == head]


def _repo(root: Path, slug="ace", *, gog_client="canopy", required=None, setup=False):
    repo = root / slug
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    (repo / ".claude-plugin").mkdir(exist_ok=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": slug}))
    (repo / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": slug}))
    (repo / "config").mkdir(exist_ok=True)
    cfg = {"email": "repo-says@dimagi-ai.com", "gog_client": gog_client}
    if required:
        cfg["required_plugins"] = required
    (repo / "config" / "agent.json").write_text(json.dumps(cfg))
    (repo / ".env.tpl").write_text("CANOPY_WEB_PAT=op://${AGENT_VAULT}/canopy-pat/credential\n")
    if setup:
        (repo / "bin").mkdir(exist_ok=True)
        (repo / "bin" / f"{slug}-setup").write_text("#!/bin/bash\n")
    return repo


def _creds(**over):
    base = dict(repo_url="https://github.com/dimagi-internal/ace", vault="Agent-Ace", key=AGENT_KEY, shared_vault="Canopy-Shared",
                shared_key=SHARED_KEY, github_token=GH, mailbox="ace@dimagi-ai.com",
                web_gog_token=_token(), values={"gog-oauth-client": '{"installed": {}}'})
    return RunnerCredentials(**{**base, **over})


def _prep(tmp, box, creds=None, reports=None, verify_ok=True):
    reports = reports if reports is not None else []

    def transport(method, url, headers, data):
        reports.append((method, url, json.loads(data) if data else None))
        return 200, "{}"

    return RunnerPreparer(
        base_url="https://canopy.test", token="runner-pat", agent_root=tmp / "agents",
        plugin_deps_root=tmp / "deps", transport=transport,
        creds_for=lambda s: creds or _creds(), runner_name="cloud-test",
        runner=box, gog_dir=str(tmp / "gog"), home=tmp / "home",
        verify=lambda ident, gog_dir=None: (verify_ok, ["x"]), echo=lambda _m: None,
        platform="linux")


def test_the_gog_token_and_client_never_come_from_1password(tmp_path):
    # Off 1Password (2026-10-26): canopy-web's token, the canopy vault's client file.
    _repo(tmp_path / "agents")
    box = Box(tmp_path, vault_items={
        "Agent-Ace/gog-token/credential": _token(created="2027-01-01T00:00:00Z"),
        "Canopy-Shared/gog-oauth-client/credential": '{"from": "1password"}'})
    rep = _prep(tmp_path, box).prepare("ace")
    reads = [c[0][2] for c in box.ran(["op", "read"])]
    assert not [r for r in reads if "gog-token" in r or "gog-oauth-client" in r]
    assert all("--account" not in c[0] for c in box.ran(["op"]))
    assert rep.env_ok is True and rep.client_creds_ok and rep.gog_client == "canopy"
    assert rep.token.startswith("canopy-web canopy")


def test_the_github_token_rides_one_git_commands_env_never_argv(tmp_path):
    box = Box(tmp_path)
    _prep(tmp_path, box).prepare("ace", credentials_only=False)
    git = box.ran(["git"])
    assert git and all(c[2] == GH for c in git)
    assert all(GH not in " ".join(c[0]) for c in box.calls)
    assert all(c[2] is None for c in box.calls if c[0][0] != "git")


def test_a_clone_whose_pull_failed_is_still_provisioned(tmp_path):
    _repo(tmp_path / "agents")
    box = Box(tmp_path, pull_fails=True)
    rep = _prep(tmp_path, box).prepare("ace")
    assert any("repo not updated" in n for n in rep.notes)
    assert box.ran(["op", "inject"]) and rep.env_ok is True


def test_no_clone_at_all_fails_the_agent_but_still_reports(tmp_path):
    reports = []
    rep = _prep(tmp_path, Box(tmp_path, clone_fails=True), reports=reports).prepare("ace")
    assert not rep.ok and any("clone of" in n for n in rep.notes)
    assert [r[0] for r in reports] == ["POST"]
    assert reports[0][1].endswith("/api/agents/ace/bootstrap-report")


def test_required_plugins_are_pulled_every_pass_get_deps_and_key_files(tmp_path):
    _repo(tmp_path / "agents", required=[{"name": "chrome-sales",
                                         "marketplace": "dimagi-internal/chrome-sales"}])
    dep = tmp_path / "deps" / "chrome-sales"
    (dep / ".git").mkdir(parents=True)
    (dep / "package.json").write_text("{}")
    (dep / "config").mkdir()
    (dep / "config" / "secrets.yaml").write_text(
        "secrets:\n  - name: gws\n    op: op://AI-Agents/gws-sa-key/credential\n"
        "    target: '{repo}/.gws-sa-key.json'\n"
        "  - name: sf\n    op: op://AI-Agents/sf/credential\n    target: '{repo}/sf'\n"
        "    optional: true\n")
    box = Box(tmp_path, vault_items={"Canopy-Shared/gws-sa-key/credential": "KEYFILE"})
    rep = _prep(tmp_path, box).prepare("ace")
    assert box.ran(["git", "-c"]) and any(dep.as_posix() in " ".join(c[0]) and "pull" in c[0]
                                          for c in box.ran(["git"]))
    assert [c[3] for c in box.ran(["npm"])] == [str(dep)]
    assert (dep / ".gws-sa-key.json").read_text() == "KEYFILE"      # agent vault, then shared
    assert not (dep / "sf").exists()                                   # optional: never staged
    assert "chrome-sales@chrome-sales" in box.plugins and "ace@ace" in box.plugins
    assert not [n for n in rep.notes if n.startswith("plugins:")], rep.notes


def test_an_agent_repo_without_a_marketplace_manifest_fails_the_plugins_step(tmp_path):
    repo = _repo(tmp_path / "agents")
    (repo / ".claude-plugin" / "marketplace.json").unlink()
    rep = _prep(tmp_path, Box(tmp_path)).prepare("ace")
    assert not rep.ok
    assert any("no .claude-plugin/marketplace.json" in n for n in rep.notes), rep.notes
    assert rep.plugins == "0 ok FAIL"


def test_the_mailbox_is_the_instances_and_a_token_for_another_is_refused(tmp_path):
    _repo(tmp_path / "agents")
    box = Box(tmp_path)
    rep = _prep(tmp_path, box, creds=_creds(
        web_gog_token=_token(email="someone-else@dimagi-ai.com"))).prepare("ace")
    assert any("NOT importing" in n for n in rep.notes)
    assert not box.ran(["gog", "auth", "tokens", "import"])


def test_a_canopy_web_client_token_gets_its_client_from_the_canopy_vault(tmp_path):
    _repo(tmp_path / "agents")
    box = Box(tmp_path)
    creds = _creds(web_gog_token=_token(client="canopy-web", created="2026-10-01T00:00:00Z"),
                   values={"gog-oauth-client-web": "{}"})
    rep = _prep(tmp_path, box, creds=creds).prepare("ace")
    assert rep.gog_client == "canopy-web" and rep.token.startswith("canopy-web canopy-web")
    # turns declare `canopy`; a canopy-web token serves them (one app, two doors)
    assert (rep.turn_client, rep.turn_ready) == ("canopy-web", True)
    assert rep.client_creds_ok


def test_readiness_is_tri_state_and_did_not_check_is_never_broken(tmp_path):
    _repo(tmp_path / "agents")
    rep = _prep(tmp_path, Box(tmp_path), creds=_creds(web_gog_token=None)).prepare(
        "ace", credentials_only=True)
    payload = rep.payload("cloud-test")
    assert payload["turn_ready"] is None and payload["env_ok"] is None
    assert payload["mailbox_ok"] is False


def test_unreachable_canopy_web_is_not_unregistered(tmp_path):
    creds = RunnerCredentials(error="could not reach canopy-web for ace's credentials: timeout")
    rep = _prep(tmp_path, Box(tmp_path), creds=creds).prepare("ace", credentials_only=True)
    assert any("could not reach canopy-web" in n for n in rep.notes)
    assert not any("PUT /api/agents" in n for n in rep.notes)


def test_no_vault_registered_says_how_and_injects_nothing(tmp_path):
    _repo(tmp_path / "agents")
    box = Box(tmp_path)
    rep = _prep(tmp_path, box, creds=_creds(vault="", key="")).prepare("ace")
    assert rep.env_ok is False and not box.ran(["op", "inject"])
    assert any("canopy agent vault --slug ace" in n for n in rep.notes)


def test_credentials_only_never_clones_or_reprovisions_a_live_agent(tmp_path):
    _repo(tmp_path / "agents", setup=True)
    box = Box(tmp_path)
    _prep(tmp_path, box).prepare("ace", credentials_only=True)
    assert not box.ran(["git"]) and not box.ran(["claude"]) and not box.ran(["bash"])
    assert not box.ran(["op", "inject"])


def test_env_only_is_cred_sync_reinject_only_on_a_change(tmp_path):
    """The claim-time refresh is canopy's `cred sync` — the function every session start
    runs — so a runner keeps no copy of its own: one `op vault get`, `op inject` only
    when the fingerprint moved, with the agent's key and its vault."""
    _repo(tmp_path / "agents")
    box = Box(tmp_path)
    rep = _prep(tmp_path, box).prepare("ace", env_only=True)
    assert rep.env_ok is True and rep.env == "re-injected"
    assert [c[0][:3] for c in box.calls] == [["op", "vault", "get"], ["op", "inject", "-f"]]
    assert all(c[1] == AGENT_KEY for c in box.calls)
    assert (tmp_path / "home" / ".ace" / ".env").is_file()
    box.calls.clear()
    rep = _prep(tmp_path, box).prepare("ace", env_only=True)
    assert rep.env == "unchanged" and [c[0][1] for c in box.calls] == ["vault"]
    import os
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in os.environ   # staged for the call, then removed


def test_the_provisioner_gets_only_this_agents_key(tmp_path):
    _repo(tmp_path / "agents", setup=True)
    box = Box(tmp_path)
    _prep(tmp_path, box).prepare("ace")
    (setup,) = box.ran(["bash"])
    assert setup[1] == AGENT_KEY and setup[0][-1] == "--skip-doctor"


def test_the_provisioner_is_handed_the_vault_and_the_env_this_pass_filled(tmp_path):
    """bin/<slug>-setup must not re-resolve what prepare already did: re-resolving goes
    through `canopy agent env`, which needs a person's token a runner never has
    (cloud-ec2-2, 2026-10-18: ACE's setup failed every pass at exactly that step)."""
    _repo(tmp_path / "agents", setup=True)
    box = Box(tmp_path)
    rep = _prep(tmp_path, box).prepare("ace")
    assert rep.env_ok is True
    ((argv, env),) = [e for e in box.envs if e[0][0] == "bash"]
    assert env["AGENT_VAULT"] == "Agent-Ace" and env["AGENT_SLUG"] == "ace"
    assert env["AGENT_ENV_FILE"] == str(tmp_path / "home" / ".ace" / ".env")
    assert Path(env["AGENT_ENV_FILE"]).read_text() == "CANOPY_WEB_PAT=x\n"
    assert env["CLAUDE_PLUGIN_DATA"].endswith("plugins/data/ace-ace")


def test_a_failed_env_step_hands_the_provisioner_no_env_file(tmp_path):
    _repo(tmp_path / "agents", setup=True)
    box = Box(tmp_path)
    # The wrong key: op inject fails, so there is no freshly filled env to hand over.
    rep = _prep(tmp_path, box, creds=_creds(key="ops_WRONG")).prepare("ace")
    assert rep.env_ok is False
    ((argv, env),) = [e for e in box.envs if e[0][0] == "bash"]
    assert "AGENT_ENV_FILE" not in env and env["AGENT_VAULT"] == "Agent-Ace"


def test_a_failed_provisioner_reports_its_fail_line_not_its_info_banner(tmp_path):
    _repo(tmp_path / "agents", setup=True)
    box = Box(tmp_path)
    box.setup_result = SimpleNamespace(returncode=1, stderr="", stdout=(
        "INFO plugin_root=/home/ubuntu/.claude/plugins/cache/ace/ace/0.13.2041 (source=x)\n"
        "PASS op: authenticated as service-account\n"
        "FAIL instance: canopy could not resolve this install's ACE instance and vault. "
        "canopy said:\n"
        "Error: not signed in to canopy-web — run /canopy:canopy-web-pat-mint, then re-run.\n"
        "  fix: several instances → re-run /ace:setup --agent <instance>\n"))
    rep = _prep(tmp_path, box).prepare("ace")
    (note,) = [n for n in rep.notes if n.startswith("provisioner:")]
    assert "INFO plugin_root" not in note
    assert "FAIL instance: canopy could not resolve" in note
    assert "not signed in to canopy-web" in note


def test_setup_failure_falls_back_to_the_last_line():
    assert ap._setup_failure("INFO a\nsomething broke\n", "") == "something broke"
    assert ap._setup_failure("", "") == "(no output)"
    assert ap._setup_failure("INFO a\n", "fatal: repository not found\n") == \
        "fatal: repository not found"


def test_turn_readiness_names_a_real_mismatch(tmp_path):
    repo = _repo(tmp_path / "agents", gog_client="canopy")
    tok = SimpleNamespace(client="ace-legacy")
    assert turn_readiness(repo, tok, True) == ("canopy", False)
    assert turn_readiness(repo, None, False) == ("canopy", None)


def test_the_repo_is_canopy_webs_record_never_a_naming_convention(tmp_path):
    box = Box(tmp_path)
    _prep(tmp_path, box, creds=_creds(repo_url="https://github.com/other-org/ace-fork")).prepare("ace")
    (clone,) = [c for c in box.ran(["git"]) if "clone" in c[0]]
    assert "https://github.com/other-org/ace-fork.git" in clone[0]
    rep = _prep(tmp_path / "x", Box(tmp_path / "x"), creds=_creds(repo_url="")).prepare("ace")
    assert any("records no repository" in n for n in rep.notes) and not rep.ok


def test_runner_credentials_read_the_repo_from_runtime():
    calls = []

    def transport(method, url, headers, data):
        calls.append(url)
        if url.endswith("/runtime"):
            return 200, '{"repo_url": "https://github.com/o/r"}'
        return 200, '{"op_vault": "V", "op_sa_token": "k", "mailbox": "a@b"}'

    c = ap.fetch_runner_credentials("ace", base_url="https://c.test", token="t",
                                    transport=transport)
    assert (c.repo_url, c.vault, c.mailbox) == ("https://github.com/o/r", "V", "a@b")


def test_runner_credentials_say_they_are_a_box_refresh():
    """canopy-web#1563: the runner-update timer refreshes outside any turn; without the
    purpose header canopy-web emailed the owner 'read outside a turn' every hour."""
    seen = []

    def transport(method, url, headers, data):
        if "/credentials/resolve" in url:
            seen.append(dict(headers))
        return 200, '{"op_vault": "V"}'

    ap.fetch_runner_credentials("ace", base_url="https://c.test", token="t", transport=transport)
    assert seen and seen[0]["X-Canopy-Read-Purpose"] == "runner-refresh"


def test_a_verification_run_labels_its_reads_as_a_check():
    """A person's (or agent's) `prepare --check WHY` is not the box's refresh: it says so,
    and canopy-web alerts on it worded as that check (never silently)."""
    seen = []

    def transport(method, url, headers, data):
        if "/credentials/resolve" in url:
            seen.append(dict(headers))
        return 200, '{"op_vault": "V"}'

    purpose = ap.verification_purpose("vault laptop check")
    ap.fetch_runner_credentials("ace", base_url="https://c.test", token="t", transport=transport,
                                purpose=purpose)
    got = seen[0]["X-Canopy-Read-Purpose"]
    assert got.startswith("check: vault laptop check (by ") and got.endswith(")")


def test_prepare_purpose_from_the_cli(monkeypatch):
    from orchestrator import agent_cli

    assert agent_cli._prepare_purpose("why").startswith("check: why (by ")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert agent_cli._prepare_purpose(None) == ""                 # the box's own refresh
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.delenv("CANOPY_TURN_ID", raising=False)
    assert agent_cli._prepare_purpose(None).startswith("check: interactive run (by ")


# ── the credentials-only pass asks for only what it uses (canopy-web#1563) ──────

def _recording(secret_refs):
    resolves = []
    answer = _sealed_answer({"gog-token": "{}"}, op_vault="V", op_sa_token="k")

    def transport(method, url, headers, data):
        if url.endswith("/runtime"):
            return 200, json.dumps({"repo_url": "u", "secret_refs": secret_refs})
        if "/credentials/resolve" in url:
            resolves.append(url)
            # A scoped answer: the extras ride beside `values`, never inside it.
            return 200, answer
        return 200, "{}"
    return transport, resolves


def test_the_refresh_asks_only_for_the_gog_token_and_the_vault_keys(monkeypatch):
    """Not the agent's PAT, Salesforce, GitHub token or anything else: the timer pass
    writes no env, so every other secret it read went on the wire every 30 minutes."""
    sealed_resolve.install(monkeypatch)
    transport, resolves = _recording(["CANOPY_WEB_PAT", "gog-token", "salesforce"])
    c = ap.fetch_runner_credentials("hal", base_url="https://c.test", token="t",
                                    transport=transport, minimal=True)
    assert resolves == ["https://c.test/api/agents/hal/credentials/resolve"
                        "?refs=gog-token,op_sa_token,shared_op_sa_token"], \
        "exactly one scoped read, never the unscoped fallback"
    assert (c.key, c.web_gog_token, c.values) == ("k", "{}", {"gog-token": "{}"})


def test_a_refresh_of_an_agent_without_a_gog_token_names_none(monkeypatch):
    sealed_resolve.install(monkeypatch)
    """Naming an undeclared ref raises an owner alert; a box must not cause one."""
    transport, resolves = _recording(["CANOPY_WEB_PAT"])
    ap.fetch_runner_credentials("eva", base_url="https://c.test", token="t",
                                transport=transport, minimal=True)
    assert resolves == ["https://c.test/api/agents/eva/credentials/resolve"
                        "?refs=op_sa_token,shared_op_sa_token"]


def test_a_full_prepare_still_resolves_everything(monkeypatch):
    sealed_resolve.install(monkeypatch)
    transport, resolves = _recording(["CANOPY_WEB_PAT", "gog-token"])
    ap.fetch_runner_credentials("hal", base_url="https://c.test", token="t", transport=transport)
    assert resolves == ["https://c.test/api/agents/hal/credentials/resolve"]


def test_prepare_scopes_only_the_credentials_only_pass(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ap, "fetch_runner_credentials",
                        lambda s, **kw: seen.append(kw["minimal"]) or ap.RunnerCredentials(
                            error="stop here"))
    for kwargs, want in (({"credentials_only": True}, True), ({"env_only": True}, False),
                         ({}, False)):
        p = ap.RunnerPreparer(base_url="https://c.test", token="t",
                              agent_root=tmp_path / "agents", home=tmp_path / "home",
                              dry_run=True)
        p.prepare("hal", report=False, **kwargs)
        assert seen[-1] is want, kwargs


# ── an agent on the canopy-web backend (Canopy Vault, canopy#946) ─────────────

def test_a_canopy_web_agent_gets_its_env_from_canopy_web_not_op(tmp_path):
    """canopy-web withholds the vault key from a canopy-web-backed agent, so `op inject`
    cannot run: the env comes from the values canopy-web resolved for this runner."""
    _repo(tmp_path / "agents")
    box = Box(tmp_path)
    creds = _creds(credential_source="canopy-web", key="", vault="",
                   values={"CANOPY_WEB_PAT": "cpat_FROM_WEB", "gog-token": "{\n}"})
    rep = _prep(tmp_path, box, creds=creds).prepare("ace", env_only=True)
    assert rep.env_ok is True and rep.env == "OK (1 vars from canopy-web)"
    env = (tmp_path / "home" / ".ace" / ".env")
    assert "CANOPY_WEB_PAT=cpat_FROM_WEB" in env.read_text() and "gog-token" not in env.read_text()
    assert oct(env.stat().st_mode)[-3:] == "600"
    assert not [c for c in box.calls if c[0][:1] == ["op"]]
    assert not any("no key" in n for n in rep.notes)


def test_the_full_env_step_takes_the_same_branch(tmp_path):
    repo = _repo(tmp_path / "agents")
    box = Box(tmp_path)
    p = _prep(tmp_path, box, creds=_creds(credential_source="canopy-web", key="",
                                          values={"CANOPY_WEB_PAT": "x"}))
    rep = ap.RunnerReport(slug="ace")
    p._env("ace", repo, rep)
    assert rep.env_ok is True and not [c for c in box.calls if c[0][:1] == ["op"]]


def test_runner_credentials_carry_the_backend_and_values(monkeypatch):
    sealed_resolve.install(monkeypatch)

    def transport(method, url, headers, data):
        if url.endswith("/credentials/access"):
            return 200, '{"credential_source": "canopy-web", "may_resolve": true}'
        if url.endswith("/runtime"):
            return 200, "{}"
        return 200, _sealed_answer({"A": "1"}, op_vault="", op_sa_token="")

    c = ap.fetch_runner_credentials("ace", base_url="https://c.test", token="t",
                                    transport=transport)
    assert c.credential_source == "canopy-web" and c.values == {"A": "1"}


def test_an_unreachable_access_route_means_the_default_path():
    def transport(method, url, headers, data):
        if url.endswith("/credentials/access"):
            return 500, "boom"
        return 200, '{"op_vault": "V", "op_sa_token": "k"}'

    c = ap.fetch_runner_credentials("ace", base_url="https://c.test", token="t",
                                    transport=transport)
    assert c.credential_source == "" and (c.vault, c.key) == ("V", "k")


def test_a_canopy_hosted_mailbox_needs_no_gmail_token_and_is_ready(tmp_path):
    # An address on the workspace's own mail domain: canopy-web receives and sends it, so a
    # missing gog token is not a broken mailbox (it used to fail "no gog token" forever).
    _repo(tmp_path / "agents")
    creds = _creds(mailbox="scout@marshellis.com", mailbox_backend="ses", vault="",
                   key="", credential_source="canopy-web")
    rep = _prep(tmp_path, Box(tmp_path), creds=creds).prepare("ace", credentials_only=True)
    assert rep.ok, rep.notes
    assert rep.gmail == "canopy-hosted" and rep.mailbox_ok and rep.turn_ready is True
    assert any("canopy-hosted email" in n for n in rep.notes)


def test_a_hosted_mailbox_stages_an_optional_google_sign_in_for_drive(tmp_path):
    _repo(tmp_path / "agents")
    box = Box(tmp_path)
    rep = _prep(tmp_path, box, creds=_creds(mailbox_backend="ses")).prepare("ace", credentials_only=True)
    assert rep.ok and rep.gmail == "canopy-hosted" and rep.gog_client == "canopy"
    assert any("Drive" in n for n in rep.notes)


# ── an agent's Google account is not always its mailbox ─────────────────────────────────

def test_an_ses_primary_agent_stages_its_separate_google_account_token(tmp_path):
    """An SES-primary agent (ace@dimagi.ai, canopy-hosted) does Drive/Docs as a separate
    Google account. canopy-web names it (`google_account`), and the token for THAT
    account is the one to stage — not refused as "another mailbox"."""
    _repo(tmp_path / "agents")
    box = Box(tmp_path)
    creds = _creds(mailbox="ace@dimagi.ai", mailbox_backend="ses",
                   google_account="ace@dimagi-ai.com")
    rep = _prep(tmp_path, box, creds=creds).prepare("ace", credentials_only=True)
    assert rep.ok and rep.gog_client == "canopy", rep.notes
    assert not any("NOT importing" in n for n in rep.notes)
    assert any("Google sign-in staged" in n for n in rep.notes)


def test_without_google_account_the_token_is_still_compared_to_the_mailbox(tmp_path):
    """An older canopy-web sends no `google_account`: the mailbox stays the comparison,
    so a token for any other address is still refused."""
    _repo(tmp_path / "agents")
    box = Box(tmp_path, vault_items={
        "Agent-Ace/gog-token/credential": _token(email="ace@dimagi-ai.com")})
    prep = _prep(tmp_path, box, creds=_creds(mailbox="ace@dimagi.ai", mailbox_backend="ses"))
    assert prep.expected_mailbox("ace", tmp_path) == "ace@dimagi.ai"
    rep = prep.prepare("ace", credentials_only=True)
    assert not box.ran(["gog", "auth", "tokens", "import"])
    assert any("not staged" in n for n in rep.notes)


def test_runner_credentials_carry_google_account_when_canopy_web_sends_it():
    def transport(method, url, headers, data):
        if url.endswith("/runtime"):
            return 200, "{}"
        return 200, json.dumps({"mailbox": "ace@dimagi.ai", "mailbox_backend": "ses",
                                "google_account": "ace@dimagi-ai.com"})

    c = ap.fetch_runner_credentials("ace", base_url="https://c.test", token="t",
                                    transport=transport)
    assert (c.mailbox, c.google_account) == ("ace@dimagi.ai", "ace@dimagi-ai.com")


def test_the_client_file_comes_from_the_workspace_vault():
    """A shared OAuth client a workspace vault holds arrives in the box's own resolve
    (merged under the agent's values); the box writes it from there — the only source."""
    from orchestrator.agent_prepare import RunnerCredentials, RunnerPreparer

    p = RunnerPreparer.__new__(RunnerPreparer)
    p._creds = {"ace": RunnerCredentials(values={"gog-oauth-client-web": '{"installed": {}}'})}
    p.creds = lambda slug: p._creds[slug]
    assert p.client_from_vault("ace", "canopy-web") == '{"installed": {}}'
    assert p.client_from_vault("ace", "canopy") is None
    assert p.client_from_vault("ace", "someone-elses-client") is None


def test_with_no_token_from_resolve_git_uses_the_turns_gh_token(tmp_path, monkeypatch):
    """canopy-web stopped returning the agent's GitHub token from credentials/resolve;
    a runner preparing an agent in a turn hands over that turn's token, which only git
    gets (it leaves the environment, so no other child inherits it)."""
    monkeypatch.setenv(ap.TURN_GITHUB_TOKEN_ENV, "ghp_TURN")
    box = Box(tmp_path)
    _prep(tmp_path, box, creds=_creds(github_token="")).prepare("ace", credentials_only=False)
    git = box.ran(["git"])
    assert git and all(c[2] == "ghp_TURN" for c in git)
    assert all(c[2] is None for c in box.calls if c[0][0] != "git")
    assert ap.TURN_GITHUB_TOKEN_ENV not in os.environ
