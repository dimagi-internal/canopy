"""Route a finished run's owned findings to their owners as GitHub issues.

Why
---
A DDD run used to write what it could not fix into ``.canopy/ddd/learnings.md``
— a file the owner of the fix never reads (and, across machines, the next run
did not either). A finding with an owner belongs in that owner's tracker, with
this run's evidence; a lesson about the PRODUCT belongs in the product's docs or
UI copy. So at the end of a run (``run_summary.finalize``):

a. **Out-of-scope floor findings with an owner** (``loop.fixed_surfaces`` entry
   or a decline naming owner/issue, DDD-1):
   * the surface names an issue → comment on it with this run's evidence;
   * else the owner's repo (``fixed_surfaces[].repo``) has an OPEN issue carrying
     this finding's marker ``<!-- ddd-finding:<narrative>/<finding_id> -->`` →
     comment on it;
   * else open one there (capped per run, :data:`DEFAULT_MAX_NEW`).
   An owner with no repo is listed in the summary only — never guessed.
b. **Product-clarity findings** (a judge tagged ``product_clarity``) → an issue
   in the PRODUCT repo (``loop.product_repo``, else the target repo's GitHub
   origin), labelled ``documentation``, asking for the doc or UI copy to make it
   unambiguous. Same marker dedupe, same cap.
c. DDD tooling problems are NOT filed (too noisy) — ``run_summary.canopy_tooling``.

``gh`` is optional. Missing, unauthenticated, ``CANOPY_DDD_ROUTE=off|dry``, or
under pytest → every filing is recorded as ``intended`` (what would have been
done, where) and nothing is sent. Every failure is recorded, never raised.

Idempotent per run: a finding already commented/opened for THIS run (the prior
routing record on ``state.run_summary``) is not filed again when the summary is
rebuilt.

The finding marker's key is ``<narrative>/<finding_id>``: DDD-1's finding ids
(``s1.visual_polish.1``) are stable across re-wordings within a narrative's
runs, not across narratives, so the narrative qualifies them.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_MAX_NEW = 3
CLARITY_LABEL = "documentation"
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_ISSUE = re.compile(r"^(?:([\w.-]+/[\w.-]+))?#(\d+)$")
_GH_REMOTE = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def marker_key(narrative: str | None, finding_id: str | None) -> str:
    return f"{narrative or 'ddd'}/{finding_id or 'unknown'}"


def marker(key: str) -> str:
    return f"<!-- ddd-finding:{key} -->"


# ---------------------------------------------------------------------------
# gh client (injectable)
# ---------------------------------------------------------------------------


class GhClient:
    """The few ``gh`` calls routing needs. Tests inject a fake with the same methods."""

    def __init__(self, gh: str = "gh", timeout: float = 30.0) -> None:
        self.gh = gh
        self.timeout = timeout
        self._issues: dict[str, list[dict]] = {}

    def _run(self, args: list[str], stdin: str | None = None) -> str:
        res = subprocess.run(
            [self.gh, *args], input=stdin, capture_output=True, text=True, timeout=self.timeout,
        )
        if res.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args[:3])} failed: {(res.stderr or res.stdout).strip()[:300]}")
        return res.stdout.strip()

    def available(self) -> tuple[bool, str]:
        if not shutil.which(self.gh):
            return False, "gh is not installed"
        try:
            self._run(["auth", "status"])
        except Exception as exc:  # noqa: BLE001
            return False, f"gh is not authenticated ({str(exc)[:160]})"
        return True, "gh ready"

    def find_open_issue(self, repo: str, mark: str) -> str | None:
        """URL of an open issue in *repo* whose body carries *mark*, or None."""
        import json

        if repo not in self._issues:
            out = self._run([
                "issue", "list", "-R", repo, "--state", "open", "--limit", "200",
                "--search", "ddd-finding in:body", "--json", "number,url,body",
            ])
            self._issues[repo] = json.loads(out or "[]")
        for issue in self._issues[repo]:
            if mark in (issue.get("body") or ""):
                return issue.get("url")
        return None

    def comment(self, issue_url: str, body: str) -> str:
        return self._run(["issue", "comment", issue_url, "--body-file", "-"], stdin=body) or issue_url

    def create(self, repo: str, title: str, body: str, labels: list[str]) -> str:
        args = ["issue", "create", "-R", repo, "--title", title, "--body-file", "-"]
        try:
            return self._run(args + [a for lab in labels for a in ("--label", lab)], stdin=body)
        except RuntimeError as exc:
            if not labels or "label" not in str(exc).lower():
                raise
            # A repo without the label: file it unlabelled rather than not at all.
            return self._run(args, stdin=body)


def _mode(client: Any) -> tuple[str, str]:
    env = os.environ.get("CANOPY_DDD_ROUTE", "").strip().lower()
    if env in ("off", "dry", "0", "false"):
        return "dry", f"CANOPY_DDD_ROUTE={env}"
    if client is None and os.environ.get("PYTEST_CURRENT_TEST") and env != "on":
        return "dry", "under pytest (set CANOPY_DDD_ROUTE=on with a client to exercise)"
    return "live", ""


# ---------------------------------------------------------------------------
# Where each finding goes
# ---------------------------------------------------------------------------


def _surface_attr(s: Any, k: str) -> Any:
    return s.get(k) if isinstance(s, dict) else getattr(s, k, None)


def owner_repo(row: dict, surfaces: Any) -> str | None:
    """``owner/repo`` for an out-of-scope finding: its configured surface's
    ``repo`` (matched by surface name, else by owner), else the repo its issue
    reference names."""
    for s in surfaces or ():
        repo = str(_surface_attr(s, "repo") or "").strip()
        if not repo:
            continue
        name = _surface_attr(s, "name") or _surface_attr(s, "match")
        if (row.get("surface") and row["surface"] in (name, _surface_attr(s, "match"))) or (
            row.get("owner") and row["owner"] == _surface_attr(s, "owner")
        ):
            return repo
    m = _ISSUE.match(str(row.get("issue") or "").strip())
    return m.group(1) if m and m.group(1) else None


def issue_ref(row: dict, repo: str | None) -> tuple[str, str] | None:
    """``(repo, number)`` when the finding already names its tracking issue."""
    m = _ISSUE.match(str(row.get("issue") or "").strip())
    if not m:
        return None
    r = m.group(1) or repo
    return (r, m.group(2)) if r else None


def product_repo(cfg: Any, repo_root: str | Path | None, *, _git=None) -> str | None:
    """``loop.product_repo``, else the target repo's GitHub ``origin``."""
    loop = getattr(cfg, "loop", cfg)
    configured = str(getattr(loop, "product_repo", None) or "").strip()
    if configured and _REPO.match(configured):
        return configured
    if repo_root is None:
        return None
    try:
        url = (_git or _origin_url)(Path(repo_root))
    except Exception:  # noqa: BLE001
        return None
    m = _GH_REMOTE.search(str(url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _origin_url(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


def _run_link(summary: dict) -> str:
    try:
        from scripts.ddd.upload import run_package_url

        return run_package_url(summary.get("narrative_slug") or "", summary.get("run_id") or "")
    except Exception:  # noqa: BLE001 — no workspace/base url: name the run instead
        return f"run `{summary.get('run_id')}` (no canopy-web link available)"


def _evidence(summary: dict, row: dict, link: str, key: str) -> str:
    scene = row.get("scene") if row.get("scene") is not None else "?"
    lines = [
        f"DDD run `{summary.get('run_id')}` (narrative `{summary.get('narrative_slug')}`) was held down by this.",
        "",
        f"- finding `{row.get('finding_id')}` — scene {scene}, `{row.get('dimension')}`",
    ]
    if row.get("fix"):
        lines.append(f"- what the judge asked for: {row['fix']}")
    if row.get("ambiguity"):
        lines.append(f"- what was ambiguous: {row['ambiguity']}")
    if row.get("reason"):
        lines.append(f"- why the loop could not fix it: {row['reason']}")
    if row.get("cells"):
        lines.append(f"- pinned cells: {', '.join(row['cells'])}")
    ending = summary.get("ending") or {}
    lines += [f"- run ended `{ending.get('action')}` ({ending.get('terminal_status')})", f"- run: {link}", "",
              marker(key) + f" <!-- ddd-run:{summary.get('run_id')} -->"]
    return "\n".join(lines) + "\n"


def _title(kind: str, summary: dict, row: dict) -> str:
    what = (row.get("ambiguity") if kind == "product_clarity" else row.get("fix")) or row.get("dimension") or "finding"
    what = " ".join(str(what).split())
    what = what if len(what) <= 90 else what[:89] + "…"
    if kind == "product_clarity":
        return f"Docs/UI copy: make this unambiguous — {what}"
    return f"DDD {summary.get('narrative_slug')}: {what}"


def _clarity_body(summary: dict, row: dict, link: str, key: str) -> str:
    head = (
        "A DDD run's judges could not tell what the product does here from the product itself "
        "(or the narrative had to invent the behaviour). Please make the product documentation "
        "or the UI copy unambiguous — that fixes it for every reader, not just the demo loop.\n\n"
    )
    return head + _evidence(summary, row, link, key)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def plan(summary: dict, *, cfg: Any = None, repo_root: str | Path | None = None, _git=None) -> tuple[list[dict], list[dict]]:
    """``(intents, unrouted)`` — what would be filed where, before any call."""
    loop = getattr(cfg, "loop", cfg)
    surfaces = getattr(loop, "surfaces", None) or ()
    narrative = summary.get("narrative_slug")
    intents: list[dict] = []
    unrouted: list[dict] = []
    for row in summary.get("out_of_scope") or []:
        if not (row.get("owner") or row.get("issue") or row.get("surface")):
            unrouted.append({"kind": "out_of_scope", "finding_id": row.get("finding_id"),
                             "why": "no owner recorded — configure loop.fixed_surfaces with owner + repo"})
            continue
        repo = owner_repo(row, surfaces)
        ref = issue_ref(row, repo)
        key = marker_key(narrative, row.get("finding_id"))
        if ref:
            intents.append({"kind": "out_of_scope", "row": row, "key": key, "repo": ref[0],
                            "issue": f"{ref[0]}#{ref[1]}", "action": "comment"})
        elif repo:
            intents.append({"kind": "out_of_scope", "row": row, "key": key, "repo": repo,
                            "issue": None, "action": "comment_or_open"})
        else:
            unrouted.append({"kind": "out_of_scope", "finding_id": row.get("finding_id"), "owner": row.get("owner"),
                             "why": f"owner {row.get('owner') or row.get('surface')!r} has no repo — add `repo:` "
                                    "to its loop.fixed_surfaces entry"})
    clarity = summary.get("product_clarity") or []
    prepo = product_repo(cfg, repo_root, _git=_git) if clarity else None
    for row in clarity:
        key = marker_key(narrative, row.get("finding_id"))
        if prepo:
            intents.append({"kind": "product_clarity", "row": row, "key": key, "repo": prepo,
                            "issue": None, "action": "comment_or_open"})
        else:
            unrouted.append({"kind": "product_clarity", "finding_id": row.get("finding_id"),
                             "why": "no product repo — set loop.product_repo (owner/repo)"})
    return intents, unrouted


def route(
    summary: dict,
    *,
    cfg: Any = None,
    client: Any = None,
    repo_root: str | Path | None = None,
    prior: dict | None = None,
    max_new: int | None = None,
    _git=None,
) -> dict:
    """File (or record the intent to file) every owned finding; never raises."""
    loop = getattr(cfg, "loop", cfg)
    configured = getattr(loop, "max_new_issues", None)
    cap = int(max_new if max_new is not None else (DEFAULT_MAX_NEW if configured is None else configured))
    intents, unrouted = plan(summary, cfg=cfg, repo_root=repo_root, _git=_git)
    mode, why = _mode(client)
    if mode == "live":
        client = client or GhClient()
        try:
            ok, why = client.available()
        except Exception as exc:  # noqa: BLE001
            ok, why = False, str(exc)[:200]
        if not ok:
            mode = "dry"
    done = {
        r.get("key"): r for r in (prior or {}).get("results") or []
        if r.get("status") in ("commented", "opened") and r.get("key")
    }
    link = _run_link(summary) if intents else ""
    results: list[dict] = []
    opened = sum(1 for r in done.values() if r.get("status") == "opened")
    for it in intents:
        row = it["row"]
        base = {"kind": it["kind"], "finding_id": row.get("finding_id"), "key": it["key"],
                "repo": it["repo"], "issue": it["issue"]}
        if it["key"] in done:
            results.append({**done[it["key"]], "why": "already filed for this run"})
            continue
        body = (_clarity_body if it["kind"] == "product_clarity" else _evidence)(summary, row, link, it["key"])
        if mode != "live":
            results.append({**base, "status": "intended", "action": it["action"], "why": why})
            continue
        try:
            target = None
            if it["action"] == "comment":
                repo, num = it["issue"].split("#", 1)
                target = f"https://github.com/{repo}/issues/{num}"
            else:
                target = client.find_open_issue(it["repo"], marker(it["key"]))
            if target:
                url = client.comment(target, body)
                results.append({**base, "status": "commented", "url": url or target})
                continue
            if opened >= cap:
                results.append({**base, "status": "capped", "action": "open",
                                "why": f"new-issue cap of {cap} per run reached"})
                continue
            labels = [CLARITY_LABEL] if it["kind"] == "product_clarity" else []
            url = client.create(it["repo"], _title(it["kind"], summary, row), body, labels)
            opened += 1
            results.append({**base, "status": "opened", "url": url})
        except Exception as exc:  # noqa: BLE001 — one failed filing never stops the rest
            results.append({**base, "status": "error", "why": " ".join(str(exc).split())[:300]})
    return {"mode": mode, "why": why, "cap": cap, "results": results, "unrouted": unrouted}
