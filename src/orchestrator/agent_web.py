"""canopy-web agent-workspace client — the shared generalization of echo's bin/echo_canopy.py.

Lets ANY agent repo publish itself to canopy-web's `/api/agents/*` surface: register the agent,
mirror its skill catalog, post syncs (with self-grades), and push work products. Backs the
`canopy agent-publish` CLI. This is the "common" half of the §4a boundary — canopy owns the
client; the agent repo owns only its identity.

Identity is resolved from the agent repo itself (no per-agent client copy needed):
  - `.claude-plugin/plugin.json` → slug (`name`) + description
  - `config/agent.json` (optional) → email / persona / avatar_url / display name overrides
  - the git `origin` remote → the GitHub blob base for skill links

Auth: a canopy-web PAT via `CANOPY_WEB_PAT`, or `~/.claude/canopy/workbench-token`
(mint once via `/canopy:canopy-web-pat-mint`). Content is attributed to the agent slug, not the
PAT user. Stdlib only (urllib) — no `requests` dependency.

Transport, PAT/base-url resolution, and skill-frontmatter parsing are single-sourced in
:mod:`orchestrator.canopy_web` / :mod:`orchestrator.agent_client`; this module is the
repo-identity convenience layer over that shared core (resolve identity from the agent repo,
then call). It keeps its own thin ``base_url``/``token``/``_call`` shims (delegating to
``canopy_web``) for back-compat with callers like ``issue_origin`` and the ``agent-publish`` CLI.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
from pathlib import Path

from orchestrator import canopy_web
from orchestrator.agent_client import catalog_from_repo as _catalog_from_skills_root

# Back-compat aliases — the canonical values live in canopy_web now.
DEFAULT_BASE = canopy_web.DEFAULT_API
TOKEN_FILE = str(canopy_web.TOKEN_FILE)


class AgentWebError(Exception):
    """Bad agent identity, missing PAT, or a non-2xx canopy-web response."""


def base_url() -> str:
    return canopy_web.resolve_base_url(None)


def token() -> str:
    try:
        return canopy_web.resolve_token(None)
    except RuntimeError as e:
        raise AgentWebError(str(e))


def _call(path: str, body=None, method: str = "POST") -> dict:
    """Thin shim over :func:`canopy_web.call` (kept for back-compat callers).

    Maps the shared ``CanopyError`` and connection failures onto ``AgentWebError`` so existing
    ``except AgentWebError`` handlers (issue_origin, the agent-publish CLI) keep working.
    """
    try:
        return canopy_web.call(method, path, body)
    except canopy_web.CanopyError as e:
        raise AgentWebError(str(e))
    except urllib.error.URLError as e:
        raise AgentWebError(f"{method} {path} -> connection error: {e.reason}")
    except RuntimeError as e:  # missing PAT from resolve_token
        raise AgentWebError(str(e))


# ---- identity resolution (pure; testable without network) -----------------------------

def gh_blob_base(repo_dir: Path) -> str:
    """`https://github.com/<owner>/<repo>/blob/main/skills/{name}/SKILL.md` from the origin remote.

    Returns "" if there's no parseable GitHub origin. `{name}` is left as a literal placeholder
    for catalog_from_repo to fill per skill.
    """
    try:
        url = subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
            capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        url = ""
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?/?$", url)
    if not m:
        return ""
    return f"https://github.com/{m.group(1)}/{m.group(2)}/blob/main/skills/{{name}}/SKILL.md"


def resolve_identity(repo_dir: Path) -> dict:
    """Build the agent identity dict from the repo's plugin.json + optional config/agent.json."""
    repo = Path(repo_dir)
    pj = repo / ".claude-plugin" / "plugin.json"
    if not pj.exists():
        raise AgentWebError(
            f"no .claude-plugin/plugin.json in {repo} — run this from an agent repo root"
        )
    p = json.loads(pj.read_text())
    slug = p.get("name")
    if not slug:
        raise AgentWebError(f"{pj} has no `name`")
    ident = {
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "email": "",
        "description": p.get("description", ""),
        "persona": "",
        "avatar_url": "",
    }
    aj = repo / "config" / "agent.json"
    if aj.exists():
        ident.update({k: v for k, v in json.loads(aj.read_text()).items() if v})
    return ident


def catalog_from_repo(repo_dir: Path) -> list[dict]:
    """Mirror skills/*/SKILL.md into canopy-web's skill-catalog shape.

    Delegates the glob + frontmatter parse to the shared
    :func:`orchestrator.agent_client.catalog_from_repo`; this wrapper just supplies the repo's
    ``skills/`` root and the git-origin-derived URL template (empty template → empty URLs,
    matching the prior behaviour).
    """
    repo = Path(repo_dir)
    return _catalog_from_skills_root(repo / "skills", gh_blob_base(repo))


# ---- API operations -------------------------------------------------------------------

def register(repo_dir: Path) -> dict:
    ident = resolve_identity(repo_dir)
    return _call("/api/agents/", {
        "slug": ident["slug"], "name": ident["name"], "email": ident["email"],
        "description": ident["description"], "persona": ident["persona"],
        "avatar_url": ident["avatar_url"],
    })


def put_skills(repo_dir: Path) -> dict:
    ident = resolve_identity(repo_dir)
    items = catalog_from_repo(repo_dir)
    return _call(f"/api/agents/{ident['slug']}/skills/", {"skills": items}, method="PUT")


def post_sync(repo_dir: Path, *, doc_url: str, title: str, summary: str,
              grades: dict, period_start: str, period_end: str, source: str) -> dict:
    ident = resolve_identity(repo_dir)
    return _call(f"/api/agents/{ident['slug']}/syncs/", {
        "period_start": period_start, "period_end": period_end, "title": title,
        "summary": summary, "doc_url": doc_url, "self_grades": grades, "source": source,
    })


def push_work(repo_dir: Path, items: list[dict]) -> dict:
    ident = resolve_identity(repo_dir)
    return _call(f"/api/agents/{ident['slug']}/work-products/", {"work_products": items})


def push_items(repo_dir: Path, items: list[dict]) -> dict:
    """Post a review-items batch. Unlike push_work, the body is a BARE list (not wrapped)."""
    ident = resolve_identity(repo_dir)
    return _call(f"/api/agents/{ident['slug']}/items/", items)
