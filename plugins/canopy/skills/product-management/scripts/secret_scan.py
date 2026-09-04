"""Secret-leak scanner for the autonomous PM gate (spec §3a).

Reads diff text on stdin (typically `git diff --staged`). Exits 1 on any
hardcoded leak pattern, restricted filename, or unrelated debug leftover
in non-test source. Hardcoded patterns are NOT configurable.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

LEAK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]+")),
    ("GitHub token", re.compile(r"gh[ps]_[A-Za-z0-9]{36}")),
]

RESTRICTED_FILENAMES = re.compile(
    r"("
    r"\.env(\.[^/]+)?$"
    r"|.*\.key$"
    r"|.*\.pem$"
    r"|credentials\.json$"
    r"|gws-sa-key\.json$"
    r"|.*-secret\..*"
    r")",
    re.MULTILINE,
)

_DIFF_HEADER = re.compile(r"^(?:diff --git [ab]/\S+ [ab]/(\S+)|\+\+\+ b/(\S+))", re.MULTILINE)

DEBUG_PATTERNS = [
    re.compile(r"^\+.*\bprint\(", re.MULTILINE),
    re.compile(r"^\+.*\bconsole\.log\(", re.MULTILINE),
    re.compile(r"^\+.*\bbreakpoint\(\)", re.MULTILINE),
    re.compile(r"^\+.*\bdebugger;", re.MULTILINE),
]


def _split_by_file(diff: str) -> list[tuple[str, str]]:
    """Split a unified diff into (path, hunk_text) pairs, keyed by 'b/' path.

    Uses '+++ b/' when available; falls back to the 'b/' path from the
    'diff --git a/... b/...' header so that minimal diffs (no hunk headers)
    are still attributed to the correct file.
    """
    chunks: list[tuple[str, str]] = []
    current_path: str | None = None
    pending_git_path: str | None = None
    current_lines: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path is not None:
                chunks.append((current_path, "".join(current_lines)))
            elif pending_git_path is not None and current_lines:
                chunks.append((pending_git_path, "".join(current_lines)))
            current_path = None
            current_lines = [line]
            # Extract 'b/' path from "diff --git a/foo b/foo"
            m = re.match(r"diff --git \S+ b/(\S+)", line)
            pending_git_path = m.group(1) if m else None
        elif line.startswith("+++ b/"):
            current_path = line[len("+++ b/"):].rstrip("\n")
            pending_git_path = None
            current_lines.append(line)
        else:
            current_lines.append(line)
    # Flush last chunk
    if current_path is not None:
        chunks.append((current_path, "".join(current_lines)))
    elif pending_git_path is not None and current_lines:
        chunks.append((pending_git_path, "".join(current_lines)))
    return chunks


def _is_test_path(path: str) -> bool:
    parts = path.split("/")
    return any(p in ("tests", "test", "__tests__") or p.startswith("test_") for p in parts)


def scan(diff: str, env_values: list[str]) -> list[str]:
    failures: list[str] = []

    for label, pattern in LEAK_PATTERNS:
        if pattern.search(diff):
            failures.append(f"{label} pattern matched in diff")

    for m in _DIFF_HEADER.finditer(diff):
        path = m.group(1) or m.group(2) or ""
        if path and RESTRICTED_FILENAMES.search(path):
            failures.append("restricted filename present in diff")
            break

    for value in env_values:
        if value and value in diff:
            failures.append(".env value appears verbatim in diff")
            break  # one is enough

    for path, hunk in _split_by_file(diff):
        if _is_test_path(path):
            continue
        for pat in DEBUG_PATTERNS:
            if pat.search(hunk):
                failures.append(f"debug leftover in {path}")
                break

    return failures


def _read_env_values(env_path: Path) -> list[str]:
    if not env_path.exists():
        return []
    values: list[str] = []
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        _, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            values.append(value)
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=None)
    args = parser.parse_args()

    diff = sys.stdin.read()
    env_values = _read_env_values(args.env_file) if args.env_file else []
    failures = scan(diff, env_values)
    if failures:
        for f in failures:
            print(f"secret-scan: {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
