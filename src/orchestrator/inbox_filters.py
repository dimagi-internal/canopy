"""Fleet inbox filters — the Gmail half of the fleet's inbound-email table.

The rules themselves live in ``orchestrator/inbox_rules.py`` (canopy#679): one table, one
row per kind of mail, each row WAKE or ARCHIVE. This module turns every archive row that
Gmail search can express into Gmail filters on any/all agent mailboxes, each one archiving,
marking read and labelling the mail with its row's name. Edit the table, not this file,
then re-run ``canopy email apply-filters --all``.

Two operations:
- ``apply_filters``   — create the row labels and the Gmail filters (affect FUTURE mail).
  Idempotent.
- ``sweep_existing``  — retroactively archive + mark-read + label mail already in the inbox
  that the filters would have caught (clears the backlog so it doesn't spawn turns).

A filter counts as present only when a live filter has the same query AND already adds the
row's label. A same-query filter without it (every filter from before the table) is
replaced: the new one is created first, then the old one deleted, so the mailbox is never
unfiltered in between.

**Editing a row's query? Add the OLD query to that row's ``supersedes``.** Filters are
matched by exact query string, so without it ``apply_filters`` installs the new rule and
leaves the old one live beside it — and Gmail applies both. ``supersedes`` makes the update
path an actual update (delete the old, create the new).
"""
from __future__ import annotations

import json
import subprocess

from orchestrator import inbox_rules


def _filters_from_table() -> list[dict]:
    """One filter per Gmail query of every archive row, named after the row
    (``<row>#2`` for a row's second query), labelled with the row name."""
    out: list[dict] = []
    for row in inbox_rules.TABLE:
        if row.bucket != inbox_rules.ARCHIVE:
            continue
        for i, query in enumerate(row.gmail):
            out.append({
                "name": row.name if i == 0 else f"{row.name}#{i + 1}",
                "row": row.name,
                "query": query,
                "archive": True, "mark_read": True,
                "add_label": row.name,
                "supersedes": list(row.supersedes) if i == 0 else [],
            })
    return out


#: Derived, never edited by hand — see ``inbox_rules.TABLE``.
FILTERS: list[dict] = _filters_from_table()


class FilterError(Exception):
    pass


def _existing_filters(mailbox: str, client: str, *,
                      runner=subprocess.run) -> list[tuple[str, str, frozenset]]:
    """Live filters on the mailbox as ``(filter_id, query, added_label_ids)``;
    ``filter_id`` may be ``""``.

    A query with no id still counts as PRESENT (so the rule is skipped, not re-created) but can
    never be deleted — the id is the only handle Gmail offers. Keeping the two concerns separate
    here is deliberate: a stricter read that dropped id-less entries would silently turn the
    idempotence check into "create everything, every run".

    Returns ``[]`` on any read failure, which makes ``apply_filters`` treat the mailbox as
    empty: it will try to create every rule, and a create that already exists is the harmless
    half of the failure. The dangerous half — deleting a superseded filter — is skipped
    entirely in that case, since we cannot tell a missing filter from an unreadable one.
    """
    try:
        r = runner(["gog", "gmail", "settings", "filters", "list",
                    "--account", mailbox, "--client", client, "--json"],
                   capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    try:
        items = json.loads(r.stdout or "{}").get("filters") or []
    except ValueError:
        return []
    out = []
    for f in items:
        q = (f.get("criteria") or {}).get("query")
        if q:
            added = frozenset((f.get("action") or {}).get("addLabelIds") or ())
            out.append((f.get("id") or "", q, added))
    return out


def _label_ids(mailbox: str, client: str, *, runner=subprocess.run) -> dict[str, str] | None:
    """``{label name: label id}`` for the mailbox's labels, or ``None`` if unreadable."""
    try:
        r = runner(["gog", "gmail", "labels", "list", "--account", mailbox,
                    "--client", client, "--json"], capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        labels = json.loads(r.stdout or "{}").get("labels") or []
    except ValueError:
        return None
    return {lb["name"]: lb.get("id") or lb["name"] for lb in labels if lb.get("name")}


def ensure_labels(mailbox: str, client: str, *, runner=subprocess.run,
                  dry_run: bool = False) -> tuple[dict[str, str], list[str]]:
    """Create any archive-row label the mailbox lacks. Returns ``(name→id, created)``.

    Labels must exist before a filter or a ``thread modify --add`` can name them — the runner
    falls back to skip-only when its labelled archive fails, so a mailbox without them quietly
    loses the archive half of the table."""
    ids = _label_ids(mailbox, client, runner=runner)
    if ids is None:
        raise FilterError(f"could not list labels on {mailbox}")
    created = []
    for name in inbox_rules.archive_labels():
        if name in ids:
            continue
        created.append(name)
        if dry_run:
            continue
        r = runner(["gog", "gmail", "labels", "create", name, "--account", mailbox,
                    "--client", client, "--no-input"], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise FilterError(f"label '{name}' on {mailbox}: {r.stderr.strip() or 'gog failed'}")
    if created and not dry_run:
        ids = _label_ids(mailbox, client, runner=runner) or ids
    return ids, created


def _delete_filter(mailbox: str, client: str, fid: str, what: str, *, runner) -> None:
    d = runner(["gog", "gmail", "settings", "filters", "delete", fid,
                "--account", mailbox, "--client", client, "--force", "--no-input"],
               capture_output=True, text=True, timeout=30)
    if d.returncode != 0:
        raise FilterError(f"{what} ({fid}) on {mailbox}: {d.stderr.strip() or 'gog delete failed'}")


def apply_filters(mailbox: str, client: str, *, runner=subprocess.run, dry_run: bool = False) -> dict:
    """Create the row labels and the FILTERS on one mailbox, idempotently.
    Gmail filters affect FUTURE mail only — pair with sweep_existing for the backlog."""
    label_ids, labels_created = ensure_labels(mailbox, client, runner=runner, dry_run=dry_run)
    live = _existing_filters(mailbox, client, runner=runner)
    applied, skipped, superseded, relabelled = [], [], [], []
    for flt in FILTERS:
        # Retire prior versions of THIS rule first, whether or not the current version needs
        # creating: a mailbox can already carry both (a hand-patched filter plus the stale one
        # it was meant to replace), and leaving the stale one live is the whole bug.
        for stale_q in flt.get("supersedes") or ():
            for fid, q, _added in live:
                if q != stale_q or not fid:
                    continue
                if not dry_run:
                    _delete_filter(mailbox, client, fid, f"superseded filter '{flt['name']}'",
                                   runner=runner)
                superseded.append(f"{flt['name']}:{fid}")
        same_query = [(fid, added) for fid, q, added in live if q == flt["query"]]
        want = label_ids.get(flt["add_label"])
        if any(want and want in added for _fid, added in same_query):
            skipped.append(flt["name"])
            continue
        cmd = ["gog", "gmail", "settings", "filters", "create",
               "--account", mailbox, "--client", client, "--query", flt["query"]]
        if flt.get("archive"):
            cmd.append("--archive")
        if flt.get("mark_read"):
            cmd.append("--mark-read")
        if flt.get("add_label"):
            cmd += ["--add-label", flt["add_label"]]
        if dry_run:
            cmd.append("--dry-run")
        r = runner(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise FilterError(f"filter '{flt['name']}' on {mailbox}: {r.stderr.strip() or 'gog failed'}")
        applied.append(flt["name"])
        # The same query without the row label: the pre-table filter this one replaces.
        # Deleted only AFTER its replacement exists, so the mailbox is never unfiltered.
        for fid, _added in same_query:
            if not fid:
                continue
            if not dry_run:
                _delete_filter(mailbox, client, fid, f"unlabelled filter '{flt['name']}'",
                               runner=runner)
            relabelled.append(f"{flt['name']}:{fid}")
    return {"applied": applied, "skipped": skipped, "superseded": superseded,
            "relabelled": relabelled, "labels_created": labels_created}


def sweep_existing(mailbox: str, client: str, *, runner=subprocess.run, dry_run: bool = False) -> dict:
    """Retroactively archive + mark-read + label mail ALREADY in the inbox that a filter
    would catch — so an existing junk backlog doesn't spawn turns when polling starts.
    Run ``apply_filters`` first: it creates the labels this names.

    Counts reflect threads whose modify actually SUCCEEDED, and each rule pages
    past gog's per-search result cap until its matches are drained. (Ada's
    2026-07-14 fleet sweep reported 184 'swept' messages that never moved: the
    modify used a nonexistent --remove-label flag, its result was discarded, and
    search matches were reported as swept. Never count what you didn't verify.)
    """
    swept = {}
    for flt in FILTERS:
        q = f'in:inbox ({flt["query"]})'
        done = 0
        for _page in range(20):  # safety bound: 20 pages × 50 = 1000 threads/rule/run
            r = runner(["gog", "gmail", "search", "--account", mailbox, "--client", client,
                        q, "--max", "50", "--json"], capture_output=True, text=True, timeout=45)
            if r.returncode != 0:
                raise FilterError(f"sweep search '{flt['name']}' on {mailbox}: {r.stderr.strip()}")
            try:
                threads = json.loads(r.stdout or "{}").get("threads") or []
            except ValueError:
                threads = []
            ids = [t["id"] for t in threads if t.get("id")]
            if not ids:
                break
            if dry_run:
                done += len(ids)
                break  # can't drain pages without modifying — report first page only
            failures = []
            for tid in ids:
                # one call archives, marks read AND labels the whole thread
                a = runner(["gog", "gmail", "thread", "modify", tid, "--remove=INBOX,UNREAD",
                            f"--add={flt['add_label']}",
                            "--account", mailbox, "--client", client, "--no-input"],
                           capture_output=True, text=True, timeout=45)
                if a.returncode == 0:
                    done += 1
                else:
                    failures.append(tid)
            if failures:
                # matched threads we couldn't modify would repeat forever — stop this rule
                # loudly rather than spin; partial success is still reported in the count.
                raise FilterError(
                    f"sweep '{flt['name']}' on {mailbox}: modify failed for "
                    f"{len(failures)}/{len(ids)} threads (e.g. {failures[0]})")
        swept[flt["name"]] = done
    return swept
