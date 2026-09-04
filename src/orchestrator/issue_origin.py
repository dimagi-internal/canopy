"""canopy.origin/v1 — provenance for architect-routed GitHub issues.

The split (per the operating model): a ROUTE'd GitHub issue stays CLEAN and portable (mandate +
done-criteria + a pointer). The rich *understanding* — who/why/when produced it, the generalized
intent it serves, the evidence, and POINTERS to the sessions the architect drilled — lives in a
structured record. canopy-web is that record's home (queryable, portable); a local copy is the
working source of truth on the machine. **Session transcripts are NEVER stored here** — only path
pointers; you can recover the raw transcript with `canopy harvest strip <path>` ONLY on a machine
where that session exists. Web = the understanding; local = the evidence.

Keyed by the GitHub issue itself (`<owner/repo>#<number>`). Stdlib + PyYAML only.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

SCHEMA = "canopy.origin/v1"


def _store_dir() -> Path:
    base = Path(os.path.expanduser("~")) / ".claude" / "canopy" / "issues"
    return base


def _repo_slug(repo: str) -> str:
    return repo.replace("/", "__")


def record_path(repo: str, number: int) -> Path:
    return _store_dir() / _repo_slug(repo) / f"{number}.yaml"


def build_record(
    *,
    repo: str,
    source: str = "hal-architect",
    agent: str = "hal",
    skill: str = "architect",
    initiative: str,
    ledger: str = "",
    created: str,
    disposition: str = "route",
    confidence: str = "medium",
    mandate: str = "",
    done_when: str = "",
    intent: str = "",
    evidence: list[dict] | None = None,
    sessions_scanned: int = 0,
    cross_user: bool = False,
    drilled: list[str] | None = None,
    number: int | None = None,
) -> dict[str, Any]:
    """Assemble a canopy.origin/v1 record. `drilled`/`evidence[].session` are PATH POINTERS only."""
    rec: dict[str, Any] = {
        "schema": SCHEMA,
        "issue": f"{repo}#{number}" if number is not None else None,
        "repo": repo,
        "number": number,
        "source": source,
        "agent": agent,
        "skill": skill,
        "initiative": initiative,
        "ledger": ledger,
        "created": created,
        "disposition": disposition,
        "confidence": confidence,
        "mandate": mandate.strip(),
        "done_when": done_when.strip(),
        "intent": intent.strip(),
        "evidence": evidence or [],
        "corpus": {
            "sessions_scanned": sessions_scanned,
            "cross_user": cross_user,
            "drilled": drilled or [],
        },
    }
    return rec


def clean_issue_body(rec: dict) -> str:
    """The PORTABLE GitHub issue body — no local paths, no yaml dump. Mandate + done + pointer."""
    lines = [rec.get("mandate", "").strip()]
    if rec.get("done_when"):
        lines += ["", f"**Done when:** {rec['done_when'].strip()}"]
    ref = rec.get("issue") or f"{rec.get('repo','')}#<n>"
    created = rec.get("created", "")
    initiative = rec.get("initiative", "")
    lines += [
        "",
        "---",
        f"_Filed by Hal's architect pass ({initiative}, {created}). "
        f"Full context (evidence, intent, the sessions it read): `canopy issue context {ref}`._",
    ]
    return "\n".join(lines).strip() + "\n"


def save_local(rec: dict) -> Path:
    p = record_path(rec["repo"], rec["number"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(rec, sort_keys=False, width=100), encoding="utf-8")
    return p


def load_local(repo: str, number: int) -> dict | None:
    p = record_path(repo, number)
    if not p.is_file():
        return None
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def find_existing_issue_number(repo: str, title: str) -> int | None:
    """Dedup: any locally-recorded issue for this repo whose stored title matches (case-insensitive)."""
    d = _store_dir() / _repo_slug(repo)
    if not d.is_dir():
        return None
    want = title.strip().lower()
    for f in d.glob("*.yaml"):
        try:
            rec = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (rec or {}).get("title", "").strip().lower() == want:
            return rec.get("number")
    return None


def render_context(rec: dict) -> str:
    """Human + agent-facing hydration: the understanding + how to recover the local evidence."""
    out: list[str] = []
    out.append(f"# {rec.get('issue')}  ·  {rec.get('disposition','').upper()}  ·  confidence: {rec.get('confidence')}")
    out.append(f"source: {rec.get('source')}  ·  initiative: {rec.get('initiative')}  ·  filed: {rec.get('created')}")
    if rec.get("ledger"):
        out.append(f"intent ledger (the whole why): {rec['ledger']}")
    out.append("")
    out.append("## Mandate")
    out.append(rec.get("mandate", ""))
    if rec.get("done_when"):
        out.append(f"\n**Done when:** {rec['done_when']}")
    out.append("")
    out.append("## Intent it serves")
    out.append(rec.get("intent", "(none recorded)"))
    ev = rec.get("evidence") or []
    if ev:
        out.append("\n## Evidence (claim → session)")
        for e in ev:
            out.append(f"- {e.get('claim','')}  ←  {e.get('session','')}")
    corpus = rec.get("corpus") or {}
    drilled = corpus.get("drilled") or []
    out.append("\n## Recover the evidence locally (transcripts are NOT in the web record)")
    out.append(f"whole arc: `canopy harvest map {rec.get('initiative')} --full`")
    if drilled:
        out.append("drilled sessions — re-read in full:")
        for path in drilled:
            here = "✓ local" if os.path.exists(path) else "✗ NOT on this machine"
            out.append(f"  [{here}] canopy harvest strip \"{path}\"")
    return "\n".join(out)


# Fields the canopy-web /api/issues OriginIssueIn schema accepts (it rejects extras like schema/issue).
_WEB_FIELDS = (
    "repo", "number", "title", "source", "agent", "skill", "initiative", "ledger", "created",
    "disposition", "confidence", "mandate", "done_when", "intent", "evidence", "corpus",
)


def _web_payload(rec: dict) -> dict:
    return {k: rec[k] for k in _WEB_FIELDS if k in rec and rec[k] is not None}


def _web_path(repo: str, number: int) -> str:
    return f"/api/issues/{_repo_slug(repo)}/{number}/"


def web_sync(rec: dict) -> tuple[bool, str]:
    """Best-effort POST (idempotent upsert) of the record to canopy-web's /api/issues/. Degrades
    gracefully — the local copy is canonical if canopy-web is unreachable. Never raises."""
    try:
        from orchestrator import canopy_web
        canopy_web.call("POST", "/api/issues/", _web_payload(rec))
        return True, "synced to canopy-web"
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        return False, f"not synced ({type(exc).__name__}); local record is canonical"


def web_fetch(repo: str, number: int) -> dict | None:
    """Fetch a record from canopy-web (fallback when no local copy). Returns None on any failure."""
    try:
        from orchestrator import canopy_web
        return canopy_web.call("GET", _web_path(repo, number))
    except Exception:  # noqa: BLE001
        return None


def web_delete(repo: str, number: int) -> tuple[bool, str]:
    """Best-effort DELETE of the canopy-web record (cleanup). Never raises."""
    try:
        from orchestrator import canopy_web
        canopy_web.call("DELETE", _web_path(repo, number))
        return True, "deleted from canopy-web"
    except Exception as exc:  # noqa: BLE001
        return False, f"canopy-web delete skipped ({type(exc).__name__})"


def delete_local(repo: str, number: int) -> bool:
    p = record_path(repo, number)
    if p.is_file():
        p.unlink()
        return True
    return False
