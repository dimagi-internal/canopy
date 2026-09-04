"""OpenClaw harvester — bridge a live OpenClaw instance into the canopy fleet.

The OpenClaw droplets (echo's predecessors: hal, eva, …) are dead-end brains, but real ideas
evolved on them — persona text, skills, memory. This harvests that: read everything off an
OpenClaw (safe to read — we assume the droplet could be compromised), compare it to the agent's
GitHub repo, and either **bootstrap** a new canopy agent repo from it or **reconcile** its
latest-and-greatest skills/ideas into the existing repo.

Three layers, decoupled so the valuable part (compare/bootstrap) is pure and testable:
  - snapshot_via_ssh(host, into)  — thin best-effort pull of the readable workspace (NOT creds).
  - inventory_snapshot(dir)       — parse persona + skills + memory from a local snapshot.
  - compare(inv, repo) / bootstrap_from_snapshot(...) — the reconciliation engine.

SAFETY: OpenClaw *content* (persona/skills/memory) is safe to read, but credential files
(auth-profiles.json, channels.json, *token*) carry live secrets and must NEVER land in a git
repo. The snapshot excludes them by default; the engine only ever reads workspace text.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from orchestrator.agent_factory import AgentSpec, create_agent

# OpenClaw workspace layout (from reef's integration): persona + skills + memory live here.
WORKSPACE_TEXT = ("SOUL.md", "IDENTITY.md", "TOOLS.md", "HEARTBEAT.md", "BOOTSTRAP.md", "MEMORY.md")
# Never pull these into a snapshot that may be committed — they hold live tokens.
SECRET_EXCLUDES = ("auth-profiles.json", "channels.json", "*token*", "*.key", "*.pem", "credentials*")


class HarvestError(Exception):
    pass


def snapshot_via_ssh(host: str, into: Path, openclaw_root: str = "~/.openclaw") -> list[str]:
    """Best-effort rsync of an OpenClaw's *readable workspace* (persona/skills/memory) to `into`.

    Excludes credential files. `host` is anything ssh can reach (user@ip, or an ssh-config alias —
    reef resolves DO droplet IPs + 1Password keys; point ssh at the result). Returns the relative
    paths pulled. Raises HarvestError if rsync/ssh isn't available or the pull fails.
    """
    into = Path(into).expanduser()
    into.mkdir(parents=True, exist_ok=True)
    if not shutil.which("rsync"):
        raise HarvestError("rsync not found — install it, or copy the OpenClaw workspace manually")
    excludes = []
    for pat in SECRET_EXCLUDES:
        excludes += ["--exclude", pat]
    # Pull the whole workspace dir (text + skills/ + memory/), minus secrets.
    src = f"{host}:{openclaw_root}/workspace/"
    cmd = ["rsync", "-az", "--prune-empty-dirs", *excludes, src, str(into) + "/"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise HarvestError(f"rsync failed: {e}")
    if r.returncode != 0:
        raise HarvestError(f"rsync {src} -> {into} failed: {r.stderr.strip()[:300]}")
    return [str(p.relative_to(into)) for p in sorted(into.rglob("*")) if p.is_file()]


def _parse_skill(path: Path) -> dict:
    """name/description/size from a SKILL.md — handles canopy frontmatter AND freeform OpenClaw."""
    text = path.read_text(errors="replace", encoding="utf-8")
    name = path.parent.name
    desc = ""
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if m:
        block = m.group(1)
        nm = re.search(r"^name:\s*(.+)$", block, re.M)
        if nm:
            name = nm.group(1).strip()
        dm = re.search(r"^description:\s*(?:>\s*)?\n?((?:.|\n)*?)(?:\n\w[\w-]*:|\Z)", block, re.M)
        if dm:
            desc = " ".join(l.strip() for l in dm.group(1).splitlines()).strip()
    if not desc:
        hm = re.search(r"^#\s*(.+)$", text, re.M)
        desc = (hm.group(1).strip() if hm else text.strip().split("\n", 1)[0])[:240]
    return {"name": name, "key": path.parent.name, "description": desc,
            "size": len(text), "path": str(path)}


def inventory_snapshot(snapshot_dir: Path) -> dict:
    """Inventory a local OpenClaw snapshot: persona, skills, memory."""
    d = Path(snapshot_dir).expanduser()
    if not d.exists():
        raise HarvestError(f"snapshot dir not found: {d}")
    persona = {}
    for fn in ("SOUL.md", "IDENTITY.md"):
        p = d / fn
        if p.exists():
            persona[fn] = p.read_text(errors="replace", encoding="utf-8")
    skills = [_parse_skill(p) for p in sorted(d.glob("skills/*/SKILL.md"))]
    memory = [
        {"name": p.name, "size": p.stat().st_size, "path": str(p)}
        for p in sorted(d.glob("memory/*.md"))
    ]
    other_text = [fn for fn in WORKSPACE_TEXT if (d / fn).exists()]
    return {
        "snapshot_dir": str(d),
        "persona": persona,
        "has_persona": bool(persona),
        "skills": skills,
        "memory": memory,
        "workspace_files": other_text,
    }


def _repo_skill_keys(repo: Path) -> set[str]:
    return {p.parent.name for p in Path(repo).glob("skills/*/SKILL.md")}


def compare(inv: dict, repo: Path | None) -> dict:
    """Compare an OpenClaw inventory against a canopy agent repo (None = repo doesn't exist yet)."""
    oc_keys = {s["key"] for s in inv["skills"]}
    if repo is None or not Path(repo).exists():
        return {
            "repo_exists": False,
            "recommendation": "bootstrap",
            "only_in_openclaw": sorted(oc_keys),
            "only_in_repo": [],
            "in_both": [],
            "summary": f"No canopy repo — bootstrap a new agent from {len(oc_keys)} OpenClaw skill(s) "
                       f"+ persona.",
        }
    repo_keys = _repo_skill_keys(repo)
    only_oc = sorted(oc_keys - repo_keys)
    return {
        "repo_exists": True,
        "repo": str(repo),
        "recommendation": "reconcile" if only_oc else "up_to_date",
        "only_in_openclaw": only_oc,
        "only_in_repo": sorted(repo_keys - oc_keys),
        "in_both": sorted(oc_keys & repo_keys),
        "summary": (
            f"{len(only_oc)} skill(s) on the OpenClaw not in the repo — port them: "
            + ", ".join(only_oc)
        ) if only_oc else "Repo already has every OpenClaw skill (by name). Check bodies for drift.",
    }


def _seed_persona(repo: Path, inv: dict) -> None:
    """Append the OpenClaw's SOUL/IDENTITY into the new repo's persona.md for the human to refine."""
    persona = inv.get("persona") or {}
    if not persona:
        return
    pp = repo / "persona.md"
    extra = ["\n\n## Ported from the OpenClaw (raw — refine, then delete this note)\n"]
    for fn, body in persona.items():
        extra.append(f"\n### {fn}\n\n{body.strip()}\n")
    pp.write_text(pp.read_text(encoding="utf-8") + "".join(extra), encoding="utf-8")


def bootstrap_from_snapshot(
    inv: dict, *, slug: str, display_name: str, mandate: str, into: Path,
    mailbox: str = "", force: bool = False,
) -> dict:
    """Scaffold a NEW canopy agent repo seeded from an OpenClaw snapshot: factory scaffold +
    seeded persona + ported skills. Returns {repo, ported_skills, scaffold_files}."""
    repo = Path(into).expanduser()
    spec = AgentSpec(slug=slug, display_name=display_name, mandate=mandate, mailbox=mailbox)
    written = create_agent(spec, repo, force=force)
    _seed_persona(repo, inv)
    ported = [k for k in (_copy_skill_dir(s, repo) for s in inv["skills"]) if k]
    return {
        "repo": str(repo),
        "ported_skills": ported,
        "scaffold_files": len(written),
        "persona_seeded": inv.get("has_persona", False),
    }


def port_new_skills(inv: dict, repo: Path) -> list[str]:
    """Reconcile: copy OpenClaw skills missing from an existing repo into it (for a PR). Returns
    the skill keys ported. Never overwrites an existing skill. Ports the WHOLE skill dir
    (SKILL.md + bundled assets), not just SKILL.md."""
    return [k for k in (_copy_skill_dir(s, Path(repo)) for s in inv["skills"]) if k]


# Junk that should never be ported with a skill.
_SKILL_IGNORE = ("node_modules", ".git", "__pycache__", "*.pyc", ".DS_Store", "*.skill")


def _copy_skill_dir(skill: dict, repo: Path) -> str | None:
    """Copy a harvested skill's WHOLE directory into repo/skills/<key>/. Never clobbers an
    existing skill dir (so factory skills + already-ported skills survive). Returns the key if
    copied, else None."""
    src_dir = Path(skill["path"]).parent
    dest_dir = repo / "skills" / skill["key"]
    if dest_dir.exists():
        return None
    shutil.copytree(src_dir, dest_dir, ignore=shutil.ignore_patterns(*_SKILL_IGNORE))
    return skill["key"]
