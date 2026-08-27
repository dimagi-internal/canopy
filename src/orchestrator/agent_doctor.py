"""Per-AGENT health checks — `canopy agent doctor`.

`canopy doctor` diagnoses the canopy plugin install; THIS module diagnoses one
agent repo's operational readiness: identity config, gating rails, secrets
manifest, gog email auth, and canopy-web registration + board reachability.
Composes the point-checks that already exist (`resolve_email_identity`,
provision's manifest loaders, `agent_email.preflight`,
`AgentClient.pending_commands`) into one command, so "the agent was set up on
some machine once" stops diverging from "this machine can run the agent".

Born from hal (2026-07-02): hal's gog client existed somewhere, but this
machine had no credentials file, no secrets.yaml to provision it from, and no
canopy-web registration — and nothing in the framework would have said so
short of an actual failed turn. `canopy create-agent` documents these steps
for NEW agents; this doctor verifies them for any agent, any machine, any day.

A check that cries wolf is worse than no check: three of the five agents reported FAIL on a
healthy machine (2026-07-23), and every one was a false positive — gating counted only the
local `deny` array while the rails actually in force come from the fleet baseline mounted via
`channels`; hook wiring recognized only `.claude/settings.json` and not the plugin-style
`hooks/hooks.json` that ace uses; and auth-services demanded the fleet-wide LOGIN_SERVICES of
every agent, failing hal/ace over a scope they never call while missing that echo needs one
the constant omits. Each check must therefore model what actually runs at call time, and each
requirement must be the AGENT's, not the fleet's. This matters doubly because auto-heal is
built on top: healing a false positive damages a working agent.

Same shape as doctor.py: small read-only checks returning CheckResult,
injectable dependencies for tests, `run_agent_doctor` composes them. Unlike
the plugin doctor, two checks are intentionally LIVE (gog token liveness,
canopy-web reachability) — an agent doctor that can't see dead auth would
miss the exact failures it exists to catch.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from copy import copy
from pathlib import Path

from orchestrator.doctor import CheckResult
from orchestrator.agent_email import (
    GOG_CONFIG_DIR,
    AgentEmailError,
    EmailIdentity,
    clients_for_account,
    granted_services,
    preflight,
    reconcile_client,
    resolve_email_identity,
)
from orchestrator.agent_client import AgentClient, CanopyError
from orchestrator.provision import (
    ProvisionError,
    load_env_block,
    load_manifest,
    resolve_target,
)


_PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net"}

# The gog services EVERY agent's mailbox needs. Deliberately NOT `LOGIN_SERVICES` — that
# constant is the generous default for an interactive `gog login`, not a per-agent
# REQUIREMENT. Demanding it of everyone reported hal/ace as broken over `appscript`, which
# neither uses, while missing that echo genuinely needs `slides` (not in the constant at all).
# Agents extend this by declaring `gog_services` in config/agent.json.
CORE_SERVICES = ("gmail", "drive", "docs", "sheets", "forms")


def _baseline_rails(cfg: dict) -> list | None:
    """The FLEET-BASELINE deny rails this agent mounts via `channels`, mirroring what
    hooks/gating_guard.py merges in at call time.

    A repo whose config/gating.json has `"deny": []` but `"channels": ["email"]` is fully
    railed at runtime — the baseline rails (agent-core/gating-baseline.json, shipped with the
    canopy plugin) are merged IN FRONT of the local list. Counting only the local `deny`
    array reported echo and hal as unrailed when they were not.

    Returns [] for legacy configs (no `channels`), or None when channels are mounted but the
    baseline can't be resolved — the state in which gating_guard fails CLOSED.
    """
    channels = cfg.get("channels")
    if not channels:
        return []
    try:
        plugin_dir = os.environ.get("CANOPY_PLUGIN_DIR")
        if not plugin_dir:
            reg = json.loads(
                (Path("~/.claude/plugins/installed_plugins.json").expanduser()).read_text())
            plugin_dir = reg["plugins"]["canopy@canopy"][0]["installPath"]
        base = json.loads(
            (Path(plugin_dir) / "agent-core" / "gating-baseline.json").read_text())
    except Exception:
        return None
    rails: list = []
    for ch in channels:
        rails.extend(base.get("channels", {}).get(ch, []))
    return rails


def required_services(identity: EmailIdentity | None) -> tuple[set[str], str]:
    """(required services, where that came from) for an agent's gog login.

    Per-agent via `gog_services` in config/agent.json; otherwise CORE_SERVICES. Lets echo
    require `slides` and hal not require `appscript`, instead of one fleet-wide list that is
    simultaneously too strict for some agents and too loose for others.
    """
    repo = getattr(identity, "repo", None)
    if repo:
        try:
            data = json.loads((Path(repo) / "config" / "agent.json").read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        declared = data.get("gog_services")
        if isinstance(declared, str):
            declared = declared.split(",")
        if declared:
            svc = {s.strip() for s in declared if isinstance(s, str) and s.strip()}
            if svc:
                return svc, "config/agent.json gog_services"
    return set(CORE_SERVICES), "fleet core"


def _is_placeholder_mailbox(addr: str) -> bool:
    """The factory stamps `<slug>@example.com` when no real address is given — resolvable, but
    NOT a configured agent. Treat that (and an empty address) as not-ready."""
    a = (addr or "").strip().lower()
    return not a or a.rsplit("@", 1)[-1] in _PLACEHOLDER_DOMAINS


def check_identity(repo: Path) -> tuple[CheckResult, EmailIdentity | None]:
    """config/agent.json (+ plugin.json) must yield a full, NON-placeholder email identity."""
    name = "Identity"
    try:
        ident = resolve_email_identity(repo)
    except AgentEmailError as e:
        return CheckResult(name, False, str(e)), None
    detail = f"slug={ident.slug} mailbox={ident.account} gog_client={ident.client}"
    # A resolvable-but-placeholder mailbox passes the resolver but silently reads as "ready" — the
    # exact trap that let eva sit on `eva@example.com`. Flag it instead of rubber-stamping it.
    if _is_placeholder_mailbox(ident.account):
        return CheckResult(
            name, False,
            f"{detail} — mailbox is the factory PLACEHOLDER; set a real address in "
            'config/agent.json ("email") and mint/vault it before wiring email',
        ), ident
    return CheckResult(name, True, detail), ident


def check_gating(repo: Path) -> CheckResult:
    """config/gating.json must exist and parse — the agent's rails.

    An outbound-capable agent (it has an email shim) with ZERO deny rails is the exact
    unsafe state the rails exist to prevent, so that combination fails rather than
    passing on "the file parses".
    """
    name = "Gating rails"
    path = Path(repo) / "config" / "gating.json"
    if not path.exists():
        return CheckResult(name, False, f"{path} not found — the agent has no rails")
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return CheckResult(name, False, f"{path} unreadable: {e}")
    deny, approve = data.get("deny", []), data.get("approve", [])
    channels = data.get("channels") or []
    baseline = _baseline_rails(data)
    shims = list((Path(repo) / "bin").glob("*-email"))
    if baseline is None:
        return CheckResult(
            name, False,
            f"gating.json mounts channels {channels} but the fleet gating baseline "
            "(agent-core/gating-baseline.json) is unresolvable — gating_guard fails CLOSED, "
            "so every guarded tool call is blocked until the canopy plugin is installed or "
            "updated (/canopy:update)",
        )
    effective = len(baseline) + len(deny)
    if not effective and shims:
        return CheckResult(
            name, False,
            f"0 effective deny rails but {shims[0].name} exists — an outbound-capable agent "
            "needs at least the raw-send rail: mount it with \"channels\": [\"email\"] or add "
            "a local rail (see the factory's templated gating.json)",
        )
    return CheckResult(
        name, True,
        f"{effective} effective deny rail(s) — {len(baseline)} fleet-baseline via "
        f"channels={channels} + {len(deny)} local; {len(approve)} approve rule(s)",
    )


def check_hook_wiring(repo: Path) -> CheckResult:
    """The rails are only real if the PreToolUse hook is actually REGISTERED.

    config/gating.json without .claude/settings.json wiring hooks/gating_guard.py is
    decorative — the exact "set up somewhere, not on this repo" drift class this doctor
    exists to catch. Checks: guard file exists + settings.json references it under a
    PreToolUse matcher.
    """
    name = "Hook wiring"
    guard = Path(repo) / "hooks" / "gating_guard.py"
    if not guard.exists():
        return CheckResult(name, False, f"{guard} missing — rails have no enforcement")
    # TWO valid registration paths. Repo-style agents wire the guard in .claude/settings.json;
    # agents shipped AS a Claude Code plugin (ace) wire it in hooks/hooks.json, which the
    # harness loads from the plugin root. Checking only the former reported ace's rails as
    # decorative when its guard is registered and firing.
    candidates = (
        (Path(repo) / ".claude" / "settings.json", ".claude/settings.json"),
        (Path(repo) / "hooks" / "hooks.json", "hooks/hooks.json"),
    )
    unreadable = []
    for path, label in candidates:
        if not path.exists():
            continue
        try:
            settings = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            unreadable.append(f"{label} unreadable: {e}")
            continue
        pre = settings.get("hooks", {}).get("PreToolUse", [])
        if any("gating_guard.py" in (h.get("command") or "")
               for entry in pre for h in entry.get("hooks", [])):
            return CheckResult(name, True,
                               f"gating_guard.py registered as a PreToolUse hook via {label}")
    if unreadable:
        return CheckResult(name, False, "; ".join(unreadable))
    return CheckResult(
        name, False,
        "no PreToolUse hook invokes gating_guard.py — the rails in config/gating.json are "
        f"decorative until one does; wire it in {repo}/.claude/settings.json (repo-style) or "
        f"{repo}/hooks/hooks.json (plugin-style)",
    )


def check_secrets_manifest(repo: Path) -> CheckResult:
    """`.env.tpl` (PRIMARY — `op inject`) or `config/secrets.yaml` (LEGACY —
    `canopy provision`) must exist and be structurally sound, so this machine's agent state
    can be rebuilt. EITHER manifest satisfies the check — `.env.tpl` is preferred for new
    agents (see agent-core/agent-runtime.md); `config/secrets.yaml` still works for agents
    that haven't migrated. Structural only — op-ref resolution stays in a live `op inject` /
    `canopy provision --check` (needs a 1Password session)."""
    name = "Secrets manifest"
    repo = Path(repo)
    env_tpl = repo / ".env.tpl"
    secrets_yaml = repo / "config" / "secrets.yaml"

    if env_tpl.exists():
        var_lines = [
            l for l in env_tpl.read_text().splitlines()
            if l.strip() and not l.strip().startswith("#") and "=" in l
        ]
        op_refs = sum(1 for l in var_lines if "op://" in l)
        return CheckResult(
            name, True,
            f".env.tpl ({len(var_lines)} var(s), {op_refs} op:// ref(s)) — primary manifest; "
            "resolve with `op inject -i .env.tpl -o ~/.<agent>/.env`",
        )

    if secrets_yaml.exists():
        try:
            secrets = load_manifest(repo)
            env = load_env_block(repo)
        except ProvisionError as e:
            return CheckResult(name, False, str(e))
        n_env = len(env.vars) if env else 0
        return CheckResult(
            name, True,
            f"config/secrets.yaml ({len(secrets)} file secret(s), {n_env} env var(s)) — LEGACY "
            "manifest via `canopy provision`; new agents should scaffold .env.tpl + `op inject` "
            "instead — validate refs via `canopy provision --check`",
        )

    # Escape hatch for agents with their own bespoke provisioning — declared, not inferred, so
    # absence stays a failure by default.
    agent_json = repo / "config" / "agent.json"
    try:
        provisioning = json.loads(agent_json.read_text()).get("provisioning", "")
    except (OSError, ValueError):
        provisioning = ""
    if provisioning:
        return CheckResult(
            name, True,
            f"self-managed ({provisioning}) — declared in agent.json; "
            "no canopy provision manifest expected",
        )
    return CheckResult(
        name, False,
        f"neither {env_tpl.name} nor {secrets_yaml} found — agent state won't survive a new "
        "machine; add a .env.tpl and resolve it with `op inject -i .env.tpl -o "
        "~/.<agent>/.env` (see create-agent § Channel + setup; config/secrets.yaml + "
        "`canopy provision` is the legacy path), or declare \"provisioning\" in "
        "config/agent.json if this agent provisions itself some other way",
    )


def check_secrets_materialized(repo: Path) -> CheckResult:
    """The manifest's targets actually EXIST on this machine.

    `check_secrets_manifest` proves the repo *declares* what it needs; it says nothing about
    whether `canopy provision` was ever run HERE. That distinction is the whole point of a
    per-machine doctor: a fresh macOS user has every repo, every manifest, and none of the
    resolved files (`~/.<slug>/.env`, `credentials-<client>.json`). Fixable non-interactively,
    so `--fix` heals it.

    `.env.tpl` has no declared-target field (unlike `secrets.yaml`'s `env.target`), so there is
    no structural way to know where `op inject` was told to write — this check SKIPS for
    `.env.tpl`-primary agents rather than guessing a path.
    """
    name = "Secrets materialized"
    repo = Path(repo)
    if (repo / ".env.tpl").exists():
        return CheckResult(
            name, True,
            "skipped — .env.tpl declares no target field to check; verify manually with "
            "`op inject -i .env.tpl -o ~/.<agent>/.env` (or `op inject --check`)",
        )
    if not (repo / "config" / "secrets.yaml").exists():
        return CheckResult(name, True, "skipped — no canopy provision manifest (see Secrets manifest)")
    try:
        secrets = load_manifest(repo)
        env = load_env_block(repo)
    except ProvisionError as e:
        return CheckResult(name, False, str(e))
    targets = [resolve_target(s.target, Path(repo)) for s in secrets]
    if env and env.target:
        targets.append(resolve_target(env.target, Path(repo)))
    missing = [str(t) for t in targets if not t.exists()]
    if missing:
        return CheckResult(
            name, False,
            f"{len(missing)}/{len(targets)} provisioned target(s) missing on this machine "
            f"({', '.join(missing[:3])}{'…' if len(missing) > 3 else ''}) — run "
            f"`canopy provision --repo {repo}` (needs a signed-in `op`), or `--fix`",
        )
    return CheckResult(name, True, f"all {len(targets)} provisioned target(s) present")


RAILS_PROBE = "gog gmail send --to probe@example.invalid --subject probe"


def _rail_matches(rule: dict, tool_name: str, subject: str) -> bool:
    """Mirror of gating_guard._matches — predicts whether a rule fires on a subject."""
    if rule.get("tool") and rule["tool"] != tool_name:
        return False
    pattern = rule.get("pattern")
    if not pattern:
        return True
    try:
        return re.search(pattern, subject) is not None
    except re.error:
        return False


def check_rails_fire(repo: Path, *, runner=subprocess.run) -> CheckResult:
    """Rails are CONFIGURED vs rails are ENFORCED — an active probe, not a file read.

    Every other rails check reads JSON. None of them prove the guard actually blocks anything:
    a broken import, a bad interpreter, or a subtly wrong pattern all leave a perfectly valid
    config that stops nothing. So predict the guard's answer from its own effective rails, then
    execute the guard with a synthetic PreToolUse payload and require it to agree.

    The probe command is never run — it is passed as text to the hook on stdin, exactly as the
    harness would. Deny → exit 2 (gating_guard's contract). Anything else means the rails are
    declared but not in force.
    """
    name = "Rails enforced"
    guard = Path(repo) / "hooks" / "gating_guard.py"
    if not guard.exists():
        return CheckResult(name, True, "skipped — no hooks/gating_guard.py (see Hook wiring)")
    try:
        cfg = json.loads((Path(repo) / "config" / "gating.json").read_text())
    except (json.JSONDecodeError, OSError):
        return CheckResult(name, True, "skipped — gating.json unreadable (see Gating rails)")
    baseline = _baseline_rails(cfg)
    if baseline is None:
        return CheckResult(name, True, "skipped — fleet baseline unresolvable (see Gating rails)")
    rails = baseline + (cfg.get("deny") or [])
    if not any(_rail_matches(r, "Bash", RAILS_PROBE) for r in rails):
        return CheckResult(
            name, True,
            "skipped — no deny rail predicts a block for the raw-send probe, so there is "
            "nothing to assert",
        )
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": RAILS_PROBE}})
    try:
        proc = runner([sys.executable, str(guard)], input=payload,
                      capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001 — any launch failure is a real finding
        return CheckResult(name, False, f"could not execute {guard}: {e}")
    if proc.returncode == 2:
        return CheckResult(name, True,
                           "guard blocked the raw-send probe (exit 2) — rails are in force")
    return CheckResult(
        name, False,
        f"config denies the raw-send probe but gating_guard.py exited {proc.returncode} "
        f"instead of 2 — rails are DECLARED BUT NOT ENFORCED"
        + (f"; stderr: {proc.stderr.strip()[:200]}" if (proc.stderr or "").strip() else ""),
    )


def check_email_auth(
    identity: EmailIdentity | None,
    *,
    gog_dir: str | None = None,
    runner=subprocess.run,
) -> CheckResult:
    """Live gog Gmail auth for the agent's mailbox (wraps email preflight).

    Probes the EFFECTIVE identity — the client runtime will actually use, after
    `reconcile_client` lets it follow the token this box holds for the mailbox. This check
    answers "can the agent read its mail right now?", and on a box the client migration never
    reached the honest answer is yes. Probing the declared pin instead would report a hard
    auth failure for an agent that works, and turn one root cause into three red checks —
    exactly the crying-wolf this module was rebuilt to stop. The config drift is real and
    still reported, once, by `check_auth_client`.
    """
    name = "Email auth (gog)"
    if identity is None:
        return CheckResult(name, False, "skipped — identity unresolved")
    # copy, not dataclasses.replace: callers (and tests) legitimately pass any object
    # exposing slug/account/client, and this check must not become the one that demands a
    # concrete type.
    effective = copy(identity)
    rec = reconcile_client(effective, runner=runner, apply=True)
    ok, lines = preflight(effective, gog_dir=gog_dir or GOG_CONFIG_DIR, runner=runner)
    if ok and rec.changed:
        return CheckResult(
            name, True,
            f"gog Gmail ready for {identity.account} via the `{rec.client}` client, which "
            f"this machine actually holds a token for (the repo pins `{rec.declared}` — see "
            f"Auth client)")
    # Keep the WHOLE remediation — preflight's multi-line FIX blocks carry the exact
    # command / console URL; truncating to the first line hides the actual fix.
    detail = " ".join(l.strip() for l in lines) if lines else ("ready" if ok else "failed")
    return CheckResult(name, ok, detail.removeprefix("OK: ").removeprefix("FIX: "))


def check_auth_client(
    identity: EmailIdentity | None,
    *,
    runner=subprocess.run,
) -> CheckResult:
    """The mailbox's gog token is stored under the OAuth client this repo actually pins.

    `gog` keys credentials on the PAIR (mailbox, client). `gog_client` is one repo-global
    value; the token store is per-machine. So a fleet migration onto the shared `canopy`
    client, performed box by box, leaves every un-migrated box pinned to a pair with no
    credentials — reads and sends die on `No auth for gmail <mailbox>` while a perfectly good
    token sits beside them under the old client name.

    Nothing here saw that state before. `granted_services` looks up the pair, so a mailbox
    authed under a DIFFERENT client returned None — indistinguishable from "gog isn't
    installed" — and `check_auth_services` treats None as a skip. The check that should have
    named it reported green (echo, cloud box, 2026-08-10). Hence this check, and hence
    `clients_for_account` separating "cannot look" from "looked, nothing there".

    Runtime now self-heals this (`agent_email.reconcile_client` follows the token, because
    identity is the mailbox and the client is plumbing), so the agent keeps working — which
    is exactly why the drift still has to be REPORTED here rather than silently absorbed.
    """
    name = "Auth client"
    if identity is None:
        return CheckResult(name, False, "skipped — identity unresolved")
    found = clients_for_account(identity.account, runner=runner)
    if found is None:
        return CheckResult(name, True, "skipped — gog auth not introspectable (see Email auth)")
    holders = [e for e in found if e["client"]]
    if any(e["client"] == identity.client for e in holders):
        return CheckResult(
            name, True,
            f"{identity.account} is authed under the configured `{identity.client}` client")
    if not holders:
        return CheckResult(
            name, False,
            f"no gog token on this machine for {identity.account} under any client — "
            f"run: gog login {identity.account} --client {identity.client} "
            f"--services {','.join(sorted(required_services(identity)[0]))}")
    names = sorted(e["client"] for e in holders)
    # Re-login onto the configured client must re-request what the stray token already had:
    # `gog login --services` REPLACES the grant set, so migrating the client with a narrower
    # list would quietly revoke scopes the agent uses.
    # `services` is None for a client gog lists only in its token store (no per-pair scope
    # data exists) — that is UNKNOWN, not empty, so it contributes nothing to the union
    # rather than narrowing it.
    keep = (set().union(*(e["services"] or set() for e in holders))
            | required_services(identity)[0])
    # Name BOTH directions. Which side is stale is not knowable from here — the box may have
    # missed the migration, or this checkout may simply predate a pin that already moved (on
    # 2026-08-11 both ace and echo flagged here for exactly the latter reason). A one-way fix
    # would be wrong half the time, and the wrong half costs a browser re-login and a
    # scope-replacing grant.
    return CheckResult(
        name, False,
        f"{identity.account} is authed under {'`' + '`, `'.join(names) + '`'} but the repo "
        f"pins `{identity.client}` — this box and this checkout disagree. Runtime follows the "
        f"token, so the agent still works. Fix whichever side is stale: if the pin is right, "
        f"migrate the box — gog login {identity.account} --client {identity.client} "
        f"--services {','.join(sorted(keep))} — or if the box is right, point gog_client at "
        f"`{names[0]}` in config/agent.json (pull first; the pin may already have moved)")


def check_auth_services(
    identity: EmailIdentity | None,
    *,
    runner=subprocess.run,
) -> CheckResult:
    """Every service THIS agent requires is actually granted for its gog auth.

    Email auth (check_email_auth) proves the token is alive for Gmail; this proves the login
    covered the surface the agent actually uses. A token that works for Gmail but never
    consented to, say, Slides would silently 403 the first time the agent builds a deck, so
    we catch it here with the exact re-login fix.

    The requirement is PER-AGENT (`required_services`), not the fleet-wide LOGIN_SERVICES
    default: requiring that constant of everyone failed hal and ace over `appscript`, which
    neither uses, while never noticing that echo needs `slides`, which the constant omits.

    Score the client that ACTUALLY holds the mailbox's token, not the one the repo pins. The
    two diverge on any box the fleet's client migration never reached, and scoring the pinned
    pair there yields no grant set at all — which this check used to read as "can't tell" and
    skip, going green on a genuinely broken box (echo, cloud, 2026-08-10). The mismatch
    itself is `check_auth_client`'s finding; here it must not stop the scopes being scored.

    "Cannot introspect gog" and "this mailbox has no token anywhere" remain skips —
    `check_email_auth` and `check_auth_client` own those, and repeating them here would just
    be a second red herring."""
    name = "Auth services"
    if identity is None:
        return CheckResult(name, False, "skipped — identity unresolved")
    required, source = required_services(identity)
    found = clients_for_account(identity.account, runner=runner)
    if found is None:
        return CheckResult(name, True, "skipped — gog auth not introspectable (see Email auth)")
    holders = [e for e in found if e["client"]]
    if not holders:
        return CheckResult(name, True, "skipped — mailbox has no gog token (see Auth client)")
    granted = next((e["services"] for e in holders if e["client"] == identity.client),
                   None)
    if granted is None:
        # Either the pinned client holds nothing, or it holds a token whose grant set gog
        # will not reveal (`services` is None = UNKNOWN, not empty — gog publishes no
        # per-pair scopes). Score the tokens the mailbox really has. With one holder that is
        # unambiguous; with several, the union is the charitable reading — it cannot
        # manufacture a false MISSING scope.
        known = [e["services"] for e in holders if e["services"] is not None]
        if not known:
            # Every holder's grant set is unknown. An empty union here would report the
            # mailbox as missing EVERY required scope — a false red on a box that may be
            # perfectly authorized. "Cannot tell" is a skip, exactly as above.
            return CheckResult(
                name, True,
                "skipped — gog exposes no scopes for this mailbox's stored client(s) "
                "(see Auth client)")
        granted = set().union(*known)
    missing = sorted(required - granted)
    if missing:
        # Re-login with required UNION already-granted: `gog login --services` REPLACES the
        # grant set, so remediating with the required list alone would silently revoke scopes
        # the agent had and uses.
        relogin = ",".join(sorted(required | granted))
        return CheckResult(
            name, False,
            f"missing scope(s) {missing} for {identity.account} (required per {source}) — "
            f"re-run: gog login {identity.account} --client {identity.client} "
            f"--services {relogin}",
        )
    return CheckResult(
        name, True,
        f"all {len(required)} required service(s) granted per {source} "
        f"({','.join(sorted(required))})",
    )


PLUGIN_REGISTRY = "~/.claude/plugins/installed_plugins.json"


def check_plugin_install(repo: Path, *, registry_path: str | None = None) -> CheckResult:
    """The agent's Claude Code PLUGIN is installed on this machine.

    Every other check reads the agent's REPO, so all of them pass on a machine where the
    repo is cloned but the plugin was never installed — and none of the agent's skills
    (`/ada:turn`, `/echo:turn`) can actually be invoked there. That is the dominant real-world
    gap when moving to a new machine or macOS user: on one such account, four of five agents
    had a full checkout, valid config, and no plugin.

    A missing registry is a SKIP, not a failure — same pattern as auth services: absence of
    introspection is not evidence of breakage.
    """
    name = "Plugin install"
    manifest = Path(repo) / ".claude-plugin" / "plugin.json"
    if not manifest.exists():
        return CheckResult(name, True, "n/a — repo ships no .claude-plugin/plugin.json")
    try:
        plugin_name = (json.loads(manifest.read_text()).get("name") or "").strip()
    except (json.JSONDecodeError, OSError) as e:
        return CheckResult(name, False, f"{manifest} unreadable: {e}")
    if not plugin_name:
        return CheckResult(name, False, f'{manifest} has no "name"')
    reg_file = Path(registry_path or PLUGIN_REGISTRY).expanduser()
    try:
        installed = json.loads(reg_file.read_text()).get("plugins", {})
    except (json.JSONDecodeError, OSError):
        return CheckResult(name, True, f"skipped — no plugin registry at {reg_file}")
    matches = {k: v for k, v in installed.items() if k.split("@", 1)[0] == plugin_name}
    if not matches:
        source = _plugin_source(repo)
        return CheckResult(
            name, False,
            f"plugin {plugin_name!r} is NOT installed — its repo is here but none of its "
            f"skills can be invoked on this machine. Install it: "
            f"`/plugin marketplace add {source}` then `/plugin install {plugin_name}@{plugin_name}`",
        )
    key, entries = next(iter(matches.items()))
    entry = (entries or [{}])[0]
    detail = f"{key} installed ({entry.get('scope', 'unknown')} scope, v{entry.get('version', '?')})"
    return CheckResult(name, True, detail)


def _plugin_source(repo: Path) -> str:
    """`owner/repo` for the marketplace-add remediation — from config/agent.json's `repo`,
    else the git origin remote, else the directory name as a last resort."""
    try:
        data = json.loads((Path(repo) / "config" / "agent.json").read_text())
        if (declared := (data.get("repo") or "").strip()):
            return declared
    except (json.JSONDecodeError, OSError):
        pass
    try:
        r = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and (url := r.stdout.strip()):
            slug = url.removesuffix(".git").replace("git@github.com:", "")
            slug = slug.replace("https://github.com/", "")
            if slug:
                return slug
    except Exception:
        pass
    # RESOLVE before taking `.name`: the documented invocation is
    # `canopy agent doctor --repo .`, and `Path(".").name` is the EMPTY STRING.
    # Unresolved, the last-resort fallback rendered the remediation as
    # "`/plugin marketplace add ` then `/plugin install scout@scout`" — a command
    # a new operator cannot run and cannot guess the missing argument for. This
    # fallback fires exactly when the other two legs are empty, which is the
    # freshly-scaffolded repo (`create-agent` writes no `repo` key and `git init`s
    # with no remote) — i.e. the one case where the reader is newest and least able
    # to recover. Found 2026-08-27 walking the onboarding path end to end.
    return Path(repo).resolve().name


def check_required_plugins(repo: Path, *, registry_path: str | None = None) -> CheckResult:
    """OTHER plugins this agent's skills call must be installed here too.

    `check_plugin_install` covers the agent's OWN plugin; an agent whose capabilities come
    from a sibling plugin passes that check and still can't work. Eva's Salesforce + Drive
    tools are `mcp__plugin_chrome-sales_*` from the chrome-sales plugin — a dependency stated
    only in her CLAUDE.md and `.mcp.json` `_doc`, i.e. prose no check reads. Her 2026-07-25
    readiness drill reported it missing; nothing enforced it, so the next turn would have
    reached for a tool that wasn't there.

    Declared per-agent in config/agent.json `required_plugins` — the AGENT's dependency, not a
    fleet constant (same rule as `gog_services`: hal must not fail over a plugin only eva
    calls). Entries are a bare name, or an object with `marketplace` (the `owner/repo` to add)
    and optional `marketplace_name` (defaults to the plugin name, true across this fleet:
    eva@eva, chrome-sales@chrome-sales) and `note` for a follow-up setup step.

    A missing registry is a SKIP, not a failure — absence of introspection is not evidence of
    breakage, same as auth services.
    """
    name = "Required plugins"
    try:
        declared = json.loads(
            (Path(repo) / "config" / "agent.json").read_text()).get("required_plugins") or []
    except (json.JSONDecodeError, OSError):
        declared = []
    if not declared:
        return CheckResult(name, True, "n/a — none declared in config/agent.json")
    reg_file = Path(registry_path or PLUGIN_REGISTRY).expanduser()
    try:
        installed = json.loads(reg_file.read_text()).get("plugins", {})
    except (json.JSONDecodeError, OSError):
        return CheckResult(name, True, f"skipped — no plugin registry at {reg_file}")
    have = {k.split("@", 1)[0] for k in installed}
    missing = []
    for entry in declared:
        spec = {"name": entry} if isinstance(entry, str) else dict(entry or {})
        if not (plugin := (spec.get("name") or "").strip()):
            return CheckResult(name, False,
                               f"config/agent.json required_plugins has an entry with no name: "
                               f"{entry!r}")
        if plugin in have:
            continue
        mkt_name = (spec.get("marketplace_name") or plugin).strip()
        source = (spec.get("marketplace") or plugin).strip()
        fix = (f"`/plugin marketplace add {source}` then "
               f"`/plugin install {plugin}@{mkt_name}`")
        if note := (spec.get("note") or "").strip():
            fix += f" — {note}"
        missing.append(f"{plugin}: {fix}")
    if missing:
        return CheckResult(
            name, False,
            f"{len(missing)} declared dependency plugin(s) NOT installed — the skills that "
            f"call them will fail mid-turn. " + " | ".join(missing),
        )
    names = ", ".join(sorted(
        (e if isinstance(e, str) else (e or {}).get("name", "?")) for e in declared))
    return CheckResult(name, True, f"all {len(declared)} declared plugin(s) installed ({names})")


def check_registration(
    identity: EmailIdentity | None,
    *,
    client_factory=AgentClient,
) -> CheckResult:
    """Agent registered on canopy-web and its board reachable (one live GET)."""
    name = "canopy-web board"
    if identity is None:
        return CheckResult(name, False, "skipped — identity unresolved")
    try:
        pending = client_factory({"slug": identity.slug}).pending_commands()
    except CanopyError as e:
        msg = str(e)
        if "404" in msg or "not found" in msg.lower():
            return CheckResult(
                name, False,
                f"agent {identity.slug!r} not registered — run "
                "`canopy agent-publish register --repo .`",
            )
        return CheckResult(name, False, msg)
    except RuntimeError as e:  # missing PAT / transport config
        return CheckResult(name, False, str(e))
    return CheckResult(name, True, f"registered; board reachable ({len(pending)} pending command(s))")


def _default_provisioner(repo: Path) -> str:
    from orchestrator.provision import provision as _provision
    summary = _provision(Path(repo))
    if summary.get("errors"):
        raise ProvisionError("; ".join(str(e) for e in summary["errors"][:3]))
    return (f"provisioned {summary.get('provisioned', 0)} target(s), "
            f"skipped {summary.get('skipped', 0)}")


def _default_registrar(repo: Path) -> str:
    from orchestrator.agent_web import register as _register
    result = _register(Path(repo))
    return f"registered {result.get('slug', Path(repo).name)} on canopy-web"


# Which failing checks `--fix` can heal, and with what. Deliberately SHORT: a fixer earns its
# place only if it is non-interactive, idempotent, and cannot destroy work. Everything else
# (gog consent, plugin install, PAT mint, config authorship) stays a printed instruction —
# a doctor that half-performs an interactive step leaves a worse mess than one that asks.
FIXERS = {
    "Secrets materialized": ("canopy provision", _default_provisioner),
    "canopy-web board": ("canopy agent-publish register", _default_registrar),
}


def heal_agent(repo: Path, results: list[CheckResult], *, fixers=None) -> list[tuple[str, bool, str]]:
    """Attempt the safe fixes for whichever checks failed. Returns [(action, ok, detail)]."""
    fixers = FIXERS if fixers is None else fixers
    actions: list[tuple[str, bool, str]] = []
    for r in results:
        if r.ok or r.name not in fixers:
            continue
        label, fn = fixers[r.name]
        try:
            actions.append((label, True, fn(Path(repo))))
        except Exception as e:  # noqa: BLE001 — surface any fixer failure verbatim
            actions.append((label, False, str(e)))
    return actions


def run_agent_doctor(
    repo: Path,
    *,
    gog_dir: str | None = None,
    runner=subprocess.run,
    client_factory=AgentClient,
    registry_path: str | None = None,
) -> tuple[list[CheckResult], bool]:
    """Run every per-agent check and return (results, overall_ok).

    ``gog_dir``, ``runner`` and ``client_factory`` are injectable for testing;
    production callers pass nothing and the real dependencies are used.
    """
    repo = Path(repo)
    ident_result, identity = check_identity(repo)
    results = [
        ident_result,
        check_plugin_install(repo, registry_path=registry_path),
        check_required_plugins(repo, registry_path=registry_path),
        check_gating(repo),
        check_hook_wiring(repo),
        check_secrets_manifest(repo),
        check_secrets_materialized(repo),
        check_rails_fire(repo),
        check_email_auth(identity, gog_dir=gog_dir, runner=runner),
        check_auth_client(identity, runner=runner),
        check_auth_services(identity, runner=runner),
        check_registration(identity, client_factory=client_factory),
    ]
    overall_ok = all(r.ok for r in results)
    return results, overall_ok
