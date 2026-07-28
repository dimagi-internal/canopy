"""Catch a synthetic world that reads as generated rather than observed.

This was the single most-repeated finding across every judge round of the first
narrative to go through the loop, and it arrived from a different angle each
time:

  * eleven calendar rows all reading "214 children / 214 cartons"
  * outcome-capture ratios landing between 4.0% and 4.7% on all eleven rows
  * an exact duplicate pair — two different sites, both "310 served / 22 recorded"
  * batch and shipment identifiers cycling in a visible round-robin

None of that needs an LLM to see, and paying a judge round to find it is
expensive: each one cost a render, a judge dispatch, and an iteration. The
checks here are pure arithmetic over the captured page text or a JSON payload,
so they run in milliseconds and can gate a render rather than follow one.

The bar is deliberately narrow. Real data DOES repeat — a column of "0" for
eleven empty sites is honest, and a status column with three values is not a
finding. What is a finding is a column of *derived quantities* that a real
world would make lumpy, arriving suspiciously smooth.

    python -m scripts.ddd.data_fidelity <page_text.json|payload.json> [--json]
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# A column needs at least this many rows before uniformity means anything.
# Three identical values is a coincidence; eight is a generator.
MIN_ROWS = 6

# Ratios this tightly clustered across many rows do not occur naturally.
RATIO_SPREAD_FLOOR = 0.02  # 2 percentage points

_NUMBER = re.compile(r"^-?[\d,]+(?:\.\d+)?%?$")
_ID_TAIL = re.compile(r"^(?P<stem>.*?)(?P<num>\d+)(?P<suffix>[A-Za-z]?)$")


def _as_number(cell: str) -> float | None:
    cell = (cell or "").strip()
    if not _NUMBER.match(cell):
        return None
    try:
        return float(cell.rstrip("%").replace(",", ""))
    except ValueError:
        return None


def _tabular_rows(page_text: str) -> list[list[str]]:
    """Tab-separated rows, which is how the capture writes table content."""
    rows = []
    for line in (page_text or "").split("\n"):
        if "\t" in line:
            rows.append([c.strip() for c in line.split("\t")])
    return rows


def _columns(rows: list[list[str]]) -> dict[int, list[str]]:
    """Column index -> values, over the rows that share the modal width."""
    if not rows:
        return {}
    width = Counter(len(r) for r in rows).most_common(1)[0][0]
    body = [r for r in rows if len(r) == width]
    return {i: [r[i] for r in body] for i in range(width)}


def _identical_column(values: list[str]) -> bool:
    """Every row the same, and it is a quantity rather than a category."""
    distinct = {v for v in values if v}
    if len(distinct) != 1:
        return False
    only = next(iter(distinct))
    return _as_number(only) is not None and _as_number(only) != 0


def _duplicate_rows(rows: list[list[str]]) -> list[tuple[str, int]]:
    """Whole rows repeated apart from their leading label."""
    seen = Counter()
    for row in rows:
        if len(row) < 3:
            continue
        # Drop the first cell — two sites legitimately share a date; they should
        # not share every derived quantity behind it.
        tail = tuple(row[1:])
        if any(_as_number(c) is not None for c in tail):
            seen[tail] += 1
    return [("\t".join(k), n) for k, n in seen.items() if n > 1]


def _round_robin(values: list[str]) -> bool:
    """Identifiers cycling through a short repeating set."""
    stems = []
    for value in values:
        m = _ID_TAIL.match((value or "").strip())
        if not m or not m.group("stem"):
            return False
        stems.append(m.group("stem"))
    if len(stems) < MIN_ROWS:
        return False
    distinct = len(set(values))
    # A handful of ids repeating across many rows in a regular cycle.
    return 1 < distinct <= max(3, len(values) // 3)


def _ratio_cluster(rows: list[list[str]], columns: dict[int, list[str]]) -> list[dict]:
    """Pairs of numeric columns whose ratio is implausibly consistent."""
    findings = []
    numeric = {
        i: [_as_number(v) for v in vals]
        for i, vals in columns.items()
        if sum(1 for v in vals if _as_number(v) is not None) >= MIN_ROWS
    }
    keys = sorted(numeric)
    for a in keys:
        for b in keys:
            if a >= b:
                continue
            ratios = [
                nb / na
                for na, nb in zip(numeric[a], numeric[b])
                if na and nb is not None and na != 0
            ]
            if len(ratios) < MIN_ROWS:
                continue
            spread = max(ratios) - min(ratios)
            if spread < RATIO_SPREAD_FLOOR and max(ratios) > 0:
                findings.append(
                    {
                        "kind": "ratio_cluster",
                        "detail": (
                            f"columns {a} and {b} hold a near-constant ratio across "
                            f"{len(ratios)} rows ({min(ratios):.3f}–{max(ratios):.3f}). "
                            "Real capture rates are lumpy; a flat ratio reads as generated."
                        ),
                    }
                )
    return findings


def data_fidelity(page_text: str) -> dict:
    """Scan captured page text for the signatures of an authored world."""
    rows = _tabular_rows(page_text)
    columns = _columns(rows)
    findings: list[dict[str, Any]] = []

    for index, values in columns.items():
        if len(values) < MIN_ROWS:
            continue
        if _identical_column(values):
            findings.append(
                {
                    "kind": "identical_column",
                    "detail": (
                        f"column {index} reads '{values[0]}' on all {len(values)} rows. "
                        "Identical derived quantities repeated down a page are the "
                        "clearest signal a fixture was generated rather than observed."
                    ),
                }
            )
        elif _round_robin(values):
            findings.append(
                {
                    "kind": "round_robin_ids",
                    "detail": (
                        f"column {index} cycles through {len(set(values))} identifiers "
                        f"across {len(values)} rows in a visible repeat."
                    ),
                }
            )

    # Below the row floor nothing is a signal — three identical rows is a
    # coincidence, and a lens that cries wolf on a short table gets ignored.
    for tail, count in (_duplicate_rows(rows) if len(rows) >= MIN_ROWS else []):
        findings.append(
            {
                "kind": "duplicate_row",
                "detail": f"{count} rows share every value after the first cell: {tail[:90]}",
            }
        )

    findings.extend(_ratio_cluster(rows, columns))

    return {
        "rows_scanned": len(rows),
        "columns_scanned": len(columns),
        "findings": findings,
        "verdict": "pass" if not findings else "warn",
    }


def _load_page_text(path: Path) -> str:
    raw = json.loads(path.read_text())
    if isinstance(raw, dict) and "page_text" in raw:
        return raw["page_text"]
    return json.dumps(raw)


def _cli() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    target = Path(args[0])
    paths = sorted(target.glob("*_page_text.json")) if target.is_dir() else [target]

    all_findings = []
    for path in paths:
        result = data_fidelity(_load_page_text(path))
        for finding in result["findings"]:
            finding["source"] = path.name
            all_findings.append(finding)

    if "--json" in sys.argv:
        print(json.dumps({"findings": all_findings, "verdict": "pass" if not all_findings else "warn"}, indent=1))
    else:
        print(f"data-fidelity: {'pass' if not all_findings else 'warn'} ({len(all_findings)} findings)")
        for finding in all_findings:
            print(f"  [{finding['kind']}] {finding.get('source', '')}")
            print(f"      {finding['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
