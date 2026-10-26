"""`canopy agent bootstrap` — decision logic, with every external (op, gog, claude,
canopy-web) faked. Nothing here touches the real machine."""
from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

from orchestrator import agent_bootstrap as ab
from orchestrator.agent_doctor import (
    RequiredPlugin,
    check_required_plugins,
    parse_required_plugins,
)


def _tok(client="canopy", created="2026-09-07T14:04:13Z", email="ace@dimagi-ai.com"):
    return json.dumps({"email": email, "client": client, "created_at": created,
                       "refresh_token": "SECRET-REFRESH"})


def _repo(tmp_path, slug="ace", *, gog_client="canopy", required=None, tpl=True, mkt=True):
    repo = tmp_path / slug
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": slug}))
    if mkt:
        (repo / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"name": slug, "plugins": [{"name": slug}]}))
    (repo / "config").mkdir()
    agent = {"email": f"{slug}@dimagi-ai.com", "gog_client": gog_client,
             "repo": f"dimagi-internal/{slug}"}
    if required is not None:
        agent["required_plugins"] = required
    (repo / "config" / "agent.json").write_text(json.dumps(agent))
    if tpl:
        (repo / ".env.tpl").write_text("A=op://x/y/z\n")
    return repo


#: The vaults canopy-web records for these test agents (Agent.op_vault) — a table, the way
#: canopy-web holds them; nothing derives a vault from a slug.
RECORDED_VAULTS = {"ace": "Agent-Ace", "eva": "Agent-Eva", "echo": "Agent-Echo"}


class FakeRunner:
    """Records argv; answers op/gog/claude from a table."""

    def __init__(self, *, op=None, gog_accounts=None, fail=()):
        self.calls: list[list[str]] = []
        self.op = op or {}
        self.gog_accounts = gog_accounts if gog_accounts is not None else []
        self.fail = set(fail)
        self.imported: list[str] = []
        self.envs: list[dict | None] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.envs.append(kw.get("env"))
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        bad = lambda err: subprocess.CompletedProcess(argv, 1, "", err)  # noqa: E731
        head = argv[0]
        if head in self.fail:
            return bad(f"{head} exploded")
        if argv[:2] == ["op", "read"]:
            v = self.op.get(argv[2])
            return ok(v) if v else bad(f'"{argv[2]}" isn\'t an item')
        if argv[:2] == ["op", "inject"]:
            Path(argv[argv.index("-o") + 1]).write_text("A=1\nB=2\n")
            return ok()
        if argv[:3] == ["gog", "auth", "list"]:
            return ok(json.dumps({"accounts": self.gog_accounts}))
        if argv[:4] == ["gog", "auth", "tokens", "import"]:
            p = Path(argv[4])
            self.imported.append(p.read_text())
            assert stat.S_IMODE(p.stat().st_mode) == 0o600
            return ok()
        if head == "claude":
            return ok()
        return bad("unexpected " + " ".join(argv))


#: The workspace canopy vault's client items — where bootstrap reads a client file from.
VAULT_CLIENTS = {"canopy": '{"installed":{"client_id":"c"}}',
                 "canopy-web": '{"installed":{"client_id":"w"}}'}


def _boot(tmp_path, runner, *, web=None, verify_ok=True, dry_run=False, installed=(),
          marketplaces=(), vaults=None, clients=None):
    reg = tmp_path / "installed_plugins.json"
    reg.write_text(json.dumps({"plugins": {f"{p}@{p}": [{}] for p in installed}}))
    mk = tmp_path / "known_marketplaces.json"
    mk.write_text(json.dumps({m: {} for m in marketplaces}))
    verified = []

    def verify(ident, gog_dir=None):
        verified.append((ident.account, ident.client))
        return verify_ok, ["OK"] if verify_ok else ["FIX: token bad"]

    b = ab.Bootstrapper(dry_run=dry_run, runner=runner,
                        web_token=lambda slug: ((web or {}).get(slug), ""),
                        gog_dir=str(tmp_path / "gog"), registry_path=str(reg),
                        marketplaces_path=str(mk), home=tmp_path / "home",
                        verify=verify, echo=lambda s: None, platform="darwin",
                        vault_for=_vault_for(RECORDED_VAULTS if vaults is None else vaults))
    b.verified = verified
    table = VAULT_CLIENTS if clients is None else clients
    b.client_from_vault = lambda slug, client: table.get(client)
    return b


def _vault_for(table):
    def lookup(slug):
        if not table.get(slug):
            from orchestrator.agent_instance import NO_VAULT, InstanceError
            raise InstanceError(f"agent '{slug}': {NO_VAULT}")
        return table[slug]
    return lookup


# ── newest wins ─────────────────────────────────────────────────────────────

def test_newest_wins_by_created_at_either_direction():
    old = ab.parse_token("vault", _tok(created="2026-05-01T21:23:22Z"))
    new = ab.parse_token("canopy-web", _tok(created="2026-09-07T14:04:13Z"))
    assert ab.pick_newest([old, new]).source == "canopy-web"
    old_web = ab.parse_token("canopy-web", _tok(created="2026-05-01T21:23:22Z"))
    new_vault = ab.parse_token("vault", _tok(created="2026-09-07T14:04:13Z"))
    assert ab.pick_newest([new_vault, old_web]).source == "vault"


def test_tie_or_undated_degrades_to_the_first_candidate_the_vault():
    v = ab.parse_token("vault", _tok(created=""))
    w = ab.parse_token("canopy-web", _tok(created=""))
    assert ab.pick_newest([v, w]).source == "vault"
    assert ab.pick_newest([None, w]).source == "canopy-web"
    assert ab.pick_newest([None, ab.parse_token("vault", "not json")]) is None


# ── the client comes from the token ─────────────────────────────────────────

def test_the_token_and_client_never_come_from_1password(tmp_path):
    # Off 1Password (Jonathan, 2026-10-26): canopy-web's token, the canopy vault's client.
    repo = _repo(tmp_path)
    runner = FakeRunner(op={"op://Agent-Ace/gog-token/credential": _tok(created="2027-01-01T00:00:00Z"),
                            "op://Canopy-Shared/gog-oauth-client/credential": "{}"})
    rep = _boot(tmp_path, runner, web={"ace": _tok()}, installed=("ace",)).bootstrap_one("ace", repo)
    assert rep.token.startswith("canopy-web canopy"), rep.token
    assert not any(c[:2] == ["op", "read"] for c in runner.calls)
    assert (tmp_path / "gog" / "credentials-canopy.json").read_text() == VAULT_CLIENTS["canopy"]
    assert not hasattr(ab, "client_op_ref") and not hasattr(ab, "agent_vault")


def test_no_web_token_says_to_connect_the_mailbox(tmp_path):
    repo = _repo(tmp_path)
    rep = _boot(tmp_path, FakeRunner(), installed=("ace",)).bootstrap_one("ace", repo)
    assert not rep.ok and rep.token == "NONE"
    assert any("Connect Google mailbox" in n for n in rep.notes)


def test_client_is_taken_from_token_not_agent_json(tmp_path):
    # agent.json declares canopy; the (newer) token names canopy-web.
    repo = _repo(tmp_path, gog_client="canopy")
    runner = FakeRunner(op={
        "op://Agent-Ace/gog-token/credential": _tok(client="ace", created="2026-05-01T00:00:00Z"),
        "op://Canopy-Shared/gog-oauth-client-web/credential": '{"installed":{}}',
    })
    b = _boot(tmp_path, runner, web={"ace": _tok(client="canopy-web")}, installed=("ace",))
    rep = b.bootstrap_one("ace", repo)
    creds = tmp_path / "gog" / "credentials-canopy-web.json"
    assert creds.is_file() and stat.S_IMODE(creds.stat().st_mode) == 0o600
    assert not (tmp_path / "gog" / "credentials-canopy.json").exists()
    cfg = json.loads((tmp_path / "gog" / "config.json").read_text())
    assert cfg["account_clients"]["ace@dimagi-ai.com"] == "canopy-web"
    assert b.verified == [("ace@dimagi-ai.com", "canopy-web")]
    # ...and a `canopy-web` token serves a `canopy` declaration: one app, two doors
    # (FLEET_CLIENTS). This was a MISMATCH failure until 2026-10-04, which failed every
    # mailbox signed in through canopy-web's "Connect Google mailbox" button.
    assert rep.turn == "OK (canopy-web; declared canopy)"
    assert not any("TURN-CLIENT MISMATCH" in n for n in rep.notes)


def test_a_non_fleet_turn_client_mismatch_is_still_loud(tmp_path):
    # A token under a non-fleet client (a legacy per-agent app) does NOT serve a fleet
    # declaration — that is the 2026-09-08 shape, and it must stay a failure.
    repo = _repo(tmp_path, gog_client="canopy")
    runner = FakeRunner(op={
        "op://Agent-Ace/gog-oauth-client/credential": '{"installed":{}}',
    })
    b = _boot(tmp_path, runner, web={"ace": _tok(client="ace")}, installed=("ace",))
    rep = b.bootstrap_one("ace", repo)
    assert rep.turn.startswith("MISMATCH") and not rep.ok
    assert any("TURN-CLIENT MISMATCH" in n for n in rep.notes)


def test_token_without_client_is_refused(tmp_path):
    repo = _repo(tmp_path)
    raw = json.dumps({"email": "ace@dimagi-ai.com", "created_at": "2026-09-07T14:04:13Z"})
    runner = FakeRunner(op={"op://Agent-Ace/gog-token/credential": raw})
    rep = _boot(tmp_path, runner, installed=("ace",)).bootstrap_one("ace", repo)
    assert not rep.ok and not any(c[:4] == ["gog", "auth", "tokens", "import"]
                                  for c in runner.calls)


def test_token_for_another_mailbox_is_not_imported(tmp_path):
    repo = _repo(tmp_path)
    runner = FakeRunner(op={"op://Agent-Ace/gog-token/credential": _tok(email="eva@dimagi-ai.com"),
                            "op://Canopy-Shared/gog-oauth-client/credential": "{}"})
    rep = _boot(tmp_path, runner, installed=("ace",)).bootstrap_one("ace", repo)
    assert not rep.ok and runner.imported == []


# ── skip-if-identical + import hygiene ──────────────────────────────────────

def test_identical_token_already_in_gog_is_not_reimported(tmp_path):
    repo = _repo(tmp_path)
    (tmp_path / "gog").mkdir()
    (tmp_path / "gog" / "credentials-canopy.json").write_text("{}")
    (tmp_path / "gog" / "config.json").write_text(json.dumps(
        {"keyring_backend": "keychain", "account_clients": {"ace@dimagi-ai.com": "canopy"}}))
    runner = FakeRunner(
        gog_accounts=[{"email": "ace@dimagi-ai.com", "client": "canopy",
                       "created_at": "2026-09-07T14:04:13Z"}])
    asked = []
    b = _boot(tmp_path, runner, web={"ace": _tok()}, installed=("ace",))
    b.client_from_vault = lambda slug, client: asked.append(client)
    rep = b.bootstrap_one("ace", repo)
    assert rep.ok, rep.notes
    assert runner.imported == []
    assert "(present)" in rep.token and rep.turn == "OK (canopy)"
    # the creds file was present, so the canopy vault was never asked for the client
    assert asked == []


def test_token_present_requires_same_client_and_created_at():
    tok = ab.parse_token("vault", _tok())
    row = {"email": "ace@dimagi-ai.com", "client": "canopy", "created_at": "2026-09-07T14:04:13Z"}
    assert ab.token_already_present(tok, [row])
    assert not ab.token_already_present(tok, [{**row, "client": "ace"}])
    assert not ab.token_already_present(tok, [{**row, "created_at": "2026-05-01T00:00:00Z"}])
    assert not ab.token_already_present(tok, None)


def test_changed_token_is_imported_via_private_tempfile_and_config_merged(tmp_path):
    repo = _repo(tmp_path)
    (tmp_path / "gog").mkdir()
    (tmp_path / "gog" / "config.json").write_text(json.dumps(
        {"keyring_backend": "keychain", "account_clients": {"eva@dimagi-ai.com": "canopy"}}))
    runner = FakeRunner(gog_accounts=[])
    rep = _boot(tmp_path, runner, web={"ace": _tok()}, installed=("ace",)).bootstrap_one("ace", repo)
    assert rep.ok, rep.notes
    assert runner.imported == [_tok()]
    import_path = next(c[4] for c in runner.calls if c[:4] == ["gog", "auth", "tokens", "import"])
    assert not Path(import_path).exists()                       # deleted afterwards
    cfg = json.loads((tmp_path / "gog" / "config.json").read_text())
    assert cfg["keyring_backend"] == "keychain"                 # other keys kept
    assert cfg["account_clients"] == {"eva@dimagi-ai.com": "canopy",
                                      "ace@dimagi-ai.com": "canopy"}
    # no secret ever in argv
    assert not any("SECRET-REFRESH" in " ".join(c) for c in runner.calls)
    # gog auth list ran once for the whole run
    assert sum(c[:3] == ["gog", "auth", "list"] for c in runner.calls) == 1


def test_merge_account_client_reports_change():
    new, changed = ab.merge_account_client({"x": 1}, "a@b", "canopy")
    assert changed and new == {"x": 1, "account_clients": {"a@b": "canopy"}}
    assert ab.merge_account_client(new, "a@b", "canopy")[1] is False


# ── plugins: same contract as doctor ────────────────────────────────────────

def test_parse_required_plugins_matches_cloud_rows():
    specs, bad = parse_required_plugins([
        "chrome-sales",
        {"name": "nova", "marketplace": "voidcraft-labs/nova-marketplace",
         "marketplace_name": "nova-marketplace", "note": "needs\nNOVA_API_KEY"},
        {"marketplace": "x/y"}, 7])
    assert specs == [RequiredPlugin("chrome-sales", "", "chrome-sales", ""),
                     RequiredPlugin("nova", "voidcraft-labs/nova-marketplace",
                                    "nova-marketplace", "needs NOVA_API_KEY")]
    assert bad == [{"marketplace": "x/y"}, 7]


def test_plugin_plan_agrees_with_doctor(tmp_path):
    required = [{"name": "chrome-sales", "marketplace": "dimagi-internal/chrome-sales"},
                {"name": "nova", "marketplace": "voidcraft-labs/nova-marketplace",
                 "marketplace_name": "nova-marketplace"}]
    repo = _repo(tmp_path, "eva", required=required)
    reg = tmp_path / "reg.json"
    # chrome-sales installed under a DIFFERENT marketplace suffix: doctor passes it, so
    # bootstrap must not reinstall it.
    reg.write_text(json.dumps({"plugins": {"chrome-sales@elsewhere": [{}]}}))
    installed = ab.installed_plugin_names(str(reg))
    actions, problems, n_ok = ab.plugin_plan(repo, installed, {"nova-marketplace"})
    assert problems == [] and n_ok == 1
    assert [(a.plugin, a.marketplace, a.add_marketplace) for a in actions] == [
        ("eva@eva", "dimagi-internal/eva", True),
        ("nova@nova-marketplace", "voidcraft-labs/nova-marketplace", False)]
    doctor = check_required_plugins(repo, registry_path=str(reg))
    assert not doctor.ok and "nova" in doctor.detail and "chrome-sales:" not in doctor.detail


def test_bare_required_plugin_without_marketplace_is_a_problem_not_a_guess(tmp_path):
    repo = _repo(tmp_path, "eva", required=["mystery"])
    actions, problems, _ = ab.plugin_plan(repo, {"eva"}, set())
    assert actions == [] and "mystery" in problems[0]


def test_plugins_installed_via_claude_cli(tmp_path):
    repo = _repo(tmp_path, "eva", required=[{"name": "chrome-sales",
                                              "marketplace": "dimagi-internal/chrome-sales"}])
    runner = FakeRunner(op={"op://Agent-Eva/gog-token/credential":
                            _tok(email="eva@dimagi-ai.com"),
                            "op://Canopy-Shared/gog-oauth-client/credential": "{}"})
    rep = _boot(tmp_path, runner, installed=("eva",)).bootstrap_one("eva", repo)
    assert ["claude", "plugin", "marketplace", "add", "dimagi-internal/chrome-sales"] in runner.calls
    assert ["claude", "plugin", "install", "chrome-sales@chrome-sales"] in runner.calls
    assert rep.plugins.startswith("2/2")


# ── dry run + isolation ─────────────────────────────────────────────────────

def test_dry_run_touches_nothing(tmp_path):
    repo = _repo(tmp_path, "eva", required=[{"name": "chrome-sales",
                                              "marketplace": "dimagi-internal/chrome-sales"}])
    runner = FakeRunner()
    b = _boot(tmp_path, runner, dry_run=True, web={"eva": _tok(email="eva@dimagi-ai.com")})
    rep = b.bootstrap_one("eva", repo)
    mutating = [c for c in runner.calls
                if c[0] == "claude" or c[:2] == ["op", "inject"] or c[:3] == ["gog", "auth", "tokens"]]
    assert mutating == []
    assert not (tmp_path / "gog").exists() and not (tmp_path / "home").exists()
    assert b.verified == []
    assert "would import" in rep.token and rep.env == "would inject"


def test_env_injected_with_private_mode(tmp_path):
    repo = _repo(tmp_path)
    runner = FakeRunner(op={"op://Agent-Ace/gog-token/credential": _tok(),
                            "op://Canopy-Shared/gog-oauth-client/credential": "{}"})
    rep = _boot(tmp_path, runner, installed=("ace",)).bootstrap_one("ace", repo)
    env = tmp_path / "home" / ".ace" / ".env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600 and rep.env == "OK (2 vars)"
    inject = next(c for c in runner.calls if c[:2] == ["op", "inject"])
    assert inject[inject.index("--account") + 1] == "dimagi.1password.com"


def test_one_failing_step_does_not_stop_the_rest(tmp_path):
    repo = _repo(tmp_path)
    runner = FakeRunner()
    orig = runner.__call__

    def flaky(argv, **kw):
        if argv[:2] == ["op", "inject"]:
            return subprocess.CompletedProcess(argv, 1, "", "[ERROR] could not resolve op://x/y/z")
        return orig(argv, **kw)

    b = _boot(tmp_path, flaky, installed=("ace",), web={"ace": _tok()})
    rep = b.bootstrap_one("ace", repo)
    assert rep.env == "FAIL" and not rep.ok
    assert rep.gmail.startswith("LIVE")        # token + verify still ran


def test_missing_repo_is_reported(tmp_path):
    rep = _boot(tmp_path, FakeRunner()).bootstrap_one("zed", None)
    assert not rep.ok and "gh repo clone dimagi-internal/zed" in rep.notes[0]


def test_render_table_never_contains_the_secret(tmp_path):
    repo = _repo(tmp_path)
    runner = FakeRunner(op={"op://Agent-Ace/gog-token/credential": _tok(),
                            "op://Canopy-Shared/gog-oauth-client/credential": "{}"})
    rep = _boot(tmp_path, runner, installed=("ace",)).bootstrap_one("ace", repo)
    out = ab.render_table([rep])
    assert "SECRET-REFRESH" not in out and "ace" in out


def test_locked_1password_is_reported_once_not_waited_out_per_call(tmp_path):
    repo = _repo(tmp_path)
    calls = []

    def locked(argv, **kw):
        calls.append(argv)
        if argv[0] == "op":
            return subprocess.CompletedProcess(
                argv, 1, "", "[ERROR] error initializing client: authorization timeout")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"accounts": []}), "")

    b = _boot(tmp_path, locked, installed=("ace",))
    r1 = b.bootstrap_one("ace", repo)
    r2 = b.bootstrap_one("ace", repo)
    assert sum(c[0] == "op" for c in calls) == 1           # first refusal short-circuits the rest
    assert r1.env == "FAIL" and "unlock the 1Password app" in " ".join(r1.notes + r2.notes)


def test_never_downgrades_a_newer_token_gog_already_holds(tmp_path):
    repo = _repo(tmp_path)
    (tmp_path / "gog").mkdir()
    (tmp_path / "gog" / "credentials-canopy.json").write_text("{}")
    runner = FakeRunner(
        gog_accounts=[{"email": "ace@dimagi-ai.com", "client": "canopy",
                       "created_at": "2026-09-07T14:04:13Z"}])
    rep = _boot(tmp_path, runner, installed=("ace",),
                web={"ace": _tok(created="2026-05-01T00:00:00Z")}).bootstrap_one("ace", repo)
    assert runner.imported == [] and "(kept newer)" in rep.token


# ── the client file comes from the workspace's canopy vault ─────────────────

def test_unreadable_canopy_vault_client_is_named_and_nothing_is_written(tmp_path):
    repo = _repo(tmp_path)
    runner = FakeRunner(gog_accounts=[])
    rep = _boot(tmp_path, runner, web={"ace": _tok()}, clients={},
                installed=("ace",)).bootstrap_one("ace", repo)
    assert not rep.ok
    assert any("canopy vault" in n and "gog-oauth-client" in n for n in rep.notes)
    assert not (tmp_path / "gog" / "credentials-canopy.json").exists()
    assert not any(c[0] == "op" and c[1] == "read" for c in runner.calls)


def test_a_non_fleet_client_token_is_told_to_re_mint(tmp_path):
    repo = _repo(tmp_path, gog_client="ace")
    rep = _boot(tmp_path, FakeRunner(gog_accounts=[]), web={"ace": _tok(client="ace")},
                installed=("ace",)).bootstrap_one("ace", repo)
    assert not rep.ok and any("not a fleet client" in n for n in rep.notes)


def test_client_file_is_written_to_gogs_data_dir_under_gog_home(tmp_path, monkeypatch):
    # cloud-ec2-2, 2026-10-26: a per-agent $GOG_HOME reads <home>/data/credentials-*.json,
    # and a box-wide XDG copy must not count as this home's.
    import sys as _sys
    monkeypatch.setattr(_sys, "platform", "linux")      # the cloud box: XDG dirs in play
    home = tmp_path / "gog"
    monkeypatch.setenv("GOG_HOME", str(home))
    xdg = tmp_path / "xdg"
    (xdg / "gogcli").mkdir(parents=True)
    (xdg / "gogcli" / "credentials-canopy.json").write_text("{}")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    repo = _repo(tmp_path)
    _boot(tmp_path, FakeRunner(gog_accounts=[]), web={"ace": _tok()},
          installed=("ace",)).bootstrap_one("ace", repo)
    assert (home / "data" / "credentials-canopy.json").read_text() == VAULT_CLIENTS["canopy"]


def test_a_legacy_home_copy_does_not_count_and_the_data_copy_is_written(tmp_path, monkeypatch):
    # cloud-ec2-2, 2026-10-26: ace and ada kept the <home>/credentials-canopy.json an earlier
    # bootstrap wrote; it satisfied the check, the data copy gog reads was never written, and
    # their mail stayed dead while eva (no legacy copy) worked.
    import sys as _sys
    monkeypatch.setattr(_sys, "platform", "linux")
    home = tmp_path / "gog"
    monkeypatch.setenv("GOG_HOME", str(home))
    home.mkdir(parents=True)
    (home / "credentials-canopy.json").write_text("{}")   # the stale legacy copy
    repo = _repo(tmp_path)
    _boot(tmp_path, FakeRunner(gog_accounts=[]), web={"ace": _tok()},
          installed=("ace",)).bootstrap_one("ace", repo)
    assert (home / "data" / "credentials-canopy.json").read_text() == VAULT_CLIENTS["canopy"]


def test_client_creds_in_gogs_data_dir_count_as_present(tmp_path):
    # gog >= 0.40 writes credentials to <home>/data/, not the config dir.
    repo = _repo(tmp_path)
    (tmp_path / "gog" / "data").mkdir(parents=True)
    (tmp_path / "gog" / "data" / "credentials-canopy.json").write_text("{}")
    asked = []
    b = _boot(tmp_path, FakeRunner(gog_accounts=[]), web={"ace": _tok()}, installed=("ace",))
    b.client_from_vault = lambda slug, client: asked.append(client)
    b.bootstrap_one("ace", repo)
    assert asked == []


# ── the vault comes from the instance (2026-10-14) ───────────────────────────

def test_env_inject_sets_agent_vault_from_the_recorded_instance(tmp_path):
    repo = _repo(tmp_path, "ace")
    runner = FakeRunner(op={"op://Some-Vault/gog-token/credential": _tok()})
    b = _boot(tmp_path, runner, vaults={"ace": "Some-Vault"})
    rep = ab.AgentReport(slug="ace")
    b.step_env("ace", repo, rep)
    i = next(n for n, c in enumerate(runner.calls) if c[:2] == ["op", "inject"])
    assert runner.envs[i]["AGENT_VAULT"] == "Some-Vault"
    assert runner.envs[i]["AGENT_SLUG"] == "ace"
    assert rep.env.startswith("OK")


def test_no_recorded_vault_fails_loudly_and_injects_nothing(tmp_path):
    repo = _repo(tmp_path, "ace")
    runner = FakeRunner()
    b = _boot(tmp_path, runner, vaults={})
    rep = b.bootstrap_one("ace", repo)
    assert not rep.ok and rep.env == "FAIL"
    assert any("no 1Password vault recorded on canopy-web" in n for n in rep.notes)
    assert not any(c[:2] == ["op", "inject"] for c in runner.calls)
    assert not any("Agent-Ace" in " ".join(c) for c in runner.calls)


# ── a canopy-hosted mailbox has no gog token ────────────────────────────────

def test_a_hosted_mailbox_asks_nobody_for_a_gog_token(tmp_path):
    """bailey, 2026-10-25: its own turn's bootstrap asked canopy-web for `gog-token`, which a
    hosted agent never declares — an owner alert — and then failed it for having none."""
    from orchestrator.agent_email import EmailIdentity
    repo = _repo(tmp_path)
    runner = FakeRunner()
    asked = []
    b = _boot(tmp_path, runner, installed=("ace",))
    b.web_token = lambda slug: asked.append(slug) or (None, "")
    b.hosted = lambda r: EmailIdentity(slug="ace", account="ace@marshellis.com",
                                       client="canopy", repo=Path(r))
    rep = b.bootstrap_one("ace", repo)
    assert asked == [] and b.verified == []
    assert not any(a[:2] == ["op", "read"] and "gog-token" in a[2] for a in runner.calls)
    assert rep.ok and (rep.token, rep.gmail) == ("n/a", "canopy-hosted")
