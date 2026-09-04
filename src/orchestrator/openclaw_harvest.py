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

import io
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

from orchestrator.agent_factory import AgentSpec, create_agent

# OpenClaw workspace layout (from reef's integration): persona + skills + memory live here.
# USER.md is who the agent works for — the single most load-bearing file for an agent's voice,
# and it was missing from this tuple, so nothing carried it across.
WORKSPACE_TEXT = ("SOUL.md", "IDENTITY.md", "USER.md", "TOOLS.md", "HEARTBEAT.md",
                  "BOOTSTRAP.md", "MEMORY.md")
# The two that define the agent's voice — these get folded INTO persona.md. The rest are carried
# across verbatim (see _carry_workspace) rather than inlined, because they are reference material,
# not voice.
PERSONA_TEXT = ("SOUL.md", "IDENTITY.md", "USER.md")
# Never pull these into a snapshot that may be committed — they hold live tokens.
#
# THESE ARE FILENAME PATTERNS AND NOTHING MORE. They cannot see a token sitting INSIDE
# state/, memory/, or a workspace-state JSON — such a file has an innocent name, is copied,
# and lands in the first commit with nothing flagged. Observed on a real harvest: the six
# patterns matched zero files, which reads as "clean" and actually means "not checked".
# scan_snapshot_for_secrets() below is the content-level check; bootstrap runs it and warns.
SECRET_EXCLUDES = ("auth-profiles.json", "channels.json", "*token*", "*.key", "*.pem", "credentials*")

# Bulk that is rebuildable and swamps the transfer. On a real harvest the workspace was 24MB,
# of which node_modules was ~23.7MB — and package.json + package-lock.json come across, so it
# rebuilds. Excluding it took the same snapshot to 320KB.
TRANSFER_EXCLUDES = ("node_modules", ".git", "__pycache__", "*.pyc", ".venv", ".DS_Store")

# High-signal, low-noise credential shapes. Deliberately conservative: this warns a human, it
# does not block, so a false positive costs a glance and a false negative costs a leaked token.
_SECRET_PATTERNS = (
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("OpenAI/Anthropic key", re.compile(r"\b(?:sk|sk-ant)-[A-Za-z0-9_\-]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("assigned secret", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|passwd|password|access[_-]?token|refresh[_-]?token)"
        r"\s*[=:]\s*[\"']?[A-Za-z0-9/+_\-]{16,}")),
)


def scan_snapshot_for_secrets(snapshot_dir: Path, *, max_bytes: int = 2_000_000) -> list[dict]:
    """Content-scan a snapshot for credential shapes. Returns findings (file, line, what).

    The filename excludes above are a coarse first pass; this is the one that would actually
    catch a token pasted into a memory note. Binary and oversized files are skipped — a token
    is text, and reading a 200MB blob to find one is not a trade worth making.
    """
    d = Path(snapshot_dir).expanduser()
    findings: list[dict] = []
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        try:
            if p.stat().st_size > max_bytes:
                continue
            text = p.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue                      # unreadable or binary — not where a pasted token lives
        for line_no, line in enumerate(text.splitlines(), 1):
            for label, pat in _SECRET_PATTERNS:
                if pat.search(line):
                    findings.append({
                        "file": str(p.relative_to(d)), "line": line_no, "kind": label,
                    })
                    break
    return findings


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
    if shutil.which("rsync"):
        return _snapshot_rsync(host, into, openclaw_root)
    if shutil.which("ssh") and shutil.which("tar"):
        return _snapshot_ssh_tar(host, into, openclaw_root)
    raise HarvestError(
        "no way to pull the workspace: need either rsync, or ssh + tar (both ship with "
        "Windows 10+ and Git for Windows)"
    )


def _snapshot_rsync(host: str, into: Path, openclaw_root: str) -> list[str]:
    excludes = []
    for pat in SECRET_EXCLUDES:
        excludes += ["--exclude", pat]
    for pat in TRANSFER_EXCLUDES:
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


def _snapshot_ssh_tar(host: str, into: Path, openclaw_root: str) -> list[str]:
    """Stream the workspace over `ssh … tar` — the no-rsync path.

    rsync is not on Windows, and it is not optional anywhere in this module's docs, so the
    whole harvest route was unreachable there. ssh and tar both ship with Windows 10+ (and
    with Git for Windows), so this removes the dependency rather than working around it.

    The remote tar does the excluding, so secrets never cross the wire in the first place —
    the same guarantee rsync's --exclude gives, not a weaker one.
    """
    excludes = [f"--exclude={pat}" for pat in (*SECRET_EXCLUDES, *TRANSFER_EXCLUDES)]
    remote = f"cd {openclaw_root}/workspace && tar cz " + " ".join(excludes) + " ."
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", host, remote],
            capture_output=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise HarvestError(f"ssh/tar snapshot failed: {e}")
    if r.returncode != 0:
        raise HarvestError(
            f"ssh {host} tar failed: {r.stderr.decode('utf-8', 'replace').strip()[:300]}"
        )
    try:
        with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:gz") as tf:
            _safe_extract(tf, into)
    except tarfile.TarError as e:
        raise HarvestError(f"could not read the tar stream from {host}: {e}")
    return [str(p.relative_to(into)) for p in sorted(into.rglob("*")) if p.is_file()]


def _safe_extract(tf: "tarfile.TarFile", into: Path) -> None:
    """Extract, refusing any member that would escape `into`.

    The tar comes off a host we explicitly assume could be compromised (see the module
    docstring), so an absolute path or a `../` traversal in a member name is exactly the
    thing to defend against — writing outside the snapshot dir would be arbitrary file
    overwrite on the operator's machine.
    """
    root = into.resolve()
    safe = []
    for member in tf.getmembers():
        if member.issym() or member.islnk():
            continue                                   # links can point anywhere; skip them
        target = (root / member.name).resolve()
        if target == root or root in target.parents:
            safe.append(member)
    tf.extractall(into, members=safe)


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
    for fn in PERSONA_TEXT:
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


def _carry_workspace(snapshot_dir: Path, repo: Path) -> dict:
    """Copy the OpenClaw's memory and remaining workspace text into the new repo.

    Bootstrap used to fold SOUL.md and IDENTITY.md into persona.md and drop EVERYTHING else on
    the floor — USER.md (who the agent works for), the whole memory/ directory, and every other
    workspace file — while reporting `"persona_seeded": true` and saying nothing about the loss.

    That is the difference between preserving an agent and preserving its voice, and preserving
    the agent is the entire reason someone picks bootstrap over create-agent. Measured on a real
    harvest: four months of daily memory, an agent's calendar conventions and its hard rule about
    who it may message were all dropped silently. One of those memory files even held the fix to
    a blocker the same agent had logged three times.

    Returns what was carried, so the caller can report it instead of implying it.
    """
    d = Path(snapshot_dir)
    carried = {"memory": [], "workspace_text": []}

    mem_src = d / "memory"
    if mem_src.is_dir():
        mem_dest = repo / "memory"
        mem_dest.mkdir(parents=True, exist_ok=True)
        for p in sorted(mem_src.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(mem_src)
            target = mem_dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():          # never clobber a factory file
                continue
            shutil.copy2(p, target)
            carried["memory"].append(str(rel))

    # Reference text (TOOLS/HEARTBEAT/BOOTSTRAP/MEMORY.md) lands under openclaw/ rather than the
    # repo root: it is the OpenClaw's own scaffolding, useful to read and wrong to present as if
    # a canopy agent authored it.
    ref = [fn for fn in WORKSPACE_TEXT if fn not in PERSONA_TEXT and (d / fn).is_file()]
    if ref:
        ref_dir = repo / "openclaw"
        ref_dir.mkdir(parents=True, exist_ok=True)
        for fn in ref:
            dest = ref_dir / fn
            if dest.exists():
                continue
            shutil.copy2(d / fn, dest)
            carried["workspace_text"].append(fn)
    return carried


def bootstrap_from_snapshot(
    inv: dict, *, slug: str, display_name: str, mandate: str, into: Path,
    mailbox: str = "", force: bool = False, stakeholders: str = "",
    git_init: bool = True,
) -> dict:
    """Scaffold a NEW canopy agent repo seeded from an OpenClaw snapshot: factory scaffold +
    seeded persona + carried memory/workspace text + ported skills.

    Returns {repo, ported_skills, scaffold_files, persona_seeded, carried, git_initialized}.
    """
    repo = Path(into).expanduser()
    spec_kwargs = {"slug": slug, "display_name": display_name, "mandate": mandate,
                   "mailbox": mailbox}
    if stakeholders:
        spec_kwargs["stakeholders"] = stakeholders
    spec = AgentSpec(**spec_kwargs)
    written = create_agent(spec, repo, force=force)
    _seed_persona(repo, inv)
    carried = _carry_workspace(Path(inv["snapshot_dir"]), repo)
    ported = [k for k in (_copy_skill_dir(s, repo) for s in inv["skills"]) if k]
    # Scan BEFORE the commit — a secret caught after `git init && git commit` is already in
    # history, and the filename excludes cannot see one that lives inside a file.
    secrets = scan_snapshot_for_secrets(Path(inv["snapshot_dir"]))
    initialized = False if secrets else (_git_init(repo, display_name) if git_init else False)
    return {
        "repo": str(repo),
        "ported_skills": ported,
        "scaffold_files": len(written),
        "persona_seeded": inv.get("has_persona", False),
        "persona_files": sorted(inv.get("persona", {})),
        "carried": carried,
        "git_initialized": initialized,
        "secret_findings": secrets,
    }


def _git_init(repo: Path, display_name: str) -> bool:
    """git init + one commit, matching what `canopy create-agent` does.

    create-agent inits behind a --git-init flag that defaults on; bootstrap called create_agent()
    directly and skipped that block entirely, so the harvest route handed you a directory that was
    not a repository — while the docs promised "an initialised git repo with one commit", and
    agent_doctor's checks assume a scaffolded repo has been inited.
    """
    if (repo / ".git").exists():
        return False
    subprocess.run(["git", "init", "-q"], cwd=repo, check=False)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=False)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"bootstrap {display_name} from OpenClaw snapshot"],
        cwd=repo, check=False,
    )
    return (repo / ".git").exists()


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
