"""A narrative's iteration sources on canopy-web — the files it takes to keep
iterating a narrative, kept where every machine can reach them.

canopy-web holds the STORY of a narrative version (narration, personas,
why-brief) and its rendered videos. Re-recording or re-cutting it also takes:

- ``recipe``           — the recipe (``style``, ``cuts``, actions, urls, setup);
- ``intent``           — the intent ledger beside it, when there is one;
- ``master``           — the master recording every cut is rendered from;
- ``capture_report``   — the recorder's ``--report`` JSON (scene timings);
- ``capture_manifest`` — the recorder's ``--manifest`` JSON.

These used to live only where the render ran — a ``mktemp -d`` work dir, a
scratchpad, an uncommitted recipe — so a narrative could be published that no
other machine could iterate (the chlorine demo, 2026-10-14: its recipe and
master were lost with one machine's temp dir). Now every narrative post and
every video upload pins them to the version on canopy-web
(``POST /api/ddd/narratives/<slug>/sources/``), and ``narrative pull`` restores
them. Anything local is a cache that can be pulled again.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from orchestrator.provenance import provenance_headers
from scripts.ddd.auth import (
    require_write_workspace,
    resolve_base_url,
    resolve_ddd_workspace,
    resolve_token,
    scoped_api_path,
)

ROLES = ("recipe", "intent", "master", "capture_report", "capture_manifest")

# role -> where a pull writes it. The recipe and intent ledger go beside the
# narrative lock, where the loop reads them; the capture files are a re-cut's
# inputs, kept under the user's canopy dir so a pull never drops a large mp4
# into a git checkout.
_SPEC_DIR_FILES = {"recipe": "{slug}.recipe.yaml", "intent": "{slug}.intent.yaml"}
_CAPTURE_FILES = {
    "master": "master.mp4",
    "capture_report": "report.json",
    "capture_manifest": "manifest.json",
}

_CT = {
    "recipe": "application/yaml",
    "intent": "application/yaml",
    "master": "video/mp4",
    "capture_report": "application/json",
    "capture_manifest": "application/json",
}


class SourcesError(RuntimeError):
    """A source could not be pushed or pulled."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RecipeLockMismatch(SourcesError):
    """A recipe that does not compose with the narrative version it would be
    pinned to (canopy#908). ``reason`` is :func:`spec_io.compose`'s own message,
    which names the orphan / missing scene ids."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


def compose_error(recipe_path: str | Path, lock: dict) -> str | None:
    """Why the recipe at ``recipe_path`` does not compose with ``lock`` — the
    :class:`spec_io.SpecCompositionError` message naming the scenes that do not
    match — or ``None`` when it composes. Pure apart from reading the file.

    This is the real test of "renderable": the loop renders ``compose(recipe,
    lock)``, so a recipe and lock that disagree on the scene list render nothing
    no matter that both files exist."""
    import yaml

    from scripts.ddd.spec_io import SpecCompositionError, compose

    try:
        recipe = yaml.safe_load(Path(recipe_path).read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        return f"recipe {Path(recipe_path).name} could not be read: {exc}"
    if not isinstance(recipe, dict):
        return f"recipe {Path(recipe_path).name} is not a mapping"
    try:
        compose(recipe, lock)
    except SpecCompositionError as exc:
        return str(exc)
    return None


def version_lock(review_id: str, *, base_url: str | None = None,
                 token: str | None = None) -> dict:
    """The story of narrative version ``review_id`` in lock shape (the same
    ``scenes`` ``narrative pull`` writes to ``<slug>.narrative.lock.json``)."""
    from scripts.ddd import review as rv
    from scripts.ddd.narrative import web_narrative_to_spec_parts

    full = rv.get_review(review_id, base_url=base_url, token=token)
    request_json = full.get("request_json") if isinstance(full, dict) else None
    if not isinstance(request_json, dict):
        raise SourcesError(
            f"could not read narrative version {review_id} from canopy-web to check "
            f"the recipe against its story"
        )
    return web_narrative_to_spec_parts(request_json)


def capture_dir(slug: str) -> Path:
    """Where a pull puts a narrative's capture files (master, report, manifest)."""
    root = os.environ.get("CANOPY_DDD_SOURCES_DIR") or str(Path.home() / ".canopy" / "ddd" / "sources")
    return Path(root) / slug


def recipe_files(spec_path: str | Path, slug: str | None = None) -> dict[str, Path]:
    """The recipe-side files for a spec path: the file itself as ``recipe`` and,
    when it exists beside it, ``<slug>.intent.yaml`` as ``intent``.

    The spec path is the recipe (``<slug>.recipe.yaml``) or a legacy single-file
    spec (``<slug>.yaml``); either is what the loop renders from, so either is
    what gets kept."""
    p = Path(spec_path)
    if not p.is_file():
        raise SourcesError(f"spec {p} does not exist")
    slug = slug or p.name.split(".")[0]
    out = {"recipe": p}
    intent = p.parent / f"{slug}.intent.yaml"
    if intent.is_file():
        out["intent"] = intent
    return out


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _multipart(fields: dict[str, str], filename: str, content_type: str, data: bytes):
    boundary = "----canopysource" + os.urandom(9).hex()
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"",
            str(value).encode("utf-8"),
        ]
    parts += [
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
        f"Content-Type: {content_type}".encode(),
        b"",
    ]
    body = crlf.join(parts) + crlf + data + crlf + f"--{boundary}--".encode() + crlf
    return body, f"multipart/form-data; boundary={boundary}"


def _default_post(url: str, token: str, fields: dict, filename: str, content_type: str,
                  data: bytes) -> dict:
    body, ctype = _multipart(fields, filename, content_type, data)
    req = urllib.request.Request(url, data=body, method="POST", headers={
        **provenance_headers(),
        "Content-Type": ctype,
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as exc:
        raise SourcesError(
            f"canopy-web refused the {fields.get('role')} source (HTTP {exc.code}): "
            f"{exc.read().decode('utf-8', 'replace')[:500]}"
        ) from exc
    raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def _default_get(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, method="GET", headers={
        **provenance_headers(), "Authorization": f"Bearer {token}",
    })
    try:
        return urllib.request.urlopen(req, timeout=600).read()
    except urllib.error.HTTPError as exc:
        raise SourcesError(f"GET {url} failed (HTTP {exc.code})", exc.code) from exc


def _sources_path(slug: str) -> str:
    return f"/api/ddd/narratives/{urllib.parse.quote(slug, safe='')}/sources/"


def push_sources(
    slug: str,
    review_id: str,
    files: dict[str, Path],
    *,
    base_url: str | None = None,
    token: str | None = None,
    lock: dict | None = None,
    _post=None,
    _fetch_lock=None,
) -> dict[str, dict]:
    """Pin each ``{role: path}`` to narrative version ``review_id``. Returns the
    server's row per role (``reused`` = identical bytes were already there).

    A ``recipe`` is composed against that version's story first — ``lock``
    when the caller already has it (``narrative post`` does: it just posted
    it), else fetched from canopy-web — and refused with the mismatching scene
    ids (:class:`RecipeLockMismatch`) before anything is posted. A recipe that
    does not compose with its own version renders nothing, and pinning it hands
    the mismatch to the next machine that pulls (canopy#908: a recipe with two
    priming scenes the story lacked, pinned to chlorine v4).

    Raises on the first failure: a version whose sources did not all land is
    exactly the state this exists to prevent, so the caller must not go on to
    publish a video for it."""
    unknown = set(files) - set(ROLES)
    if unknown:
        raise SourcesError(f"unknown source role(s): {', '.join(sorted(unknown))}")
    if files.get("recipe") is not None:
        if lock is None:
            lock = (_fetch_lock or version_lock)(review_id, base_url=base_url, token=token)
        reason = compose_error(files["recipe"], lock)
        if reason:
            raise RecipeLockMismatch(
                f"refusing to pin {Path(files['recipe']).name} to {slug!r} version "
                f"{review_id}: it does not compose with that version's story — {reason}",
                reason,
            )
    api = resolve_base_url(base_url)
    pat = resolve_token(token)
    ws = require_write_workspace()
    url = f"{api}{scoped_api_path(_sources_path(slug), ws)}"
    post = _post or _default_post
    out: dict[str, dict] = {}
    for role in ROLES:  # a stable order: the recipe first
        path = files.get(role)
        if path is None:
            continue
        data = Path(path).read_bytes()
        body = post(url, pat, {"role": role, "narrative_review_id": review_id},
                    Path(path).name, _CT[role], data)
        if body.get("sha256") != _sha256(data):
            raise SourcesError(
                f"{role} source for {slug!r} did not land intact "
                f"(sent {_sha256(data)}, canopy-web has {body.get('sha256')})"
            )
        out[role] = body
    return out


def list_sources(slug: str, *, base_url: str | None = None, token: str | None = None,
                 _get=None) -> list[dict]:
    """Every source of ``slug`` the caller can read, newest first."""
    api = resolve_base_url(base_url)
    pat = resolve_token(token)
    url = f"{api}{scoped_api_path(_sources_path(slug), resolve_ddd_workspace(None))}"
    try:
        raw = (_get or _default_get)(url, pat)
    except SourcesError as exc:
        # A canopy-web without the sources route (or that cannot see the
        # narrative) has nothing to restore; the caller reports what is missing.
        if exc.status == 404:
            return []
        raise
    return json.loads(raw.decode("utf-8") or "[]")


def newest_per_role(rows: list[dict], review_id: str | None = None) -> dict[str, dict]:
    """The newest row per role — from version ``review_id`` when it has one,
    else the newest any version holds (a new version re-pins its recipe on post,
    but its master is still the last one recorded)."""
    out: dict[str, dict] = {}
    if review_id:
        for r in rows:
            if str(r.get("review_id")) == str(review_id):
                out.setdefault(r["role"], r)
    for r in rows:
        out.setdefault(r["role"], r)
    return out


def pull_sources(
    slug: str,
    spec_dir: str | Path,
    *,
    review_id: str | None = None,
    roles: tuple[str, ...] = ROLES,
    base_url: str | None = None,
    token: str | None = None,
    _get=None,
) -> dict[str, Path]:
    """Restore ``slug``'s sources: recipe + intent into ``spec_dir`` (beside the
    lock), the capture files into :func:`capture_dir`. Verifies every file's
    sha256. Returns ``{role: written path}``; a role canopy-web has no file for
    is simply absent."""
    get = _get or _default_get
    picked = newest_per_role(list_sources(slug, base_url=base_url, token=token, _get=get), review_id)
    if not picked:
        return {}
    api = resolve_base_url(base_url)
    pat = resolve_token(token)
    written: dict[str, Path] = {}
    for role in roles:
        row = picked.get(role)
        if row is None:
            continue
        data = get(f"{api}{row['content_url']}", pat)
        if _sha256(data) != row["sha256"]:
            raise SourcesError(f"{role} source for {slug!r} failed its sha256 check")
        if role in _SPEC_DIR_FILES:
            dest = Path(spec_dir) / _SPEC_DIR_FILES[role].format(slug=slug)
        else:
            dest = capture_dir(slug) / _CAPTURE_FILES[role]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        written[role] = dest
    return written
