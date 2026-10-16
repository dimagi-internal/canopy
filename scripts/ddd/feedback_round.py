"""Consume a closed storyboard feedback round (canopy-web spec C, step 2-5).

    python -m scripts.ddd.feedback_round read  <slug> <n> [--out round.json]
    python -m scripts.ddd.feedback_round apply <slug> <n> --dispositions d.json --version N --run-id R

`apply` refuses — before writing anything — unless EVERY note in the round has
a disposition with a state (answered|declined) and words. That is the
"every comment gets an answer" rule, enforced rather than hoped for.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from pathlib import Path

from orchestrator.canopy_web import pinned_api_path as _pinned
from scripts.ddd.auth import (
    require_write_workspace as _require_write_ws,
    resolve_base_url as _resolve_base_url,
    resolve_ddd_workspace as _resolve_ws,
    resolve_token as _resolve_token,
)
from scripts.ddd.review import _json_request

STATES = ("answered", "declined")


class UnansweredNotes(ValueError):
    pass


class UnknownRun(ValueError):
    pass


def _request(method: str, path: str, body: dict | None = None, *, workspace=None, base_url=None, token=None):
    # Writes must name a workspace (auth convention); reads fall back to the active one.
    ws = _require_write_ws(workspace) if method != "GET" else _resolve_ws(workspace)
    url = f"{_resolve_base_url(base_url)}{_pinned(path, ws or '')}"
    return _json_request(method, url, _resolve_token(token), body)


def http_error_message(e: urllib.error.HTTPError) -> str:
    """``<code> <detail>`` from a problem+json body (falls back to the reason)."""
    detail = ""
    try:
        body = json.loads(e.read().decode("utf-8"))
        detail = body.get("detail") or body.get("title") or json.dumps(body)
    except Exception:
        detail = str(e.reason or "")
    return f"{e.code} {detail}".strip()


def read(slug: str, number: int, **kw) -> dict:
    return _request("GET", f"/api/storyboards/{slug}/rounds/{number}", **kw)


def cluster(notes: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for n in notes:
        out.setdefault(f"{n.get('target_ref', '')}#{n.get('anchor_id') or '*'}", []).append(n)
    return dict(sorted(out.items()))


def missing_dispositions(round_doc: dict, dispositions: dict[str, dict]) -> list[int]:
    missing = []
    for n in round_doc.get("notes") or []:
        d = dispositions.get(str(n["id"])) or {}
        if d.get("state") not in STATES or not (d.get("note") or "").strip():
            missing.append(n["id"])
    return missing


def missing_or_raise(round_doc: dict, dispositions: dict[str, dict]) -> None:
    missing = missing_dispositions(round_doc, dispositions)
    if missing:
        raise UnansweredNotes("every note needs a disposition (answered|declined) and a sentence; "
                              f"missing: {', '.join(map(str, missing))}")


def _record_on_run_doc(run_id: str, *, slug: str, number: int, feedback_ids: list[int]) -> None:
    """Record the round on the run doc, idempotently (one entry per (storyboard, round))."""
    from scripts.ddd import run_store

    doc = run_store.pull(run_id)
    if doc is None:
        raise UnknownRun(f"run {run_id!r} not found; nothing was written")
    state = dict(doc.get("state") or {})
    state.setdefault("run_id", run_id)
    entry = {"storyboard": slug, "round": number, "feedback_ids": feedback_ids}
    rounds = [r for r in state.get("feedback_rounds") or []
              if not (r.get("storyboard") == slug and r.get("round") == number)]
    state["feedback_rounds"] = rounds + [entry]
    run_store.push(state, base_version=doc.get("state_version"))


def apply(slug: str, number: int, dispositions: dict[str, dict], *, version: int, run_id: str, **kw) -> dict:
    round_doc = read(slug, number, **kw)
    missing_or_raise(round_doc, dispositions)
    ids = [n["id"] for n in round_doc["notes"]]
    # Record first (validates the run exists, idempotent): a failure after this point
    # leaves a retry-safe state, since resolve and the record are both re-runnable.
    _record_on_run_doc(run_id, slug=slug, number=number, feedback_ids=ids)
    for nid in ids:
        d = dispositions[str(nid)]
        body = {"state": d["state"], "note": d["note"].strip()}
        if d["state"] == "answered":
            body["resolved_in_version"] = version
        _request("POST", f"/api/feedback/{nid}/resolve", body, **kw)
    nxt = _request("POST", f"/api/storyboards/{slug}/rounds/{number}/consume", {"run_id": run_id}, **kw)
    return {"resolved": len(ids), "next_round": nxt.get("number")}


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="feedback_round")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read")
    r.add_argument("slug")
    r.add_argument("number", type=int)
    r.add_argument("--out")
    a = sub.add_parser("apply")
    a.add_argument("slug")
    a.add_argument("number", type=int)
    a.add_argument("--dispositions", required=True)
    a.add_argument("--version", type=int, required=True)
    a.add_argument("--run-id", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "read":
        try:
            doc = read(args.slug, args.number)
        except urllib.error.HTTPError as e:
            print(f"feedback_round: {http_error_message(e)}", file=sys.stderr)
            return 1
        doc["clusters"] = cluster(doc.get("notes") or [])
        text = json.dumps(doc, indent=2)
        if args.out:
            Path(args.out).write_text(text)
        else:
            print(text)
        return 0
    try:
        out = apply(args.slug, args.number, json.loads(Path(args.dispositions).read_text()),
                    version=args.version, run_id=args.run_id)
    except (UnansweredNotes, UnknownRun) as e:
        print(f"feedback_round: {e}", file=sys.stderr)
        return 2
    except urllib.error.HTTPError as e:
        print(f"feedback_round: {http_error_message(e)}", file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
