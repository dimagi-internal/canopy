"""`canopy agent prepare` — make ONE agent runnable on THIS machine, the same way everywhere.

    canopy agent prepare --slug ace --runner [--agent-root /opt/agents] [--credentials-only]
                         [--env-only] [--report] [--json]
    canopy agent bootstrap [--slug …]          # a PERSON's laptop: the same steps, person mode

WHY ONE IMPLEMENTATION (2026-10-15, docs/architecture/agent-setup-lifecycle.md phase 2).
Preparing an agent had five homes: canopy-web's runner/ec2/bootstrap_agents.sh (1,400 lines
of bash, a fleet-wide batch at box start), cloud_runner.py's per-claim patches (env refresh,
lazy clone, pull), this module's laptop twin, `canopy agent env`, and an unwired runtime
reconciler. The laptop runner did none of it. Every lesson in that bash — the token names its
client, newest token wins, the shared client needs the TENANT's key, keep op's stderr, a
stale clone is still a clone, readiness is a CALL — is kept here, once, and every runner calls
this per agent.

TWO CREDENTIAL MODES, one set of steps:
  person  — `canopy agent bootstrap`: `op` as the signed-in person, the instance's vault
            from canopy-web, the shared clients from Canopy-Shared. (agent_bootstrap.py)
  runner  — `--runner`: everything from canopy-web's `/api/agents/<slug>/credentials/resolve`,
            asked with the RUNNER's token (CANOPY_TOKEN): the agent's vault + its own
            read-only key, the workspace's shared vault + ITS key, the owner's lent GitHub
            token, the instance's mailbox, canopy-web's gog-token. Every `op` call names the
            one key it is made with (OP_SERVICE_ACCOUNT_TOKEN, scoped to that call), so
            "which identity read this secret" always has an answer and nothing downstream
            inherits a key that spans agents.

RUNNER STEPS, in order (each says what it did; one failing never stops the rest):
  1. repo — clone, or `git pull --ff-only`, with the owner's GitHub token on that one git
     command only. A pull that fails on an existing clone provisions the clone as it is.
  2. env — `op inject` the agent's .env.tpl into ~/.<slug>/.env with ITS key.
  3. plugins — its own plugin from the clone (a directory marketplace: the pull is the
     update); each required plugin cloned beside the CLI's marketplaces, pulled every pass,
     node deps installed, its declared key files staged from the two vaults this box may
     read; then `bin/<slug>-setup` with only this agent's key.
  4. gmail — the token's own client materialized from the right vault with the right key,
     the NEWEST of the vault's and canopy-web's tokens imported, the account mapped, then
     verified by making the call; turn-client compared with what turns present.
  5. report — POST /api/agents/<slug>/bootstrap-report: what this box could actually do.
`--credentials-only` does 4 + 5 only (no clone, no re-provision under a live agent);
`--env-only` does 2 only (a claim's cheap refresh).
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from orchestrator import canopy_web
from orchestrator.agent_bootstrap import (
    AgentReport,
    Bootstrapper,
    _first_line,
    _write_private,
)
from orchestrator.agent_doctor import declared_required_plugins

#: The fleet's shared OAuth clients and their item in the WORKSPACE's shared vault.
SHARED_CLIENT_ITEMS = {"canopy": "gog-oauth-client", "canopy-web": "gog-oauth-client-web"}
GIT_HELPER = ('!f() { test "$1" = get || exit 0; [ -n "$GH_TOKEN" ] || exit 0; '
              'echo username=x-access-token; echo "password=$GH_TOKEN"; }; f')

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class RunnerCredentials:
    """What canopy-web lends a runner for one agent. Any field may be blank."""
    vault: str = ""
    key: str = field(default="", repr=False)
    shared_vault: str = ""
    shared_key: str = field(default="", repr=False)
    github_token: str = field(default="", repr=False)
    mailbox: str = ""
    web_gog_token: Optional[str] = field(default=None, repr=False)
    repo_url: str = ""
    error: str = ""
    #: The agent's backend (canopy#850/#946): "canopy-web" means its env comes from
    #: `values` below, not from `op inject` — canopy-web withholds the vault key then.
    credential_source: str = ""
    values: dict = field(default_factory=dict, repr=False)


def fetch_runner_credentials(slug: str, *, base_url: str, token: str,
                             transport: Optional[canopy_web.Transport] = None
                             ) -> RunnerCredentials:
    """`/credentials/resolve` with the runner's token. Unreachable is not unregistered: it
    returns an `error` the report states, so a human retries instead of registering."""
    try:
        from orchestrator import cred_resolve
        # A box staging an agent outside any turn (bootstrap, the runner-update timer)
        # says so, or canopy-web emails the owner "read outside a turn" for routine
        # maintenance every hour (canopy-web#1563).
        body = cred_resolve.resolve(slug, base_url=base_url, token=token,
                                    transport=transport,
                                    purpose=cred_resolve.RUNNER_REFRESH) or {}
    except Exception as e:  # noqa: BLE001
        return RunnerCredentials(error=f"could not reach canopy-web for {slug}'s credentials: "
                                       f"{_first_line(str(e), 200)}")
    # Where the agent's code lives — canopy-web's record (`/runtime`), never a convention: an
    # agent in another org or under another name would otherwise clone the wrong repo.
    try:
        rt = canopy_web.call("GET", f"/api/agents/{slug}/runtime", base_url=base_url,
                             token=token, transport=transport) or {}
        repo_url = str(rt.get("repo_url") or "").strip()
    except Exception:  # noqa: BLE001 — the report says "no repo" if it matters
        repo_url = ""
    try:
        acc = canopy_web.call("GET", f"/api/agents/{slug}/credentials/access",
                              base_url=base_url, token=token, transport=transport) or {}
        source = str(acc.get("credential_source") or "")
    except Exception:  # noqa: BLE001 — unknown backend = the default path (op inject)
        source = ""
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    return RunnerCredentials(
        credential_source=source, values=values,
        repo_url=repo_url,
        vault=str(body.get("op_vault") or ""), key=str(body.get("op_sa_token") or ""),
        shared_vault=str(body.get("shared_op_vault") or ""),
        shared_key=str(body.get("shared_op_sa_token") or ""),
        github_token=str(body.get("github_token") or ""), mailbox=str(body.get("mailbox") or ""),
        web_gog_token=((body.get("values") or {}).get("gog-token")) or None)


@dataclass
class RunnerReport(AgentReport):
    """AgentReport plus the readiness facts canopy-web stores (AgentBootstrapReport).
    Tri-state where the box may not have checked: None means "did not say", never False."""
    client_creds_ok: bool = False
    mailbox_ok: bool = False
    gog_client: str = ""
    turn_client: str = ""
    turn_ready: Optional[bool] = None
    env_ok: Optional[bool] = None

    def detail(self) -> str:
        return "; ".join(n for n in self.notes if not n.startswith("ok: "))[:2000]

    def payload(self, runner_name: str) -> dict:
        return {"runner_name": runner_name, "client_creds_ok": self.client_creds_ok,
                "mailbox_ok": self.mailbox_ok, "gog_client": self.gog_client,
                "turn_client": self.turn_client, "turn_ready": self.turn_ready,
                "env_ok": self.env_ok, "detail": self.detail()}


def turn_readiness(repo: Path, tok, mailbox_ok: bool) -> tuple[str, Optional[bool]]:
    """(the client turns will present, can they read the mailbox). Turns ask for what
    config/agent.json declares; either FLEET client serves a fleet declaration (one app,
    two sign-in doors — reconcile_client), so then they present the live token's. None
    when nothing was checked: never report "broken" for "did not look" (2026-09-08)."""
    from orchestrator.agent_email import AgentEmailError, is_fleet_client, resolve_email_identity

    try:
        declared = resolve_email_identity(Path(repo)).client
    except AgentEmailError:
        return "", None
    if tok is None or not tok.client:
        return declared, None
    if tok.client == declared or (is_fleet_client(declared) and is_fleet_client(tok.client)):
        return tok.client, bool(mailbox_ok)
    return declared, False


class RunnerPreparer(Bootstrapper):
    """The runner mode. Same steps as a person's bootstrap; different keys, plus what only a
    machine that RUNS turns needs (the clone, directory plugins, the provisioner, a report)."""

    def __init__(self, *, base_url: str, token: str, agent_root: Path,
                 plugin_deps_root: Optional[Path] = None,
                 transport: Optional[canopy_web.Transport] = None,
                 creds_for: Optional[Callable[[str], RunnerCredentials]] = None,
                 runner_name: str = "", **kw):
        self.base_url, self.token, self.transport = base_url, token, transport
        self.agent_root = Path(agent_root)
        self.plugin_deps_root = Path(plugin_deps_root or
                                     Path.home() / ".claude" / "plugins" / "agent-deps")
        self.runner_name = runner_name or os.uname().nodename
        self._creds_for = creds_for or (lambda s: fetch_runner_credentials(
            s, base_url=base_url, token=token, transport=transport))
        self._creds: dict[str, RunnerCredentials] = {}
        self._slug = ""
        super().__init__(vault_for=self._vault_for, web_token=self._web_token, **kw)

    # ── credentials ──────────────────────────────────────────────────────────
    def creds(self, slug: str) -> RunnerCredentials:
        if slug not in self._creds:
            self._creds[slug] = self._creds_for(slug)
        return self._creds[slug]

    def _vault_for(self, slug: str) -> str:
        c = self.creds(slug)
        if c.error:
            raise RuntimeError(c.error)
        if not c.vault or not c.key:
            raise RuntimeError(f"canopy-web has {'no vault' if not c.vault else 'no key'} for "
                               f"{slug} (PUT /api/agents/{slug}/vault — `canopy agent vault`)")
        return c.vault

    def _web_token(self, slug: str) -> tuple[Optional[str], str]:
        c = self.creds(slug)
        return c.web_gog_token, c.error

    def op_argv(self, argv: list[str]) -> list[str]:
        return list(argv)   # a service-account key needs no --account

    def op_env(self, vault: str, base: Optional[dict] = None) -> dict:
        """op's environment with exactly ONE key: the agent's for its vault, the
        workspace's for the shared vault — never the box's, never another agent's."""
        c = self.creds(self._slug)
        env = {k: v for k, v in (base if base is not None else os.environ).items()
               if not k.startswith("OP_SESSION_") and k != "OP_SERVICE_ACCOUNT_TOKEN"}
        if vault and vault == c.shared_vault:
            key = c.shared_key
        elif vault and vault == c.vault:
            key = c.key
        else:
            key = ""
        if key:
            env["OP_SERVICE_ACCOUNT_TOKEN"] = key
        return env

    def client_ref(self, client: str, vault: str) -> str:
        """A shared client from the WORKSPACE's shared vault (read with its own key), any
        other from the agent's vault. Measured 2026-09-07: one pass read Agent-Ace fine and
        failed Canopy-Shared — a per-agent key cannot read the shared vault, by design."""
        item = SHARED_CLIENT_ITEMS.get(client)
        if item:
            return f"op://{self.creds(self._slug).shared_vault or '<no shared vault>'}/{item}/credential"
        return f"op://{vault}/gog-oauth-client/credential"

    def expected_mailbox(self, slug: str, repo: Path) -> str:
        """THIS instance's mailbox (canopy-web `Agent.email`), never the repo's or the
        slug's: instances share a repo, so either would point two at one inbox (#984)."""
        return self.creds(slug).mailbox.lower()

    # ── runner-only steps ────────────────────────────────────────────────────
    def _git(self, args: list[str], github_token: str, timeout: int = 300):
        env = {**os.environ, "GH_TOKEN": github_token}
        return self.run(["git", "-c", "credential.https://github.com.helper=",
                         "-c", f"credential.https://github.com.helper={GIT_HELPER}", *args],
                        capture_output=True, text=True, timeout=timeout, env=env)

    def clone_or_pull(self, url: str, dest: Path, github_token: str) -> str:
        """'' on success, else git's own error line."""
        try:
            if (dest / ".git").is_dir():
                r = self._git(["-C", str(dest), "pull", "--ff-only"], github_token)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                r = self._git(["clone", "--depth", "1", url, str(dest)], github_token)
        except (OSError, subprocess.TimeoutExpired) as e:
            return str(e)
        return "" if r.returncode == 0 else _first_line(r.stderr or r.stdout, 300)

    def step_repo(self, slug: str, repo_url: str, rep: RunnerReport) -> Optional[Path]:
        dest = self.agent_root / slug
        url = repo_url or self.creds(slug).repo_url
        if not url:
            if (dest / ".git").is_dir():
                rep.notes.append("repo not updated: canopy-web records no repository for "
                                 f"{slug} — provisioning the clone as it is")
                return dest
            rep.fail(f"repo: canopy-web records no repository for {slug} — set it on the "
                     "agent's settings")
            return None
        if self.dry_run:
            rep.notes.append(f"repo: would {'pull' if (dest / '.git').is_dir() else 'clone'} {url}")
            return dest if (dest / ".git").is_dir() else None
        err = self.clone_or_pull(url.removesuffix(".git") + ".git", dest,
                                 self.creds(slug).github_token)
        if err and (dest / ".git").is_dir():
            # A clone that could not be UPDATED is still a clone — provision it as it is,
            # the way a turn runs in a clone whose pull failed (#965).
            rep.notes.append(f"repo not updated: {err} — its owner may have lent no GitHub "
                             "token that can read it (Settings → Credentials → GitHub)")
        elif err:
            rep.fail(f"repo: clone of {url} failed: {err}")
            return None
        return dest

    def _claude(self, args: list[str], timeout: int = 600) -> str:
        try:
            r = self.run(["claude", "plugin", *args], capture_output=True, text=True,
                         timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"claude plugin {' '.join(args)}: {e}"
        return "" if r.returncode == 0 else f"claude plugin {' '.join(args)}: " + _first_line(
            r.stderr or r.stdout)

    def _listed(self, args: list[str]) -> str:
        try:
            r = self.run(["claude", "plugin", *args], capture_output=True, text=True, timeout=120)
            return r.stdout or ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def install_dir_plugin(self, name: str, marketplace_name: str, path: Path) -> str:
        """A plugin whose marketplace IS a clone on this box: the pull is the update."""
        markets = self._listed(["marketplace", "list"]).split()
        if marketplace_name not in markets:
            err = self._claude(["marketplace", "add", str(path)])
            if err:
                return err
        if f"{name}@{marketplace_name}" in self._listed(["list"]):
            return ""
        return self._claude(["install", f"{name}@{marketplace_name}"])

    def node_deps(self, path: Path) -> str:
        """`npm ci` (or install) when there are no node_modules, or the lockfile is newer than
        the one npm last installed from — a dependency added upstream otherwise never
        arrives and the MCP server dies at import (canopy-web#1237)."""
        if not (path / "package.json").is_file():
            return ""
        nm, lock = path / "node_modules", path / "package-lock.json"
        hidden = nm / ".package-lock.json"
        if nm.is_dir() and not (lock.is_file() and hidden.is_file()
                                and lock.stat().st_mtime > hidden.stat().st_mtime):
            return ""
        verb = "ci" if lock.is_file() else "install"
        try:
            r = self.run(["npm", verb, "--silent"], cwd=str(path), capture_output=True,
                         text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"npm {verb} in {path}: {e}"
        return "" if r.returncode == 0 else f"npm {verb} in {path}: " + _first_line(r.stderr)

    def stage_plugin_secrets(self, slug: str, name: str, path: Path, rep: RunnerReport) -> None:
        """A dependency plugin's declared key FILES (config/secrets.yaml, required, inside
        the plugin) from the two vaults this box may read, each with its own key."""
        spec = path / "config" / "secrets.yaml"
        if not spec.is_file():
            return
        try:
            import yaml
            data = yaml.safe_load(spec.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a dependency's secrets list never sinks the agent
            return
        c = self.creds(slug)
        for entry in (data or {}).get("secrets") or [] if isinstance(data, dict) else []:
            if not isinstance(entry, dict) or entry.get("optional") is True:
                continue
            sname, ref = str(entry.get("name") or ""), str(entry.get("op") or "")
            target, mode = str(entry.get("target") or ""), str(entry.get("mode") or "0600")
            if not (sname and ref.startswith("op://") and target.startswith("{repo}/")):
                continue
            item = ref[len("op://"):].split("/", 1)[1] if "/" in ref[len("op://"):] else ""
            rel = target[len("{repo}/"):]
            if not item or not rel or rel.startswith("/") or ".." in rel.split("/"):
                continue
            dest = path / rel
            if dest.is_file() and dest.stat().st_size:
                continue
            tried = []
            for vault in (c.vault, c.shared_vault):
                if not vault:
                    continue
                tried.append(f"op://{vault}/{item}")
                value, _ = self._op_read(f"op://{vault}/{item}")
                if value:
                    if not self.dry_run:
                        _write_private(dest, value)
                        os.chmod(dest, int(mode, 8))
                    break
            else:
                rep.notes.append(f"{name}: secret '{sname}' is in neither vault this box reads "
                                 f"({', '.join(tried) or 'none registered'}) — share 1Password "
                                 f"item '{item.split('/')[0]}' into the workspace's shared vault")

    def step_runner_plugins(self, slug: str, repo: Path, rep: RunnerReport) -> None:
        installed = 0
        problems: list[str] = []
        manifest = repo / ".claude-plugin" / "marketplace.json"
        if manifest.is_file():
            try:
                mname = json.loads(manifest.read_text(encoding="utf-8")).get("name") or slug
            except (OSError, ValueError):
                mname = slug
            if self.dry_run:
                rep.notes.append(f"plugins: would install {slug}@{mname} from {repo}")
            elif err := self.install_dir_plugin(slug, mname, repo):
                problems.append(err)
            else:
                installed += 1
        specs, bad = declared_required_plugins(repo)
        problems += [f"required_plugins entry with no name: {b!r}" for b in bad]
        for s in specs:
            if not s.marketplace:
                problems.append(f"{s.name}: declares no marketplace (owner/repo)")
                continue
            pdir = self.plugin_deps_root / s.name
            if self.dry_run:
                rep.notes.append(f"plugins: would clone/pull {s.marketplace} and install "
                                 f"{s.name}@{s.marketplace_name}")
                continue
            # Pulled EVERY pass, installed or not — a plugin installed once was never updated
            # again, and never got its node deps (canopy-web#1237).
            err = self.clone_or_pull(f"https://github.com/{s.marketplace}.git", pdir,
                                     self.creds(slug).github_token)
            if err and not (pdir / ".git").is_dir():
                problems.append(f"{s.name}: clone of {s.marketplace} failed: {err}")
                continue
            if err := self.node_deps(pdir):
                problems.append(err)
            self.stage_plugin_secrets(slug, s.name, pdir, rep)
            if err := self.install_dir_plugin(s.name, s.marketplace_name, pdir):
                problems.append(err)
            else:
                installed += 1
                if s.note:
                    rep.notes.append(f"plugins: follow-up for {s.name}: {s.note}")
        for p in problems:
            rep.fail(f"plugins: {p}")
        rep.plugins = f"{installed} ok" + (" FAIL" if problems else "")

    def step_provisioner(self, slug: str, repo: Path, rep: RunnerReport) -> None:
        """The agent's own installer, with THIS agent's key and no other. Then mirror its
        data-dir key file to the plugin root, where a directory-source plugin's MCP server
        looks for it (otherwise every Drive call fails "unregistered callers")."""
        if err := self.node_deps(repo):
            rep.notes.append(f"provisioner: {err}")
        setup = repo / "bin" / f"{slug}-setup"
        if not setup.is_file() or self.dry_run:
            return
        data_dir = self.home / ".claude" / "plugins" / "data" / f"{slug}-{slug}"
        data_dir.mkdir(parents=True, exist_ok=True)
        env = self.op_env(self.creds(slug).vault,
                          {**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)})
        try:
            r = self.run(["bash", str(setup), "--skip-doctor"], capture_output=True, text=True,
                         timeout=900, env=env, cwd=str(repo))
            if r.returncode != 0:
                rep.notes.append(f"provisioner: bin/{slug}-setup failed: "
                                 f"{_first_line(r.stderr or r.stdout)}")
        except (OSError, subprocess.TimeoutExpired) as e:
            rep.notes.append(f"provisioner: bin/{slug}-setup: {e}")
        for name, dest in (("gws-sa-key.json", repo / ".gws-sa-key.json"),
                           (".env", repo / ".env")):
            src = data_dir / name
            if src.is_file() and (name != ".env" or not dest.exists()):
                _write_private(dest, src.read_text(encoding="utf-8"))

    def report(self, slug: str, rep: RunnerReport) -> None:
        """Tell canopy-web what this box could actually do. Best-effort: a box that cannot
        reach the control plane still finishes; a missing report reads as "not said"."""
        if self.dry_run:
            return
        try:
            canopy_web.call("POST", f"/api/agents/{slug}/bootstrap-report",
                            rep.payload(self.runner_name), base_url=self.base_url,
                            token=self.token, transport=self.transport)
        except Exception as e:  # noqa: BLE001
            rep.notes.append(f"report: could not reach canopy-web ({_first_line(str(e))})")

    # ── orchestration ────────────────────────────────────────────────────────
    def prepare(self, slug: str, *, repo_url: str = "", credentials_only: bool = False,
                env_only: bool = False, report: bool = True) -> RunnerReport:
        self._slug = slug
        rep = RunnerReport(slug=slug)
        c = self.creds(slug)
        if c.error:
            rep.fail(c.error)
        elif c.credential_source == "canopy-web":
            pass  # no vault key on purpose: its env comes from canopy-web
        elif not (c.vault and c.key):
            rep.notes.append(f"canopy-web has {'no vault' if not c.vault else 'no key'} for "
                             f"{slug} — `canopy agent vault --slug {slug}`")
        repo = self.agent_root / slug
        if env_only:
            self._env_sync(slug, repo, rep)
            return rep
        if not credentials_only:
            repo = self.step_repo(slug, repo_url, rep)
            if repo is None:
                if report:
                    self.report(slug, rep)
                return rep
            self._env(slug, repo, rep)
            for step in (lambda: self.step_runner_plugins(slug, repo, rep),
                         lambda: self.step_provisioner(slug, repo, rep)):
                try:
                    step()
                except Exception as e:  # noqa: BLE001 — one step must not sink the agent
                    rep.fail(f"unexpected error: {_first_line(str(e))}")
        if c.mailbox:
            tok = None
            try:
                tok = self.step_token(slug, repo, rep)
            except Exception as e:  # noqa: BLE001
                rep.fail(f"token: unexpected error: {_first_line(str(e))}")
            if tok is not None:
                rep.gog_client = tok.client
                rep.client_creds_ok = (self.gog_dir / f"credentials-{tok.client}.json").is_file()
            try:
                self.step_verify(repo, tok, rep)
            except Exception as e:  # noqa: BLE001
                rep.fail(f"verify: unexpected error: {_first_line(str(e))}")
            rep.mailbox_ok = rep.gmail.startswith("LIVE")
            rep.turn_client, rep.turn_ready = turn_readiness(repo, tok, rep.mailbox_ok)
        else:
            rep.notes.append(f"canopy-web records no mailbox for {slug} — Gmail not set up")
        if report:
            self.report(slug, rep)
        return rep

    def _env_sync(self, slug: str, repo: Path, rep: RunnerReport) -> None:
        """The claim-time refresh: canopy's `cred sync` (the function every agent session's
        start hook runs) — re-inject only when the vault or template changed, keep the last
        good file on failure. ONE implementation; cloud_runner used to carry its own."""
        from orchestrator.cred_cli import CredError, sync_env

        c = self.creds(slug)
        if c.credential_source == "canopy-web" and not c.error:
            self._env_from_web(slug, c, rep)
            return
        if not (c.vault and c.key):
            rep.env, rep.env_ok = "skipped", None
            return
        saved = {k: os.environ.get(k) for k in ("OP_SERVICE_ACCOUNT_TOKEN", "AGENT_VAULT",
                                                  "AGENT_SLUG")}
        os.environ.update(OP_SERVICE_ACCOUNT_TOKEN=c.key, AGENT_VAULT=c.vault, AGENT_SLUG=slug)
        try:
            rep.env = sync_env(slug, ttl=0, runner=self.run, repo=repo)
            rep.env_ok = True
        except CredError as e:
            rep.env, rep.env_ok = "FAIL", False
            rep.fail(f"env: {e}")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def _env_from_web(self, slug: str, c: RunnerCredentials, rep: RunnerReport) -> None:
        """An agent on the canopy-web backend: ~/.<slug>/.env from the values canopy-web
        resolved for this runner — the same file `canopy cred env` writes."""
        from orchestrator.cred_cli import write_env_from_values

        if self.dry_run:
            rep.env, rep.env_ok = "would write from canopy-web", None
            return
        try:
            n, _ = write_env_from_values(slug, self.home / f".{slug}" / ".env", c.values)
        except OSError as e:
            rep.env, rep.env_ok = "FAIL", False
            rep.fail(f"env: could not write from canopy-web: {e}")
            return
        rep.env, rep.env_ok = f"OK ({n} vars from canopy-web)", True

    def _env(self, slug: str, repo: Path, rep: RunnerReport) -> None:
        c = self.creds(slug)
        if c.credential_source == "canopy-web" and not c.error:
            self._env_from_web(slug, c, rep)
            return
        try:
            self.step_env(slug, repo, rep)
        except Exception as e:  # noqa: BLE001
            rep.fail(f"env: unexpected error: {_first_line(str(e))}")
        rep.env_ok = None if rep.env in ("n/a", "-", "would inject") else rep.env.startswith("OK")
