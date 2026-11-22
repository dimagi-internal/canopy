"""A stored agent's definition: the manifest root and the file policy, client side.

A **stored agent** keeps its whole definition in a git repo canopy-web hosts, with revisions
(no GitHub repo); runners and laptops clone it (`agent_materialize.py`). Contract:
docs/architecture/stored-agents.md.

VENDORED. canopy-web's SDK ships the same two helpers as
``canopy_sdk.agent_definition.manifest_root`` / ``check_tree``; this repo does not depend
on the SDK, so a copy lives here. ``manifest_root`` MUST stay byte-for-byte the SDK's
serialization — canopy recomputes a checkout's ``root_sha256`` from its commit's tree and
reports it next to canopy-web's, so a drift here makes the two disagree.
`tests/test_agent_definition.py` pins it with a fixed vector.

``check_tree`` is a PRE-CHECK only (so `canopy agent definition edit|convert` can say what
will be refused before uploading); canopy-web enforces the policy on every write and is the
authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable, Mapping, Union

# ── manifest root (MUST match canopy_sdk.agent_definition.manifest_root) ────────────────


def file_entry(path: str, content: bytes, executable: bool = False) -> dict:
    """One manifest entry for ``content`` at ``path``."""
    return {"path": path, "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content), "executable": bool(executable)}


def manifest_entries(files: Union[Mapping, Iterable]) -> list[dict]:
    """The canonical manifest: ``{path, sha256, size, executable}`` entries sorted by path.

    ``files`` is either a revision ``tree`` (``{path: {sha256, size, executable}}``) or an
    iterable of entries (``{path, sha256, size, executable}``)."""
    if isinstance(files, Mapping):
        rows = [{"path": p, **{k: v[k] for k in ("sha256", "size", "executable")}}
                for p, v in files.items()]
    else:
        rows = [{k: e[k] for k in ("path", "sha256", "size", "executable")} for e in files]
    out = [{"path": str(r["path"]), "sha256": str(r["sha256"]).lower(),
            "size": int(r["size"]), "executable": bool(r["executable"])} for r in rows]
    return sorted(out, key=lambda r: r["path"])


def canonical_manifest(files) -> bytes:
    """The bytes the root hashes: ``json.dumps(entries, sort_keys=True, separators=(",",":"))``."""
    return json.dumps(manifest_entries(files), sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def manifest_root(files) -> str:
    """sha256 (hex) of the canonical manifest JSON of ``files``."""
    return hashlib.sha256(canonical_manifest(files)).hexdigest()


# ── file policy (pre-check; canopy-web is the authority) ─────────────────────────────────

MAX_FILES = 200
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024

#: Paths an edit may write (glob-ish; `**` = any depth, `<name>` = one segment).
EDITABLE = (
    "CLAUDE.md", "persona.md", "README.md",
    "skills/<name>/SKILL.md", "skills/<name>/**/*.{md,json,yaml,yml,txt}",
    "config/agent.json", "config/gating.json", "config/allowlist.txt",
    "tests/**/*.{json,yaml,yml,md,txt}",
    "docs/**/*.md",
)

_TEXT_EXT = ("md", "json", "yaml", "yml", "txt")
_FIXED_EDITABLE = {"CLAUDE.md", "persona.md", "README.md", "config/agent.json",
                   "config/gating.json", "config/allowlist.txt"}


def generated_paths(slug: str) -> tuple[str, ...]:
    """Paths canopy-web writes on every publish (an edit to one is refused)."""
    return (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
            ".claude/settings.json", f"bin/{slug}-email", ".gitignore")


# The same credential shapes canopy scans a harvested snapshot for (openclaw_harvest uses
# this list). High-signal, low-noise.
SECRET_PATTERNS = (
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


def secret_shape(text: str) -> str:
    """The label of the first credential shape in ``text``, or ""."""
    for label, pat in SECRET_PATTERNS:
        if pat.search(text):
            return label
    return ""


def path_problem(path: str) -> str:
    """Why ``path`` can never be in a tree (traversal, absolute, .git, …), or ""."""
    if not path:
        return "empty path"
    if "\x00" in path:
        return "NUL in path"
    if "\\" in path:
        return "backslash in path"
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        return "control character in path"
    if path.startswith("/"):
        return "absolute path"
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "'..', '.' or an empty segment in path"
    if ".git" in parts:
        return ".git/ is refused"
    if parts[0] == "hooks":
        return "hooks/ is refused (a stored agent registers no hooks)"
    return ""


def _is_editable(path: str) -> bool:
    if path in _FIXED_EDITABLE:
        return True
    parts = path.split("/")
    ext = path.rsplit(".", 1)[-1] if "." in parts[-1] else ""
    if parts[0] == "skills" and len(parts) >= 3:
        if len(parts) == 3 and parts[2] == "SKILL.md":
            return True
        return ext in _TEXT_EXT
    if parts[0] == "tests" and len(parts) >= 2:
        return ext in _TEXT_EXT
    if parts[0] == "docs" and len(parts) >= 2:
        return ext == "md"
    return False


def classify_path(path: str, slug: str, executable: bool = False) -> str:
    """``"editable"``, ``"generated"``, or ``"refused: <why>"`` for one path."""
    why = path_problem(path)
    if why:
        return f"refused: {why}"
    if path in generated_paths(slug):
        return "generated"
    if executable:
        return "refused: an executable (only bin/<slug>-email, which canopy-web writes)"
    if _is_editable(path):
        return "editable"
    return "refused: not an editable path (" + ", ".join(EDITABLE) + ")"


def check_tree(files: Mapping[str, Mapping], slug: str, *, allow_generated: bool = False) -> list[str]:
    """Problems with ``files`` under the stored-agent file policy; [] = passes the pre-check.

    ``files``: ``{path: {"content": bytes|str|None, "executable": bool, "symlink": bool}}``.
    ``content`` None means a deletion (only the path is checked). ``allow_generated``: the
    tree is a whole revision (server-generated paths are part of it), not an edit."""
    problems: list[str] = []
    live = {p: f for p, f in files.items() if (f or {}).get("content") is not None}
    if len(live) > MAX_FILES:
        problems.append(f"{len(live)} files (cap {MAX_FILES})")
    total = 0
    for path in sorted(files):
        f = files[path] or {}
        if f.get("symlink"):
            problems.append(f"{path}: symlinks are refused")
            continue
        kind = classify_path(path, slug, bool(f.get("executable")))
        if kind == "generated":
            if not allow_generated:
                problems.append(f"{path}: written by canopy-web on every publish; not editable")
                continue
        elif kind != "editable":
            problems.append(f"{path}: {kind}")
            continue
        content = f.get("content")
        if content is None:
            continue
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        total += len(data)
        if len(data) > MAX_FILE_BYTES:
            problems.append(f"{path}: {len(data)} bytes (cap {MAX_FILE_BYTES})")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{path}: not UTF-8")
            continue
        label = secret_shape(text)
        if label:
            problems.append(f"{path}: looks like it holds a secret ({label})")
    if total > MAX_TOTAL_BYTES:
        problems.append(f"{total} bytes in all (cap {MAX_TOTAL_BYTES})")
    return problems

