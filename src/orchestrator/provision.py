"""LEGACY agent secret provisioning from 1Password (`config/secrets.yaml` + `canopy provision`).

**Superseded as the fleet standard by `.env.tpl` + native `op inject` / `op read`** (see
`agent-core/agent-runtime.md`) — the fleet has migrated (ada/eva/hal/echo ship `.env.tpl`,
`secrets.yaml` deleted). This module stays fully functional for agents/repos that haven't
migrated yet and is still called during that transition; don't build new agents on it.

The problem this solved: agents need credentials (service-account keys, OAuth creds, PATs) that
must never live in git, yet must be available wherever the agent runs — the main checkout, any
emdash worktree, and on every operator's machine. Hand-shuffling keys around is the lazy,
non-portable status quo.

The (legacy) fix: each agent/provider repo declares a tracked `config/secrets.yaml` that lists
what it needs as **1Password references + local targets** (NO secret values). `canopy provision`
materializes them via the `op` CLI into their targets — idempotent, validated, one command on any
machine. 1Password is the source of truth either way (`op` is already the auth path for
echo/ace/chrome-sales) — only the manifest format and the resolving command differ from the
current `.env.tpl` + `op inject` standard.

Manifest (`config/secrets.yaml`, legacy):

    secrets:
      - name: chrome-sales-gws-sa          # human label
        op: "op://AI-Agents/chrome-sales GWS SA/key"   # what `op read` resolves
        target: "{repo}/.gws-sa-key.json"  # where to write ({repo} = this repo; ~ ok; rel = repo-relative)
        mode: "0600"                        # default 0600 for files
        optional: false                     # if true, a missing op ref is skipped, not an error

Targets that should survive worktrees (things an agent reads globally) point at an absolute path
like `~/.canopy/agents/<slug>/<name>`; provider-local creds (e.g. chrome-sales's MCP keys) point
at `{repo}/...` in the provider's main checkout, where its launcher resolves them.
"""
from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml


class ProvisionError(Exception):
    pass


@dataclass
class Secret:
    name: str
    op_ref: str
    target: str
    mode: str = "0600"
    optional: bool = False


def load_manifest(repo: Path) -> list[Secret]:
    """Parse <repo>/config/secrets.yaml into Secret entries."""
    path = Path(repo) / "config" / "secrets.yaml"
    if not path.exists():
        raise ProvisionError(f"no secrets manifest at {path}")
    data = yaml.safe_load(path.read_text()) or {}
    items = data.get("secrets") or []
    if not isinstance(items, list):
        raise ProvisionError(f"{path}: `secrets` must be a list")
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not it.get("name") or not it.get("op") or not it.get("target"):
            raise ProvisionError(f"{path}: secret #{i} needs name, op, target")
        out.append(Secret(
            name=it["name"], op_ref=it["op"], target=it["target"],
            mode=str(it.get("mode", "0600")), optional=bool(it.get("optional", False)),
        ))
    return out


@dataclass
class EnvVar:
    key: str
    op_ref: str = ""        # exactly one of op_ref / value
    value: str = ""
    optional: bool = False


@dataclass
class EnvBlock:
    target: str             # the ONE .env file all vars are written into (worktree-clean global home)
    vars: list              # list[EnvVar]
    mode: str = "0600"


def load_env_block(repo: Path):
    """Parse the OPTIONAL `env:` block of secrets.yaml — many KEY=value materialized into ONE .env
    file (a stable global home like `~/.<slug>/.env`, NOT a per-worktree repo file). Returns
    EnvBlock or None. Each var carries an `op:` ref (a secret) OR a literal `value:` (a non-secret
    id/name); `optional: true` lets a missing op ref be skipped instead of failing the whole file."""
    path = Path(repo) / "config" / "secrets.yaml"
    if not path.exists():
        return None
    blk = (yaml.safe_load(path.read_text()) or {}).get("env")
    if not blk:
        return None
    if not isinstance(blk, dict) or not blk.get("target") or not isinstance(blk.get("vars"), list):
        raise ProvisionError(f"{path}: `env` needs a `target` and a `vars` list")
    out = []
    for i, it in enumerate(blk["vars"]):
        if not isinstance(it, dict) or not it.get("key"):
            raise ProvisionError(f"{path}: env var #{i} needs a `key`")
        if not it.get("op") and "value" not in it:
            raise ProvisionError(f"{path}: env var {it['key']} needs `op` (secret) or `value` (literal)")
        out.append(EnvVar(key=it["key"], op_ref=it.get("op", ""),
                          value=str(it.get("value", "")), optional=bool(it.get("optional", False))))
    return EnvBlock(target=blk["target"], vars=out, mode=str(blk.get("mode", "0600")))


def resolve_target(target: str, repo: Path) -> Path:
    """Resolve a manifest target to an absolute path: {repo} → repo; ~ expanded; rel → repo/rel."""
    t = target.replace("{repo}", str(Path(repo)))
    p = Path(t).expanduser()
    if not p.is_absolute():
        p = Path(repo) / p
    return p


def _op_read(ref: str) -> str:
    """Resolve a 1Password reference to its value via the `op` CLI."""
    try:
        r = subprocess.run(["op", "read", ref], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise ProvisionError("the 1Password CLI `op` is not installed / on PATH")
    except subprocess.TimeoutExpired:
        raise ProvisionError(f"`op read {ref}` timed out")
    if r.returncode != 0:
        raise ProvisionError(f"`op read {ref}` failed: {r.stderr.strip()[:200]}")
    return r.stdout


def provision(repo: Path, *, op_read=_op_read, check: bool = False) -> dict:
    """Materialize every secret in the repo's manifest from 1Password into its target.

    check=True validates that each op ref resolves (and reports where it WOULD write) without
    writing anything. Returns {provisioned, skipped, errors, results}. Idempotent: re-running
    overwrites targets with the current 1Password value.
    """
    repo = Path(repo)
    secrets = load_manifest(repo)
    results, errors = [], []
    provisioned = skipped = 0
    for s in secrets:
        dest = resolve_target(s.target, repo)
        try:
            value = op_read(s.op_ref)
        except ProvisionError as e:
            if s.optional:
                skipped += 1
                results.append({"name": s.name, "status": "skipped", "reason": str(e)})
                continue
            errors.append(f"{s.name}: {e}")
            results.append({"name": s.name, "status": "error", "reason": str(e)})
            continue
        if not value.strip():
            (errors if not s.optional else None)
            results.append({"name": s.name, "status": "empty", "target": str(dest)})
            if not s.optional:
                errors.append(f"{s.name}: 1Password returned an empty value")
            else:
                skipped += 1
            continue
        if check:
            results.append({"name": s.name, "status": "ok", "target": str(dest), "would_write": True})
            provisioned += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(value)
        try:
            dest.chmod(int(s.mode, 8))
        except ValueError:
            dest.chmod(0o600)
        results.append({"name": s.name, "status": "written", "target": str(dest),
                        "mode": oct(stat.S_IMODE(dest.stat().st_mode))})
        provisioned += 1

    # The env block: materialize many KEY=value into ONE .env (the worktree-clean global home).
    # Resolve everything FIRST; only write if all required vars succeeded (never half-write a .env
    # over a good one).
    env = load_env_block(repo)
    if env is not None:
        dest = resolve_target(env.target, repo)
        env_lines, env_failed = [], False
        for v in env.vars:
            if v.op_ref:
                try:
                    val = op_read(v.op_ref).rstrip("\n")
                except ProvisionError as e:
                    if v.optional:
                        skipped += 1
                        results.append({"name": f"env:{v.key}", "status": "skipped", "reason": str(e)})
                        continue
                    errors.append(f"env:{v.key}: {e}")
                    results.append({"name": f"env:{v.key}", "status": "error", "reason": str(e)})
                    env_failed = True
                    continue
                if not val.strip():
                    if v.optional:
                        skipped += 1
                        continue
                    errors.append(f"env:{v.key}: 1Password returned an empty value")
                    env_failed = True
                    continue
            else:
                val = v.value
            env_lines.append(f"{v.key}={val}")
            provisioned += 1
        if env_failed:
            results.append({"name": "env-file", "status": "error", "target": str(dest),
                            "reason": "a required env var failed — .env NOT written"})
        elif check:
            results.append({"name": "env-file", "status": "ok", "target": str(dest),
                            "would_write": True, "vars": len(env_lines)})
        else:
            body = ("# Provisioned by `canopy provision` from config/secrets.yaml — do not edit by hand.\n"
                    + "\n".join(env_lines) + "\n")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(body)
            try:
                dest.chmod(int(env.mode, 8))
            except ValueError:
                dest.chmod(0o600)
            results.append({"name": "env-file", "status": "written", "target": str(dest),
                            "vars": len(env_lines), "mode": oct(stat.S_IMODE(dest.stat().st_mode))})

    return {
        "repo": str(repo), "check": check,
        "provisioned": provisioned, "skipped": skipped,
        "errors": errors, "results": results,
    }
