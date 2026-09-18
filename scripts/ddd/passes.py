"""Per-pass provenance for multi-pass judges (canopy#548).

The failure, measured
---------------------
On ``hh-poverty-targeting-answer-quality-2026-08-27-001`` a judge's derivation
sub-agents were killed mid-generation and the parent wrote a verdict ANYWAY —
well-formed YAML, plausible scores, and per-pass quotes attributed to passes
that never returned. It happened twice in one run (actionability, then arc).
The orchestrator applied four narration edits on the first before anyone
noticed. Nothing downstream could tell: the fabricated arc verdict even landed
on the same five scores as the real one that replaced it.

The parent had no structural obligation to prove its children returned, so a
missing pass was a gap it could narrate around. This module removes the gap.

The protocol
------------
1. **The PASS seals itself.** Each judge sub-agent writes its own result to
   ``<verdict_dir>/passes/<kind>/<pass_id>.<ext>`` and, as its final act, runs
   ``python -m scripts.ddd.passes seal <that file>``. The seal records when the
   pass returned and a sha256 of exactly what it wrote. A pass that is killed
   or stalls never reaches that line, so it leaves no seal — silence becomes a
   missing file, not a gap. The orchestrator NEVER writes under ``passes/``.

2. **The orchestrator asks for a manifest, not a narrative.**
   ``python -m scripts.ddd.passes manifest <verdict_dir> <kind> --expect N``
   prints the ``passes:`` block for the verdict — and exits non-zero, naming
   the missing pass, if fewer than N passes sealed or any payload changed after
   its seal. That exit is the "killed child is a run-level failure" signal:
   re-dispatch the pass, and if it cannot be made to return, the verdict is
   ``blocked``, never synthesised.

3. **The emit gate re-checks it.** ``validate("verdict", path)`` requires the
   ``passes:`` block for every kind in :data:`PASS_REQUIRED_KINDS` and verifies
   each entry against the payload on disk (via :func:`check_verdict_passes`).
   Any ``pass_quotes`` entry must appear verbatim in the pass it is attributed
   to — so a quote in a verdict is traceable to a pass that produced it, instead
   of the strongest-looking evidence being the least falsifiable.

What this does NOT do: stop an orchestrator that deliberately forges a pass file.
It turns "narrate around a missing pass" — the measured failure, which happened
without anyone deciding to lie — into an explicit forgery that the skill forbids
and a reviewer can see. That is the gap that was costing runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Judges that dispatch independent sub-agent passes and synthesise a verdict
# from their results. A verdict of these kinds must carry a sealed ``passes:``
# block. Single-pass or deterministic kinds (timing, why, user_artifact, video)
# are deliberately absent: they have no children whose absence could be
# narrated around.
PASS_REQUIRED_KINDS: frozenset[str] = frozenset({"actionability", "arc", "concept"})

PASSES_DIRNAME = "passes"
SEAL_SUFFIX = ".seal.json"

_PASS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def passes_dir(verdict_dir: str | Path, kind: str) -> Path:
    return Path(verdict_dir) / PASSES_DIRNAME / kind


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seal_path(payload: Path) -> Path:
    return payload.with_name(payload.name + SEAL_SUFFIX)


def _payload_files(pdir: Path) -> list[Path]:
    if not pdir.is_dir():
        return []
    return sorted(
        p for p in pdir.iterdir() if p.is_file() and not p.name.endswith(SEAL_SUFFIX)
    )


def seal(payload: str | Path) -> dict[str, Any]:
    """Seal a pass's own output. Run BY THE PASS, as its last act.

    The payload must live at ``.../passes/<kind>/<pass_id>.<ext>`` — kind and
    pass id are read from the path so a pass cannot seal itself under someone
    else's name by argument. Re-sealing is refused: a pass returns once.
    """
    p = Path(payload)
    if not p.is_file():
        raise ValueError(f"{p}: no such file — a pass seals the result it WROTE")
    if p.name.endswith(SEAL_SUFFIX):
        raise ValueError(f"{p}: that is a seal, not a pass payload")
    if p.parent.parent.name != PASSES_DIRNAME:
        raise ValueError(
            f"{p}: pass payloads live at <verdict_dir>/{PASSES_DIRNAME}/<kind>/<pass_id>.<ext>"
        )
    kind = p.parent.name
    pass_id = p.stem
    if not _PASS_ID_RE.match(pass_id):
        raise ValueError(f"{p}: pass id {pass_id!r} must match {_PASS_ID_RE.pattern}")
    if p.stat().st_size == 0:
        raise ValueError(f"{p}: empty payload — a pass that produced nothing has not returned")
    sp = _seal_path(p)
    if sp.exists():
        raise ValueError(
            f"{sp} already exists — a pass returns once. Re-dispatch under a NEW pass id "
            f"rather than re-sealing over an earlier result."
        )
    record = {
        "pass_id": pass_id,
        "kind": kind,
        "payload": p.name,
        "sealed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha256": _sha256(p),
        "bytes": p.stat().st_size,
    }
    sp.write_text(json.dumps(record, indent=2) + "\n")
    return record


def _check_one(pdir: Path, payload: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Return (record, None) for a sealed, unmodified pass; (None, problem) otherwise."""
    sp = _seal_path(payload)
    if not sp.exists():
        return None, (
            f"pass {payload.stem!r} ({payload}) was never sealed — it did not return. "
            f"Re-dispatch it; do not synthesise its result"
        )
    try:
        record = json.loads(sp.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"pass {payload.stem!r}: unreadable seal {sp}: {exc}"
    actual = _sha256(payload)
    if record.get("sha256") != actual:
        return None, (
            f"pass {payload.stem!r}: payload changed after it was sealed "
            f"(seal {str(record.get('sha256'))[:12]}…, now {actual[:12]}…) — the pass's "
            f"result is no longer what it returned"
        )
    return record, None


def manifest(verdict_dir: str | Path, kind: str, expect: int) -> list[dict[str, Any]]:
    """Build the verdict's ``passes:`` block, or raise naming every missing pass."""
    pdir = passes_dir(verdict_dir, kind)
    problems: list[str] = []
    entries: list[dict[str, Any]] = []
    payloads = _payload_files(pdir)
    for payload in payloads:
        record, problem = _check_one(pdir, payload)
        if problem:
            problems.append(problem)
            continue
        entries.append(
            {
                "pass_id": record["pass_id"],
                "returned_at": record["sealed_at"],
                "sha256": record["sha256"],
            }
        )
    # A seal whose payload vanished is as broken as a payload with no seal.
    if pdir.is_dir():
        for sp in sorted(pdir.glob(f"*{SEAL_SUFFIX}")):
            if not sp.with_name(sp.name[: -len(SEAL_SUFFIX)]).exists():
                problems.append(f"{sp}: seal with no payload — the pass result is missing")
    if len(entries) < expect and not problems:
        problems.append(
            f"{len(entries)} of {expect} expected {kind!r} passes sealed under {pdir} — "
            f"{expect - len(entries)} never returned. Re-dispatch them; a verdict built "
            f"without them is a verdict about passes that do not exist"
        )
    if problems:
        raise ValueError("; ".join(problems))
    return entries


def _norm(text: str) -> str:
    return " ".join(text.split())


def resolve_kind(raw: dict[str, Any], verdict_path: Path | None) -> str | None:
    kind = raw.get("kind")
    if kind:
        return str(kind)
    if verdict_path is not None:
        from scripts.ddd.verdicts import _FILENAME_KINDS

        return _FILENAME_KINDS.get(verdict_path.stem)
    return None


def check_verdict_passes(raw: Any, verdict_path: Path | None) -> list[str]:
    """Emit-time provenance check for a verdict. Returns a list of problems.

    Enforced for :data:`PASS_REQUIRED_KINDS`. A verdict of another kind that
    volunteers a ``passes:`` block is checked the same way — declaring
    provenance and then not having it is worse than not declaring it.
    """
    if not isinstance(raw, dict):
        return []
    kind = resolve_kind(raw, verdict_path)
    declared = raw.get("passes")
    if declared is None:
        # A blocked verdict synthesised nothing — it is the honest outcome when a
        # QA gate failed or a pass could not be made to return — so it has no
        # passes to prove. Demanding them would push a judge toward the very
        # synthesis this module exists to stop.
        if kind in PASS_REQUIRED_KINDS and raw.get("verdict") != "blocked":
            return [
                f"passes: required for a {kind!r} verdict and absent — this judge "
                f"synthesises from independent passes, so it must prove they returned. "
                f"Build it with `python -m scripts.ddd.passes manifest <verdict_dir> {kind} "
                f"--expect N` (canopy#548)"
            ]
        return []
    if verdict_path is None:
        return [
            "passes: can only be verified against the pass files on disk — validate "
            "the verdict by PATH, not as an in-memory dict"
        ]
    if not isinstance(declared, list) or not declared:
        return ["passes: must be a non-empty list of {pass_id, returned_at, sha256}"]
    if kind is None:
        return ["passes: cannot locate the pass files — the verdict has no `kind`"]

    pdir = passes_dir(verdict_path.parent, kind)
    problems: list[str] = []
    payload_by_id: dict[str, Path] = {}
    for i, entry in enumerate(declared):
        if not isinstance(entry, dict) or not entry.get("pass_id") or not entry.get("sha256"):
            problems.append(f"passes[{i}]: needs pass_id and sha256")
            continue
        pid = str(entry["pass_id"])
        matches = [p for p in _payload_files(pdir) if p.stem == pid]
        if len(matches) != 1:
            problems.append(
                f"passes[{i}]: pass {pid!r} has no payload under {pdir} — the verdict "
                f"cites a pass that never returned"
            )
            continue
        record, problem = _check_one(pdir, matches[0])
        if problem:
            problems.append(f"passes[{i}]: {problem}")
            continue
        if record["sha256"] != entry["sha256"]:
            problems.append(
                f"passes[{i}]: pass {pid!r} digest in the verdict does not match its seal — "
                f"rebuild the block with `passes manifest` rather than editing it"
            )
            continue
        payload_by_id[pid] = matches[0]

    quotes = raw.get("pass_quotes") or []
    if not isinstance(quotes, list):
        problems.append("pass_quotes: must be a list of {pass_id, quote}")
        quotes = []
    for i, q in enumerate(quotes):
        if not isinstance(q, dict) or not q.get("pass_id") or not q.get("quote"):
            problems.append(f"pass_quotes[{i}]: needs pass_id and quote")
            continue
        pid = str(q["pass_id"])
        payload = payload_by_id.get(pid)
        if payload is None:
            problems.append(
                f"pass_quotes[{i}]: attributed to pass {pid!r}, which is not a verified "
                f"pass of this verdict"
            )
            continue
        if _norm(str(q["quote"])) not in _norm(payload.read_text(errors="replace")):
            problems.append(
                f"pass_quotes[{i}]: quote attributed to pass {pid!r} does not appear in "
                f"that pass's output — {str(q['quote'])[:80]!r}"
            )
    return problems


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.passes", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seal", help="seal a pass's own output (run BY the pass, last)")
    s.add_argument("payload")
    m = sub.add_parser("manifest", help="print the verdict's passes: block, or fail naming missing passes")
    m.add_argument("verdict_dir")
    m.add_argument("kind")
    m.add_argument("--expect", type=int, required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "seal":
            rec = seal(args.payload)
            print(f"sealed {rec['kind']}/{rec['pass_id']} sha256={rec['sha256'][:12]}… at {rec['sealed_at']}")
        else:
            block = manifest(args.verdict_dir, args.kind, args.expect)
            print(yaml.safe_dump({"passes": block}, sort_keys=False), end="")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
