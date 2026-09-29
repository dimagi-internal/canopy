"""`canopy agent bootstrap` — make this MACHINE ready to run the fleet's agents.

The laptop counterpart of canopy-web's `runner/ec2/bootstrap_agents.sh` (the cloud
runner's bootstrap). Same rules, macOS paths, and the interactive 1Password account a
laptop has instead of per-agent service-account keys. Per agent:

  1. plugins  — the agent's own plugin (`<slug>@<slug>`, marketplace from its repo) plus its
                config/agent.json `required_plugins`, parsed by the SAME function
                `canopy agent doctor`'s check_required_plugins uses. Skipped when the
                registry already has them.
  2. env      — `op inject -f -i <repo>/.env.tpl -o ~/.<slug>/.env`, chmod 600.
  3. token    — the gog Gmail token, NEWEST WINS by the token's own `created_at`, between
                canopy-web's `/credentials/resolve` copy and op://Agent-<Slug>/gog-token.
  4. client   — the OAuth client the TOKEN names (never config/agent.json — that inference
                broke echo's working mailbox on 2026-09-05, canopy-web#661), materialized to
                <gog dir>/credentials-<client>.json.
  5. import   — `gog auth tokens import`, skipped when gog already holds that exact token
                (same client + created_at); then account_clients[<email>] = <client> merged
                into gog's config.json.
  6. verify   — ONE real call (agent_email.preflight), then the TURN-CLIENT check: the client
                an agent's turns ask for (config/agent.json gog_client) against the live
                token's client. A mismatch is a mailbox that verifies green while every email
                turn fails (ACE, 2026-09-08). Reported loudly, never "fixed" by remapping.

Secrets hygiene: no secret is printed, passed in argv, or left on disk — tokens travel
in-process and through 0600 temp files that are deleted. The macOS Keychain prompts on
gog's first access to each item, so gog is invoked as little as possible: one
`gog auth list` for the whole run, an import only when the token actually changed, and one
gmail call per agent.
"""
from __future__ import annotations

import calendar
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from orchestrator import canopy_web
from orchestrator.agent_doctor import (
    PLUGIN_REGISTRY,
    _plugin_source,
    declared_required_plugins,
    installed_plugin_names,
)
from orchestrator.agent_email import (
    GOG_CONFIG_DIR,
    AgentEmailError,
    EmailIdentity,
    gog_client_credentials,
    preflight,
    resolve_email_identity,
)

OP_ACCOUNT = "dimagi.1password.com"
KNOWN_MARKETPLACES = "~/.claude/plugins/known_marketplaces.json"

#: Shared fleet OAuth clients live in the tenant's shared vault; any other client is an
#: agent's own and lives in that agent's vault. Keyed on the client the TOKEN names.
SHARED_CLIENT_REFS = {
    "canopy": "op://Canopy-Shared/gog-oauth-client/credential",
    "canopy-web": "op://Canopy-Shared/gog-oauth-client-web/credential",
}

#: Deliberately NO fallback vault. An operator who cannot read Canopy-Shared is granted
#: membership in it — the fix is access, not a second copy. AI-Agents is a humans-only
#: vault now that every agent has its own Agent-<Slug> vault plus Canopy-Shared (Jonathan,
#: 2026-09-29, reverting #707's AI-Agents fallback: "Don't have anything fall back to
#: ai-agents").
SHARED_VAULT_HINT = ("you are not a member of the Canopy-Shared vault — ask a vault owner "
                     "(Jonathan, or Hal) to add you, then re-run")

#: stderr fragments meaning "1Password is locked / not signed in", not "item missing".
OP_AUTH_FAILURES = ("authorization timeout", "not currently signed in", "authorization prompt",
                    "session expired", "no accounts configured", "connect to 1password",
                    "prompterror")

KEYCHAIN_HEADS_UP = (
    "Heads-up: gog reads its tokens from the macOS login Keychain — if a dialog asks to "
    "allow `gog`, click \"Always Allow\" (once per item)."
)

Runner = Callable[..., subprocess.CompletedProcess]


# ──────────────────────────────────────────────────────────────────────────────
# pure decision logic
# ──────────────────────────────────────────────────────────────────────────────

def agent_vault(slug: str) -> str:
    return f"Agent-{slug[:1].upper()}{slug[1:]}"


def client_op_ref(client: str, slug: str) -> str:
    """The 1Password ref holding <client>'s OAuth id+secret. Decided by the CLIENT (which
    the token names), not by the agent: a shared client is read from the shared vault no
    matter which agent's token needs it."""
    return SHARED_CLIENT_REFS.get(client) or f"op://{agent_vault(slug)}/gog-oauth-client/credential"


def parse_created_at(value) -> int:
    """Epoch seconds for a gog `created_at` ("2026-09-07T14:04:13Z"), 0 when absent/odd."""
    v = str(value or "").strip()
    if not v:
        return 0
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return int(calendar.timegm(time.strptime(v, fmt)))
        except ValueError:
            continue
    return 0


@dataclass
class Token:
    source: str          # "canopy-web" | "vault"
    raw: str = field(repr=False)   # the secret — never printed
    client: str
    email: str
    created_at: str
    epoch: int


def parse_token(source: str, raw: str | None) -> Token | None:
    """A gog token JSON → Token, or None when absent/unparseable."""
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return Token(source=source, raw=raw, client=str(data.get("client") or "").strip(),
                 email=str(data.get("email") or "").strip().lower(),
                 created_at=str(data.get("created_at") or ""),
                 epoch=parse_created_at(data.get("created_at")))


def pick_newest(candidates: list[Token | None]) -> Token | None:
    """NEWEST WINS by the token's own created_at. On a tie (including both undated) the
    EARLIER candidate wins — callers pass the vault first, so an undecidable comparison
    degrades toward the vault, same as the cloud bootstrap (never import canopy-web's copy
    over a good vault rotation on a guess)."""
    best: Token | None = None
    for c in candidates:
        if c is None:
            continue
        if best is None or c.epoch > best.epoch:
            best = c
    return best


def token_already_present(tok: Token, accounts: list[dict] | None) -> bool:
    """gog already holds this exact token — same mailbox, same client, same created_at —
    per `gog auth list --json`. Re-importing it would only cost Keychain prompts."""
    if not accounts or not tok.epoch:
        return False
    for a in accounts:
        if (str(a.get("email") or "").lower() == tok.email
                and (a.get("client") or "") == tok.client
                and parse_created_at(a.get("created_at")) == tok.epoch):
            return True
    return False


def gog_holds_newer(tok: Token, accounts: list[dict] | None) -> str:
    """created_at of a token gog ALREADY holds for this mailbox+client that is newer than
    `tok`, else "". Importing over it would downgrade a working credential — which is what
    happens when one source was unreadable and the older copy "won" by default."""
    for a in accounts or []:
        if (str(a.get("email") or "").lower() == tok.email
                and (a.get("client") or "") == tok.client
                and parse_created_at(a.get("created_at")) > tok.epoch):
            return str(a.get("created_at"))
    return ""


def age_label(epoch: int, now: float | None = None) -> str:
    if not epoch:
        return "undated"
    days = int(((now if now is not None else time.time()) - epoch) // 86400)
    return f"{days}d" if days >= 1 else "<1d"


@dataclass
class PluginAction:
    plugin: str            # name@marketplace_name
    marketplace: str       # owner/repo to add ("" = cannot install)
    marketplace_name: str
    add_marketplace: bool
    note: str = ""


def plugin_plan(repo: Path, installed: set[str] | None, known_marketplaces: set[str]
                ) -> tuple[list[PluginAction], list[str], int]:
    """(actions, problems, n_ok) for the agent's own plugin + its required_plugins.

    "Installed" means the plugin NAME is in the registry, marketplace suffix ignored —
    exactly `canopy agent doctor`'s test, so bootstrap never skips what doctor would fail
    nor installs what doctor would pass.
    """
    installed = installed or set()
    wanted: list[tuple[str, str, str, str]] = []   # (name, source, marketplace_name, note)
    problems: list[str] = []

    manifest = Path(repo) / ".claude-plugin" / "plugin.json"
    if manifest.is_file():
        try:
            own = (json.loads(manifest.read_text(encoding="utf-8")).get("name") or "").strip()
        except (ValueError, OSError):
            own = ""
        if own:
            mname = own
            mp = Path(repo) / ".claude-plugin" / "marketplace.json"
            try:
                mname = (json.loads(mp.read_text(encoding="utf-8")).get("name") or "").strip() or own
            except (ValueError, OSError):
                pass
            wanted.append((own, _plugin_source(Path(repo)), mname, ""))
        else:
            problems.append(f"{manifest} has no name")

    specs, bad = declared_required_plugins(Path(repo))
    for b in bad:
        problems.append(f"required_plugins entry with no name: {b!r}")
    for s in specs:
        wanted.append((s.name, s.marketplace, s.marketplace_name, s.note))

    actions: list[PluginAction] = []
    n_ok = 0
    for name, source, mname, note in wanted:
        if name in installed:
            n_ok += 1
            continue
        if not source:
            problems.append(f"{name}: declares no marketplace (owner/repo) — cannot install")
            continue
        actions.append(PluginAction(plugin=f"{name}@{mname}", marketplace=source,
                                    marketplace_name=mname,
                                    add_marketplace=mname not in known_marketplaces, note=note))
    return actions, problems, n_ok


def merge_account_client(config: dict, email: str, client: str) -> tuple[dict, bool]:
    """account_clients[email] = client, every other key untouched. (new, changed)."""
    new = dict(config or {})
    ac = dict(new.get("account_clients") or {})
    changed = ac.get(email) != client
    ac[email] = client
    new["account_clients"] = ac
    return new, changed


# ──────────────────────────────────────────────────────────────────────────────
# side-effecting steps
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AgentReport:
    slug: str
    plugins: str = "-"
    env: str = "-"
    token: str = "-"
    gmail: str = "-"
    turn: str = "-"
    notes: list[str] = field(default_factory=list)
    ok: bool = True

    def fail(self, note: str) -> None:
        self.ok = False
        self.notes.append(note)


def _first_line(text: str, limit: int = 200) -> str:
    line = (text or "").strip().splitlines()[:1]
    return (line[0] if line else "(no output)")[:limit]


def _write_private(path: Path, data: str) -> None:
    """Write `data` to `path` via a 0600 temp file in the same dir + atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _operator_token() -> str:
    """The OPERATOR's canopy-web PAT. Bootstrapping is the human provisioning their machine,
    and `/credentials/resolve` authorizes the operator — so an agent PAT picked up from the
    cwd (canopy_web.resolve_token's second tier) is the wrong identity here. Explicit env
    still wins; otherwise the workbench-token; otherwise whatever resolve_token finds."""
    env = os.environ.get("CANOPY_WEB_PAT", "").strip()
    if env:
        return env
    try:
        stored = canopy_web.TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        stored = ""
    return stored or canopy_web.resolve_token(None)


def fetch_web_token(slug: str) -> tuple[str | None, str]:
    """(gog-token JSON or None, error). canopy-web having none is the normal case."""
    try:
        body = canopy_web.call("GET", f"/api/agents/{slug}/credentials/resolve",
                               token=_operator_token())
    except Exception as e:  # noqa: BLE001 — any transport/auth failure is just "no copy"
        return None, _first_line(str(e), 160)
    return ((body or {}).get("values") or {}).get("gog-token"), ""


class Bootstrapper:
    def __init__(self, *, dry_run: bool = False, op_account: str = OP_ACCOUNT,
                 runner: Runner = subprocess.run,
                 web_token: Callable[[str], tuple[str | None, str]] = fetch_web_token,
                 gog_dir: str | None = None, registry_path: str | None = None,
                 marketplaces_path: str | None = None, home: Path | None = None,
                 verify: Callable = preflight, echo: Callable[[str], None] = print,
                 platform: str = sys.platform):
        self.dry_run = dry_run
        self.op_account = op_account
        self.run = runner
        self.web_token = web_token
        self.gog_dir = Path(gog_dir or GOG_CONFIG_DIR)
        self.registry_path = registry_path or PLUGIN_REGISTRY
        self.marketplaces_path = Path(marketplaces_path or KNOWN_MARKETPLACES).expanduser()
        self.home = home or Path.home()
        self.verify = verify
        self.echo = echo
        self.platform = platform
        self._accounts: list[dict] | None = None
        self._accounts_read = False
        self._warned_keychain = False
        self._op_blocked = ""

    # ── helpers ───────────────────────────────────────────────────────────────
    def _keychain_heads_up(self) -> None:
        if self.platform == "darwin" and not self._warned_keychain:
            self._warned_keychain = True
            self.echo(KEYCHAIN_HEADS_UP)

    def _op(self, argv: list[str], timeout: int) -> tuple[subprocess.CompletedProcess | None, str]:
        """Run `op …`, short-circuiting once 1Password has refused to authorize.

        A locked 1Password makes every `op` call wait out its own ~60s authorization timeout;
        without this, five agents × three reads is a quarter of an hour of silence."""
        if self._op_blocked:
            return None, self._op_blocked
        try:
            r = self.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            self._op_blocked = "op CLI not installed"
            return None, self._op_blocked
        except subprocess.TimeoutExpired:
            self._op_blocked = f"op timed out after {timeout}s — is 1Password unlocked?"
            return None, self._op_blocked
        err = (r.stderr or "").lower()
        if r.returncode != 0 and any(m in err for m in OP_AUTH_FAILURES):
            # The reason only — op's full line names the FIRST ref tried, which would read as
            # the wrong agent's secret in every later agent's report.
            reason = _first_line(r.stderr).rsplit(": ", 1)[-1]
            self._op_blocked = (f"1Password did not authorize `op` ({reason}) — "
                                "unlock the 1Password app / approve its prompt, then re-run")
            return None, self._op_blocked
        return r, ""

    def _op_read(self, ref: str) -> tuple[str | None, str]:
        r, err = self._op(["op", "read", ref, "--account", self.op_account], timeout=120)
        if r is None:
            return None, err
        if r.returncode != 0 or not (r.stdout or "").strip():
            return None, _first_line(r.stderr)
        return r.stdout, ""

    def gog_accounts(self) -> list[dict] | None:
        """`gog auth list --json`, ONCE per run (every gog call can be a Keychain prompt)."""
        if not self._accounts_read:
            self._accounts_read = True
            self._keychain_heads_up()
            try:
                r = self.run(["gog", "auth", "list", "--json"],
                             capture_output=True, text=True, timeout=60)
                if r.returncode == 0:
                    self._accounts = json.loads(r.stdout or "{}").get("accounts") or []
            except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
                self._accounts = None
        return self._accounts

    def _known_marketplaces(self) -> set[str]:
        try:
            return set(json.loads(self.marketplaces_path.read_text(encoding="utf-8")))
        except (ValueError, OSError, TypeError):
            return set()

    # ── steps ─────────────────────────────────────────────────────────────────
    def step_plugins(self, repo: Path, rep: AgentReport) -> None:
        actions, problems, n_ok = plugin_plan(
            repo, installed_plugin_names(self.registry_path), self._known_marketplaces())
        for p in problems:
            rep.fail(f"plugins: {p}")
        installed_now = failed = 0
        for a in actions:
            cmds = []
            if a.add_marketplace:
                cmds.append(["claude", "plugin", "marketplace", "add", a.marketplace])
            cmds.append(["claude", "plugin", "install", a.plugin])
            if self.dry_run:
                rep.notes.append("plugins: would run " + " && ".join(" ".join(c) for c in cmds))
                continue
            for c in cmds:
                try:
                    r = self.run(c, capture_output=True, text=True, timeout=600)
                    err = "" if r.returncode == 0 else _first_line(r.stderr or r.stdout)
                except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                    err = str(e)
                if err:
                    failed += 1
                    rep.fail(f"plugins: `{' '.join(c)}` failed: {err}")
                    break
            else:
                installed_now += 1
                if a.note:
                    rep.notes.append(f"plugins: follow-up for {a.plugin}: {a.note}")
        total = n_ok + len(actions) + len(problems)
        rep.plugins = f"{n_ok + installed_now}/{total}"
        if self.dry_run and actions:
            rep.plugins += f" (+{len(actions)} to install)"
        elif installed_now:
            rep.plugins += f" (+{installed_now} new)"
        rep.plugins += " FAIL" if (problems or failed) else ("" if self.dry_run and actions else " OK")

    def step_env(self, slug: str, repo: Path, rep: AgentReport) -> None:
        tpl = Path(repo) / ".env.tpl"
        if not tpl.is_file():
            rep.env = "n/a"
            return
        out = self.home / f".{slug}" / ".env"
        if self.dry_run:
            rep.env = "would inject"
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        r, err = self._op(["op", "inject", "-f", "-i", str(tpl), "-o", str(out),
                           "--account", self.op_account], timeout=180)
        if r is None or r.returncode != 0:
            rep.env = "FAIL"
            # op names the reference it could not resolve, never a value.
            rep.fail(f"env: op inject failed: {err or _first_line(r.stderr, 300)}")
            return
        os.chmod(out, 0o600)
        n = sum(1 for line in out.read_text(encoding="utf-8").splitlines()
                if "=" in line and not line.lstrip().startswith("#"))
        rep.env = f"OK ({n} vars)"

    def step_token(self, slug: str, repo: Path, rep: AgentReport) -> Token | None:
        """Pick the newest token, materialize its client, import it, map the account."""
        vault_raw, vault_err = self._op_read(f"op://{agent_vault(slug)}/gog-token/credential")
        web_raw, web_err = self.web_token(slug)
        # Vault FIRST: a tie degrades toward the vault (see pick_newest).
        tok = pick_newest([parse_token("vault", vault_raw), parse_token("canopy-web", web_raw)])
        if tok is None:
            rep.token = "NONE"
            rep.fail(f"token: no gog token in op://{agent_vault(slug)}/gog-token "
                     f"({vault_err or 'unparseable'}) nor canopy-web "
                     f"({web_err or 'none stored'})")
            return None
        rep.token = f"{tok.source} {tok.client or '?'} {age_label(tok.epoch)}"
        if vault_raw is None and self._op_blocked:
            rep.notes.append("token: vault copy not consulted (1Password locked) — "
                             "newest-wins decided on canopy-web's copy alone")
        if not tok.client:
            rep.fail("token: the token names no `client` — refusing to guess one "
                     "(the client is a property of the token, never of agent.json)")
            return None
        try:
            expected = resolve_email_identity(Path(repo)).account.lower()
        except AgentEmailError:
            expected = ""
        if expected and tok.email and tok.email != expected:
            rep.fail(f"token: the {tok.source} token is for {tok.email}, not {expected} — "
                     "NOT importing (one mailbox per agent)")
            return None
        email = tok.email or expected
        if not email:
            rep.fail("token: no mailbox — neither the token nor config/agent.json names one")
            return None

        # Client creds for the client the TOKEN names.
        creds = self.gog_dir / f"credentials-{tok.client}.json"
        if not gog_client_credentials(str(self.gog_dir), tok.client):
            ref = client_op_ref(tok.client, slug)
            if self.dry_run:
                rep.notes.append(f"token: would materialize credentials-{tok.client}.json "
                                 f"from {ref}")
            else:
                secret, err = self._op_read(ref)
                if secret is None:
                    hint = ("; " + SHARED_VAULT_HINT
                            if tok.client in SHARED_CLIENT_REFS and not self._op_blocked else "")
                    rep.fail(f"token: cannot read OAuth client '{tok.client}' from {ref}: "
                             f"{err}{hint}")
                    return tok
                _write_private(creds, secret)
                rep.notes.append(f"token: materialized credentials-{tok.client}.json")

        # Import — unless gog already holds this exact token, or a newer one.
        accounts = self.gog_accounts()
        if token_already_present(tok, accounts):
            rep.token += " (present)"
        elif newer := gog_holds_newer(tok, accounts):
            rep.token += " (kept newer)"
            rep.notes.append(f"token: gog already holds a newer {tok.client} token for "
                             f"{tok.email} ({newer}) — not downgrading it")
        elif self.dry_run:
            rep.token += " (would import)"
        else:
            self._keychain_heads_up()
            fd, tmp = tempfile.mkstemp(prefix="gog-token-", suffix=".json")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(tok.raw)
                r = self.run(["gog", "auth", "tokens", "import", tmp],
                             capture_output=True, text=True, timeout=60)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            if r.returncode != 0:
                rep.fail(f"token: gog auth tokens import failed: {_first_line(r.stderr)}")
                return tok
            rep.token += " (imported)"

        # account_clients[email] = the token's client, other keys untouched.
        cfg_path = self.gog_dir / "config.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                cfg = {}
        except (OSError, ValueError):
            cfg = {}
        new, changed = merge_account_client(cfg, email, tok.client)
        if changed:
            if self.dry_run:
                rep.notes.append(f"token: would map {email} -> {tok.client} in {cfg_path}")
            else:
                _write_private(cfg_path, json.dumps(new, indent=2) + "\n")
                rep.notes.append(f"token: mapped {email} -> {tok.client}")
        tok.email = email
        return tok

    def step_verify(self, repo: Path, tok: Token | None, rep: AgentReport) -> None:
        try:
            turn_client = resolve_email_identity(Path(repo)).client
        except AgentEmailError as e:
            rep.gmail = rep.turn = "?"
            rep.fail(f"verify: {e}")
            return
        if tok is None or not tok.client:
            rep.gmail = "skipped"
            rep.turn = f"? (turns ask {turn_client})"
            return
        if self.dry_run:
            rep.gmail = "would verify"
        else:
            self._keychain_heads_up()
            ident = EmailIdentity(slug=rep.slug, account=tok.email, client=tok.client,
                                  repo=Path(repo))
            ok, lines = self.verify(ident, gog_dir=str(self.gog_dir))
            rep.gmail = f"LIVE ({tok.client})" if ok else "DEAD"
            if not ok:
                rep.fail("gmail: " + " ".join(line.strip() for line in lines[:2]))
        if turn_client == tok.client:
            rep.turn = f"OK ({turn_client})"
        else:
            rep.turn = f"MISMATCH {turn_client}!={tok.client}"
            rep.fail(
                f"TURN-CLIENT MISMATCH: turns ask gog for client '{turn_client}' "
                f"(config/agent.json gog_client) but the live token is under "
                f"'{tok.client}' — the mailbox verifies while every email turn fails. "
                f"Mint a token under '{turn_client}' and store it as this agent's gog-token "
                f"(not fixed by remapping).")

    # ── orchestration ─────────────────────────────────────────────────────────
    def bootstrap_one(self, slug: str, repo: Path | None) -> AgentReport:
        rep = AgentReport(slug=slug)
        if repo is None or not (Path(repo) / ".claude-plugin" / "plugin.json").is_file():
            rep.fail(f"repo: no agent repo for {slug!r} — clone it "
                     f"(gh repo clone dimagi-internal/{slug} ~/emdash/repositories/{slug}) "
                     "or pass --repo")
            return rep
        for step in (lambda: self.step_plugins(repo, rep),
                     lambda: self.step_env(slug, repo, rep)):
            try:
                step()
            except Exception as e:  # noqa: BLE001 — one step must not sink the agent
                rep.fail(f"unexpected error: {_first_line(str(e))}")
        tok = None
        try:
            tok = self.step_token(slug, repo, rep)
        except Exception as e:  # noqa: BLE001
            rep.fail(f"token: unexpected error: {_first_line(str(e))}")
        try:
            self.step_verify(repo, tok, rep)
        except Exception as e:  # noqa: BLE001
            rep.fail(f"verify: unexpected error: {_first_line(str(e))}")
        return rep


def render_table(reports: list[AgentReport]) -> str:
    cols = ("agent", "plugins", "env", "token (src client age)", "gmail", "turn-client")
    rows = [(r.slug, r.plugins, r.env, r.token, r.gmail, r.turn) for r in reports]
    widths = [max(len(c), *(len(row[i]) for row in rows)) if rows else len(c)
              for i, c in enumerate(cols)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*cols), fmt.format(*("-" * w for w in widths))]
    lines += [fmt.format(*row) for row in rows]
    for r in reports:
        for n in r.notes:
            lines.append(f"  {r.slug}: {n}")
    return "\n".join(lines)
