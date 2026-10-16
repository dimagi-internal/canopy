"""Publish a set of demos as one storyboard, with decision rows (canopy-web spec B).

    python -m scripts.ddd.demo_set publish <board.json>

`board.json` is the StoryboardIn shape: slug, title, lede, brief (audience,
purpose, data_disclosure, short_version), decisions[], acts[], capability.
The brief's old assumptions/divergences/open_questions lists are refused here:
say them as decisions[].
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from pathlib import Path

from scripts.ddd.feedback_round import _request, http_error_message

_LEGACY = ("assumptions", "divergences", "open_questions")


def publish(board: dict, **kw) -> dict:
    legacy = [k for k in _LEGACY if (board.get("brief") or {}).get(k)]
    if legacy:
        raise ValueError(f"brief.{', brief.'.join(legacy)}: send these as decisions[] rows instead")
    try:
        return _request("POST", "/api/storyboards/", board, **kw)
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise
    patch = {k: v for k, v in board.items() if k != "slug"}
    return _request("PATCH", f"/api/storyboards/{board['slug']}", patch, **kw)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="demo_set")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish")
    p.add_argument("path")
    args = ap.parse_args(argv)
    try:
        out = publish(json.loads(Path(args.path).read_text()))
    except urllib.error.HTTPError as e:
        print(f"demo_set: {http_error_message(e)}", file=sys.stderr)
        return 1
    print(json.dumps({"slug": out.get("slug"), "decisions": len(out.get("decisions") or [])}))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
