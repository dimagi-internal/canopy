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
from pathlib import Path

from scripts.ddd.auth import (
    require_write_workspace as _require_write_ws,
    resolve_base_url as _resolve_base_url,
    resolve_ddd_workspace as _resolve_ws,
    resolve_token as _resolve_token,
    scoped_api_path as _scoped,
)
from scripts.ddd.review import _json_request

STATES = ("answered", "declined")


class UnansweredNotes(ValueError):
    pass


def _request(method: str, path: str, body: dict | None = None, *, workspace=None, base_url=None, token=None):
    # Writes must name a workspace (auth convention); reads fall back to the active one.
    ws = _require_write_ws(workspace) if method != "GET" else _resolve_ws(workspace)
    url = f"{_resolve_base_url(base_url)}{_scoped(path, ws)}"
    return _json_request(method, url, _resolve_token(token), body)


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
    from scripts.ddd import run_store

    doc = run_store.pull(run_id)
    if doc is None:
        return
    state = dict(doc.get("state") or {})
    state.setdefault("run_id", run_id)
    state.setdefault("feedback_rounds", []).append(
        {"storyboard": slug, "round": number, "feedback_ids": feedback_ids})
    run_store.push(state, base_version=doc.get("state_version"))


def apply(slug: str, number: int, dispositions: dict[str, dict], *, version: int, run_id: str, **kw) -> dict:
    round_doc = read(slug, number, **kw)
    missing_or_raise(round_doc, dispositions)
    ids = [n["id"] for n in round_doc["notes"]]
    for nid in ids:
        d = dispositions[str(nid)]
        body = {"state": d["state"], "note": d["note"].strip()}
        if d["state"] == "answered":
            body["resolved_in_version"] = version
        _request("POST", f"/api/feedback/{nid}/resolve", body, **kw)
    _record_on_run_doc(run_id, slug=slug, number=number, feedback_ids=ids)
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
        doc = read(args.slug, args.number)
        doc["clusters"] = cluster(doc.get("notes") or [])
        text = json.dumps(doc, indent=2)
        Path(args.out).write_text(text) if args.out else print(text)
        return 0
    try:
        out = apply(args.slug, args.number, json.loads(Path(args.dispositions).read_text()),
                    version=args.version, run_id=args.run_id)
    except UnansweredNotes as e:
        print(f"feedback_round: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
