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

SECRETS OFF DISK (opt-in; runner isolation units, spec §4). With `$CANOPY_TURN_ENV_ROOT`
set (the runner's tmpfs root for per-turn env files) or `--no-secret-files`, step 2
resolves the values IN MEMORY and checks them, and writes no `~/.<slug>/.env`, no
`<clone>/.env` and no `.gws-sa-key.json` copy. `bin/<slug>-setup` gets its env through a
0600 file under that root that is removed as soon as it exits. The runner writes each
turn's env itself at claim (`canopy cred env --runner --out …`). Without either, nothing
changes: the live fleet keeps its files until its runner opts in.
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
    #: Where `mailbox` lives (canopy-web): "ses" = canopy-hosted — mail goes through
    #: canopy-web (`canopy email`), there is no Gmail to sign in; "google"/"" = Gmail via gog.
    mailbox_backend: str = ""
    #: The agent's Google account (Drive/Docs/gog), when it is not `mailbox` — an
    #: SES-hosted primary has no Google account behind it. "" (or an older canopy-web
    #: that does not send it) = the Google account IS `mailbox`.
    google_account: str = ""
    web_gog_token: Optional[str] = field(default=None, repr=False)
    repo_url: str = ""
    error: str = ""
    #: The agent's backend (canopy#850/#946): "canopy-web" means its env comes from
    #: `values` below, not from `op inject` — canopy-web withholds the vault key then.
    credential_source: str = ""
    values: dict = field(default_factory=dict, repr=False)
    #: The resolve body's top-level secrets a runner hands a turn as-is (`cred env --runner
    #: --print-json`), each only when the resolve returned it: RESOLVE_EXTRAS.
    extras: dict = field(default_factory=dict, repr=False)


#: Top-level fields of the /credentials/resolve body (its own names) that
#: `cred env --runner --print-json` passes through beside `values`.
RESOLVE_EXTRAS = ("op_sa_token", "op_vault", "salesforce_creds")


#: What the credentials-only pass (the runner-update timer, every 30 minutes) actually
#: uses: the vault keys `op` reads the gog token and OAuth client with, and canopy-web's
#: copy of the gog token for newest-wins. Never the agent's other secrets — it writes no
#: env (a turn's claim does, naming its turn), so reading them every 30 minutes outside a
#: turn only put them on the wire (canopy-web#1563).
REFRESH_EXTRAS = ("op_sa_token", "shared_op_sa_token")
REFRESH_VALUES = ("gog-token",)


CHECK_PREFIX = "check: "


def verification_purpose(why: str = "") -> str:
    """The X-Canopy-Read-Purpose of a person's (or an agent's) verification run — not the
    box's own refresh. canopy-web still raises its read-outside-a-turn alert for it (a
    header proves nothing; a stolen token could send it too), only worded as the labelled
    check it says it is, so an expected check reads as expected."""
    import getpass
    import socket

    try:
        who = f"{getpass.getuser()}@{socket.gethostname().split('.')[0]}"
    except Exception:  # noqa: BLE001
        who = "unknown"
    label = " ".join((why or "verification run").split())[:120]
    return f"{CHECK_PREFIX}{label} (by {who})"


#: The runner's tmpfs root for per-turn env files. Set → secrets stay off disk (see above).
TURN_ENV_ROOT_VAR = "CANOPY_TURN_ENV_ROOT"


def secrets_off_disk(environ=None) -> bool:
    """Whether this runner keeps decrypted secrets off persistent disk: opted in by
    ``$CANOPY_TURN_ENV_ROOT`` (or ``$CANOPY_NO_SECRET_FILES=1``)."""
    env = os.environ if environ is None else environ
    return bool((env.get(TURN_ENV_ROOT_VAR) or "").strip()
                or (env.get("CANOPY_NO_SECRET_FILES") or "").strip() in ("1", "true", "yes"))


def fetch_runner_credentials(slug: str, *, base_url: str, token: str,
                             transport: Optional[canopy_web.Transport] = None,
                             minimal: bool = False, purpose: str = "",
                             in_turn: bool = False) -> RunnerCredentials:
    """`/credentials/resolve` with the runner's token. Unreachable is not unregistered: it
    returns an `error` the report states, so a human retries instead of registering.

    `minimal`: the credentials-only pass — ask only for REFRESH_EXTRAS and those of
    REFRESH_VALUES the agent declares (an undeclared name would raise an owner alert).

    `purpose`: what the read says it is (X-Canopy-Read-Purpose). Default the box's own
    refresh; a person's verification run says `check: …` (see `verification_purpose`),
    which canopy-web still alerts on, labelled, rather than staying quiet."""
    # Where the agent's code lives — canopy-web's record (`/runtime`), never a convention: an
    # agent in another org or under another name would otherwise clone the wrong repo.
    try:
        rt = canopy_web.call("GET", f"/api/agents/{slug}/runtime", base_url=base_url,
                             token=token, transport=transport) or {}
        repo_url = str(rt.get("repo_url") or "").strip()
    except Exception:  # noqa: BLE001 — the report says "no repo" if it matters
        rt, repo_url = {}, ""
    refs = None
    if minimal:
        declared = set(rt.get("secret_refs") or [])
        refs = [n for n in REFRESH_VALUES if n in declared] + list(REFRESH_EXTRAS)
    try:
        from orchestrator import cred_resolve
        # A box staging an agent outside any turn (bootstrap, the runner-update timer)
        # says so, or canopy-web emails the owner "read outside a turn" for routine
        # maintenance every hour (canopy-web#1563).
        # A claim resolving the env FOR a turn (`in_turn`, the turn id rides
        # X-Canopy-Turn-Id) is the read the alert is about — it carries no refresh label.
        body = cred_resolve.resolve(slug, base_url=base_url, token=token,
                                    transport=transport, refs=refs, fallback=not minimal,
                                    purpose=purpose or ("" if in_turn else
                                                        cred_resolve.RUNNER_REFRESH)) or {}
    except Exception as e:  # noqa: BLE001
        return RunnerCredentials(error=f"could not reach canopy-web for {slug}'s credentials: "
                                       f"{_first_line(str(e), 200)}")
    try:
        acc = canopy_web.call("GET", f"/api/agents/{slug}/credentials/access",
                              base_url=base_url, token=token, transport=transport) or {}
        source = str(acc.get("credential_source") or "")
    except Exception:  # noqa: BLE001 — unknown backend = the default path (op inject)
        source = ""
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    extras = {k: body[k] for k in RESOLVE_EXTRAS if body.get(k) not in (None, "")}
    return RunnerCredentials(
        credential_source=source, values=values, extras=extras,
        repo_url=repo_url,
        vault=str(body.get("op_vault") or ""), key=str(body.get("op_sa_token") or ""),
        shared_vault=str(body.get("shared_op_vault") or ""),
        shared_key=str(body.get("shared_op_sa_token") or ""),
        github_token=str(body.get("github_token") or ""), mailbox=str(body.get("mailbox") or ""),
        mailbox_backend=str(body.get("mailbox_backend") or ""),
        google_account=str(body.get("google_account") or "").strip(),
        web_gog_token=((body.get("values") or {}).get("gog-token")) or None)


#: Where a runner hands `canopy agent prepare --runner` the GitHub token of the turn it
#: prepares the agent in (canopy-web's runner/ec2/cloud_runner.py::prepare_agent).
TURN_GITHUB_TOKEN_ENV = "CANOPY_TURN_GITHUB_TOKEN"


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


def _remove_tree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


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


def _setup_failure(stdout: str, stderr: str, limit: int = 300) -> str:
    """The line that says WHY an installer failed — not its first line, which is an INFO
    banner (ACE's setup opens with `INFO plugin_root=…`, and that is all the report said
    on every failed pass). The first FAIL / ERROR line, plus the next line when it ends
    in a colon (the cause follows it); else the last non-empty line."""
    lines = [ln.rstrip() for ln in f"{stdout or ''}\n{stderr or ''}".splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith(("FAIL", "ERROR", "Error:", "error:", "fatal:")):
            out = ln.strip()
            if out.endswith(":") and i + 1 < len(lines):
                out = f"{out} {lines[i + 1].strip()}"
            return out[:limit]
    return (lines[-1].strip() if lines else "(no output)")[:limit]


class RunnerPreparer(Bootstrapper):
    """The runner mode. Same steps as a person's bootstrap; different keys, plus what only a
    machine that RUNS turns needs (the clone, directory plugins, the provisioner, a report)."""

    def __init__(self, *, base_url: str, token: str, agent_root: Path,
                 plugin_deps_root: Optional[Path] = None,
                 transport: Optional[canopy_web.Transport] = None,
                 creds_for: Optional[Callable[[str], RunnerCredentials]] = None,
                 runner_name: str = "", purpose: str = "",
                 no_secret_files: Optional[bool] = None, in_turn: bool = False, **kw):
        self.base_url, self.token, self.transport = base_url, token, transport
        #: Secrets off disk (module doc): resolve and check, write no value to a file.
        self.no_secret_files = secrets_off_disk() if no_secret_files is None else no_secret_files
        self.agent_root = Path(agent_root)
        # The agent's GitHub token for THIS turn, when a runner prepares an agent inside
        # one (canopy-web returns it from credentials/resolve no longer: it reaches a box
        # only per turn). Taken out of the environment so no op/setup child inherits it;
        # only `_git` uses it.
        self._turn_github_token = os.environ.pop(TURN_GITHUB_TOKEN_ENV, "").strip()
        self.plugin_deps_root = Path(plugin_deps_root or
                                     Path.home() / ".claude" / "plugins" / "agent-deps")
        self.runner_name = runner_name or os.uname().nodename
        self._minimal = False   # set by prepare(credentials_only=True)
        self._creds_for = creds_for or (lambda s: fetch_runner_credentials(
            s, base_url=base_url, token=token, transport=transport, minimal=self._minimal,
            purpose=purpose, in_turn=in_turn))
        self._creds: dict[str, RunnerCredentials] = {}
        self._values: dict[str, dict] = {}   # off-disk: this pass's resolved env, in memory
        self._gog_env: dict = {}              # this agent's gog home + password, if any
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

    def client_from_vault(self, slug: str, client: str):
        """From this box's own resolve: a workspace vault layered under the agent's values."""
        item = SHARED_CLIENT_ITEMS.get(client)
        value = (self.creds(slug).values or {}).get(item) if item else None
        return str(value) if value else None

    def client_ref(self, client: str, vault: str) -> str:
        """A shared client from the WORKSPACE's shared vault (read with its own key), any
        other from the agent's vault. Measured 2026-09-07: one pass read Agent-Ace fine and
        failed Canopy-Shared — a per-agent key cannot read the shared vault, by design."""
        item = SHARED_CLIENT_ITEMS.get(client)
        if item:
            return f"op://{self.creds(self._slug).shared_vault or '<no shared vault>'}/{item}/credential"
        return f"op://{vault}/gog-oauth-client/credential"

    def expected_mailbox(self, slug: str, repo: Path) -> str:
        """The Google account THIS instance's token must be for — canopy-web's
        `google_account` when it records one (an SES-primary agent keeps Drive/Docs under
        a separate Google account), else its mailbox (`Agent.email`). Never the repo's or
        the slug's: instances share a repo, so either would point two at one inbox (#984)."""
        c = self.creds(slug)
        return (c.google_account or c.mailbox).lower()

    # ── runner-only steps ────────────────────────────────────────────────────
    def _git(self, args: list[str], github_token: str, timeout: int = 300):
        env = {**os.environ, "GH_TOKEN": github_token or self._turn_github_token}
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
        else:
            # No manifest = the agent's own plugin is never installed, so its turns run
            # without its skills. That used to pass as "0 ok"; it is a failure the box's
            # report must show (the factory stamps this file since agent-factory 1.2.0).
            problems.append(f"{slug}: no .claude-plugin/marketplace.json in {repo} — the "
                            f"agent's own plugin cannot be installed; add one (name "
                            f"'{slug}', plugins[0] source './')")
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
        c = self.creds(slug)
        base = {**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir), "AGENT_SLUG": slug}
        # What this pass already resolved, handed over so the installer need not resolve
        # it again: the vault, and the .env the env step just filled from it. An agent's
        # setup re-resolving them goes through `canopy agent env`, which needs a PERSON's
        # canopy-web token — a runner has none, so ACE's setup died there on every pass
        # (cloud-ec2-2, 2026-10-18). Names only; an installer that ignores them is unchanged.
        if c.vault:
            base["AGENT_VAULT"] = c.vault
        transient = None
        if self.no_secret_files:
            # Off disk: the values this pass resolved in memory, through a 0600 file under
            # the runner's tmpfs root that is gone as soon as the installer exits.
            if rep.env_ok and self._values.get(slug):
                transient = self._transient_env(slug, self._values[slug])
                base["AGENT_ENV_FILE"] = base["CANOPY_AGENT_ENV_FILE"] = str(transient)
        else:
            env_file = self.home / f".{slug}" / ".env"
            if rep.env_ok and env_file.is_file():
                base["AGENT_ENV_FILE"] = str(env_file)
        env = self.op_env(c.vault, base)
        try:
            r = self.run(["bash", str(setup), "--skip-doctor"], capture_output=True, text=True,
                         timeout=900, env=env, cwd=str(repo))
            if r.returncode != 0:
                rep.notes.append(f"provisioner: bin/{slug}-setup failed: "
                                 f"{_setup_failure(r.stdout, r.stderr)}")
        except (OSError, subprocess.TimeoutExpired) as e:
            rep.notes.append(f"provisioner: bin/{slug}-setup: {e}")
        finally:
            if transient is not None:
                _remove_tree(transient.parent)
        if self.no_secret_files:
            return   # no `.gws-sa-key.json` / `.env` copy into the clone
        for name, dest in (("gws-sa-key.json", repo / ".gws-sa-key.json"),
                           (".env", repo / ".env")):
            src = data_dir / name
            if src.is_file() and (name != ".env" or not dest.exists()):
                _write_private(dest, src.read_text(encoding="utf-8"))

    def _transient_env(self, slug: str, values: dict) -> Path:
        """A 0700 dir + 0600 env file under the runner's tmpfs root (else the system temp
        dir) holding ``values``; the caller removes the directory."""
        import tempfile

        from orchestrator.cred_cli import env_values, mkdirs_private
        root = (os.environ.get(TURN_ENV_ROOT_VAR) or "").strip() or None
        if root:
            mkdirs_private(Path(root))
        d = Path(tempfile.mkdtemp(prefix=f"prepare-{slug}-", dir=root))
        os.chmod(d, 0o700)
        usable, _ = env_values(values)
        f = d / "env"
        _write_private(f, "".join(f"{k}={v}\n" for k, v in usable.items()))
        os.chmod(f, 0o600)
        return f

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

    # ── per-agent gog home (runner isolation units §6) ───────────────────────
    def _gog(self, argv: list[str], **kw):
        """gog with THIS agent's own home and keyring password when the box names a unit
        gog secret (`orchestrator.gog_home`); else exactly the person's/box's gog."""
        if self._gog_env:
            kw["env"] = {**{k: v for k, v in os.environ.items() if k not in self._gog_env},
                         **self._gog_env}
            return self.run(argv, **kw)
        return super()._gog(argv, **kw)

    # ── orchestration ────────────────────────────────────────────────────────
    def prepare(self, slug: str, *, repo_url: str = "", credentials_only: bool = False,
                env_only: bool = False, report: bool = True) -> RunnerReport:
        """`_prepare` for one agent, inside that agent's own gog home and keyring when the
        box names a unit gog secret (`$CANOPY_GOG_SECRET_FILE`). The pair is set on this
        process for the call (the mailbox check's gog inherits it) and restored after."""
        from orchestrator.agent_email import _default_gog_config_dir
        from orchestrator.gog_home import GogHomeError, agent_gog_env

        try:
            own = agent_gog_env(slug, strict=True)
        except GogHomeError as e:
            rep = RunnerReport(slug=slug)
            rep.fail(f"gog: {e}")
            if report:
                self.report(slug, rep)
            return rep
        if not own:
            return self._prepare(slug, repo_url=repo_url, credentials_only=credentials_only,
                                 env_only=env_only, report=report)
        saved = {k: os.environ.get(k) for k in own}
        saved_dir, saved_accounts = self.gog_dir, (self._accounts, self._accounts_read)
        Path(own["GOG_HOME"]).mkdir(parents=True, exist_ok=True, mode=0o700)
        os.environ.update(own)
        self._gog_env = own
        self.gog_dir = Path(_default_gog_config_dir())
        self.gog_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._accounts, self._accounts_read = None, False   # another keyring: re-list
        try:
            return self._prepare(slug, repo_url=repo_url, credentials_only=credentials_only,
                                 env_only=env_only, report=report)
        finally:
            self._gog_env = {}
            self.gog_dir = saved_dir
            self._accounts, self._accounts_read = saved_accounts
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def _prepare(self, slug: str, *, repo_url: str = "", credentials_only: bool = False,
                 env_only: bool = False, report: bool = True) -> RunnerReport:
        self._slug = slug
        # The credentials-only pass stages the gog token and reports readiness, nothing
        # else, so it asks canopy-web for only what that uses (REFRESH_EXTRAS).
        self._minimal = credentials_only and not env_only
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
        if c.mailbox and c.mailbox_backend == "ses":
            self._hosted_mailbox(slug, repo, c, rep)
        elif c.mailbox:
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

    def _hosted_mailbox(self, slug: str, repo: Path, c: RunnerCredentials, rep: RunnerReport) -> None:
        """A canopy-hosted address: mail in is the agent's turn and `canopy email` sends
        through canopy-web, so there is no Gmail token to check and nothing is broken
        without one. A Google sign-in (Drive, Docs) is optional: staged when one exists,
        never a failure when not. It is for the agent's Google account — canopy-web's
        `google_account` (e.g. <slug>@dimagi-ai.com), else the same address."""
        rep.gmail = "canopy-hosted"
        rep.turn = "OK (canopy-hosted)"
        rep.mailbox_ok = True
        rep.turn_ready = True
        rep.notes.append(f"ok: {c.mailbox} is canopy-hosted email — no Gmail sign-in needed")
        if not (c.web_gog_token or c.vault):
            return
        side = RunnerReport(slug=slug)
        try:
            tok = self.step_token(slug, repo, side)
        except Exception as e:  # noqa: BLE001 — optional: must never sink a hosted agent
            tok, side.ok = None, False
            side.notes.append(_first_line(str(e)))
        if tok is not None and side.ok:
            rep.token, rep.gog_client = side.token, tok.client
            rep.client_creds_ok = (self.gog_dir / f"credentials-{tok.client}.json").is_file()
            rep.notes.append("ok: Google sign-in staged (Drive/Docs)")
        else:
            rep.notes.append("google sign-in (optional, for Drive/Docs) not staged: "
                             + "; ".join(n for n in side.notes if not n.startswith("ok: "))[:200])

    # ── secrets off disk: resolve + check in memory ─────────────────────────────
    def env_values(self, slug: str, repo: Path) -> tuple[dict, str]:
        """({NAME: value}, which backend) for the agent's env, resolved IN MEMORY: canopy-web's
        values, or `op inject` of the clone's .env.tpl to a pipe with the agent's own key.
        Raises RuntimeError with the reason (never a value)."""
        from orchestrator.agent_instance import inject_env, uses_vault_var
        from orchestrator.cred_cli import env_values, parse_dotenv

        c = self.creds(slug)
        if c.error:
            raise RuntimeError(c.error)
        if c.credential_source == "canopy-web":
            usable, _ = env_values(c.values)
            return usable, "canopy-web"
        tpl = Path(repo) / ".env.tpl"
        if not tpl.is_file():
            raise RuntimeError(f"no .env.tpl in {repo}")
        if not (c.vault and c.key):
            raise RuntimeError(f"canopy-web has {'no vault' if not c.vault else 'no key'} for "
                               f"{slug} — `canopy agent vault --slug {slug}`")
        base = dict(os.environ)
        if uses_vault_var(tpl.read_text(encoding="utf-8")):
            base = inject_env(base, c.vault, slug)
        r, err = self._op(["op", "inject", "-i", str(tpl)], timeout=180,
                          env=self.op_env(c.vault, base))
        if r is None or r.returncode != 0:
            raise RuntimeError(f"op inject failed: {err or _first_line(r.stderr, 300)}")
        return parse_dotenv(r.stdout or ""), "1Password"

    def _env_in_memory(self, slug: str, repo: Path, rep: RunnerReport) -> None:
        """Step 2 with secrets off disk: resolve and check, keep the values in this process
        only (for the provisioner's transient file), write nothing."""
        if self.dry_run:
            rep.env, rep.env_ok = "would resolve (in memory)", None
            return
        if not (repo and (Path(repo) / ".env.tpl").is_file()) and \
                self.creds(slug).credential_source != "canopy-web":
            rep.env, rep.env_ok = "n/a", None
            return
        try:
            values, how = self.env_values(slug, repo)
        except RuntimeError as e:
            rep.env, rep.env_ok = "FAIL", False
            rep.fail(f"env: {e}")
            return
        self._values[slug] = values
        rep.env, rep.env_ok = f"OK ({len(values)} vars from {how}, not written)", True

    def _env_sync(self, slug: str, repo: Path, rep: RunnerReport) -> None:
        """The claim-time refresh: canopy's `cred sync` (the function every agent session's
        start hook runs) — re-inject only when the vault or template changed, keep the last
        good file on failure. ONE implementation; cloud_runner used to carry its own."""
        from orchestrator.cred_cli import CredError, sync_env

        if self.no_secret_files:
            self._env_in_memory(slug, repo, rep)
            return
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
        if self.no_secret_files:
            self._env_in_memory(slug, repo, rep)
            return
        c = self.creds(slug)
        if c.credential_source == "canopy-web" and not c.error:
            self._env_from_web(slug, c, rep)
            return
        try:
            self.step_env(slug, repo, rep)
        except Exception as e:  # noqa: BLE001
            rep.fail(f"env: unexpected error: {_first_line(str(e))}")
        rep.env_ok = None if rep.env in ("n/a", "-", "would inject") else rep.env.startswith("OK")
