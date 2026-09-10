"""Run pytest and parse junit-xml results, with optional reruns for flake detection."""
from __future__ import annotations

import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TestResult:
    __test__ = False  # don't let pytest try to collect this dataclass
    nodeid: str
    status: str  # passed | failed | error | skipped | unknown
    duration_ms: int = 0
    error: str | None = None
    flake_count: int = 0  # number of runs whose status differed from majority


def _normalize_nodeid(classname: str, name: str) -> str:
    """Junit puts class as `tests.test_foo` or `tests.test_foo.TestClass`.
    Pytest junit emits classname with dots; convert back to nodeid form.

    Strips the `[param]` suffix that pytest emits for parametrized tests so
    the result matches the AST collector's nodeid (which has no suffix).
    """
    # Drop pytest parametrize suffix: 'test_x[a-1-True]' -> 'test_x'.
    bracket = name.find("[")
    if bracket != -1:
        name = name[:bracket]
    # classname like 'tests.test_foo' -> 'tests/test_foo.py'
    # classname like 'tests.test_foo.TestBar' -> 'tests/test_foo.py::TestBar'
    parts = classname.split(".")
    # find the last test_* part — anything after is a class
    file_parts: list[str] = []
    class_parts: list[str] = []
    for i, p in enumerate(parts):
        if p.startswith("test_") or p.endswith("_test"):
            file_parts = parts[: i + 1]
            class_parts = parts[i + 1 :]
            break
    if not file_parts:
        file_parts = parts
    file_path = "/".join(file_parts) + ".py"
    suffix = "::".join(class_parts + [name]) if class_parts else name
    return f"{file_path}::{suffix}"


# Status priority for parametrize aggregation: any worse status wins.
_STATUS_RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}


def _parse_junit(xml_path: Path) -> dict[str, TestResult]:
    if not xml_path.exists():
        return {}
    out: dict[str, TestResult] = {}
    tree = ET.parse(xml_path)
    root = tree.getroot()
    # junit may be <testsuites><testsuite>... or just <testsuite>
    suites = root.findall(".//testsuite") or [root]
    for suite in suites:
        for case in suite.findall("testcase"):
            classname = case.get("classname", "")
            name = case.get("name", "")
            time_s = float(case.get("time", "0") or 0)
            nodeid = _normalize_nodeid(classname, name)
            status = "passed"
            err: str | None = None
            if case.find("failure") is not None:
                status = "failed"
                err = (case.find("failure").get("message") or "")[:500]
            elif case.find("error") is not None:
                status = "error"
                err = (case.find("error").get("message") or "")[:500]
            elif case.find("skipped") is not None:
                status = "skipped"
            duration_ms = int(time_s * 1000)
            existing = out.get(nodeid)
            if existing is None:
                out[nodeid] = TestResult(
                    nodeid=nodeid, status=status,
                    duration_ms=duration_ms, error=err,
                )
            else:
                # Aggregate parametrize results into one nodeid: worst status wins,
                # durations sum, first error message kept.
                if _STATUS_RANK[status] > _STATUS_RANK[existing.status]:
                    existing.status = status
                    existing.error = err or existing.error
                existing.duration_ms += duration_ms
    return out


def run_pytest(repo: Path, reruns: int = 0, extra_args: list[str] | None = None,
               timeout: int = 600) -> dict[str, TestResult]:
    """Run pytest in `repo`, parse junit XML, return nodeid -> TestResult.

    If reruns > 0, run the suite (1 + reruns) times and tally flake_count
    against the majority status per nodeid.

    pytest's exit code is intentionally ignored — failures are normal data here.
    """
    runs: list[dict[str, TestResult]] = []
    extra = extra_args or []

    for _ in range(1 + reruns):
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
            xml_path = Path(tmp.name)
        try:
            cmd = [
                "pytest",
                f"--junit-xml={xml_path}",
                "--quiet",
                "-p", "no:cacheprovider",
                *extra,
            ]
            proc = None
            try:
                proc = subprocess.run(
                    cmd, cwd=repo, capture_output=True, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                # Partial junit may still exist; carry on.
                pass
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "test-audit could not run pytest: no `pytest` on PATH. Activate the "
                    "project's virtualenv, or collect with --no-run."
                ) from exc

            # A suite that cannot even start writes no JUnit XML, and parsing an
            # empty file raises `ParseError: no element found` — an error that
            # names neither pytest nor the environment. Say what happened, and
            # point at the flag that gets the audit moving without a runner.
            if not xml_path.exists() or xml_path.stat().st_size == 0:
                detail = ""
                if proc is not None:
                    tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
                    if tail:
                        detail = "\n  " + "\n  ".join(tail[-15:])
                raise RuntimeError(
                    "test-audit ran pytest but it produced no JUnit XML, so the suite "
                    "never started (a missing settings module, an unset env var, or a "
                    "collection error). Re-run with --no-run to audit statically, or fix "
                    "the invocation so `pytest` works from the repo root."
                    + (f"\n\npytest said:{detail}" if detail else "")
                )
            runs.append(_parse_junit(xml_path))
        finally:
            try:
                xml_path.unlink()
            except OSError:
                pass

    if not runs:
        return {}
    if len(runs) == 1:
        return runs[0]

    # Combine: majority status wins; flake_count = runs that disagreed.
    nodeids = set().union(*(r.keys() for r in runs))
    out: dict[str, TestResult] = {}
    for nid in nodeids:
        statuses = [r.get(nid).status for r in runs if nid in r]
        if not statuses:
            continue
        majority, _ = Counter(statuses).most_common(1)[0]
        flake = sum(1 for s in statuses if s != majority)
        # Take the first run that matches majority for duration/err.
        rep = next(r[nid] for r in runs if nid in r and r[nid].status == majority)
        rep.flake_count = flake
        out[nid] = rep
    return out
