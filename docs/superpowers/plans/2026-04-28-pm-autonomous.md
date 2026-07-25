# Autonomous mode for canopy:product-management — Implementation Plan

> **Status: shipped — `/canopy:pm-autonomous(-loop)` live. Historical record, not current-state (PM state later moved to `<repo>/.canopy/pm/`).**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an autonomous mode to the `canopy:product-management` skill — single command runs a full working-backwards sprint (scout → email draft → ship → email-and-stop) with a multi-layer convince-self-it's-clean gate, no per-proposal approval. Existing `/canopy:pm-scout` human-gated path must keep working unchanged.

**Architecture:** The skill stays project-agnostic. Project-specific shipping/test/email config lives in `.claude/pm/autonomous.yaml` per consumer project. Two new commands (`/canopy:pm-autonomous`, `/canopy:pm-autonomous-loop`) plus four new template files extend the existing skill; deterministic mechanical checks (secret-leak scan, diff-size cap, config validation) are Python scripts shipped under the skill, with pytest coverage. The first adopter (`ace-web`) writes its `autonomous.yaml` in a separate, out-of-scope PR.

**Tech Stack:** Markdown (skills + commands), Python 3.11 stdlib + PyYAML (canopy already depends on it), pytest. No new runtime deps.

---

## Source spec

`docs/superpowers/specs/2026-04-28-pm-autonomous-design.md`. The plan implements §1–§6 and the acceptance criteria. Re-read it before starting; this plan does not duplicate prose the spec already nails.

## File structure

**New files:**
- `plugins/canopy/commands/pm-autonomous.md` — single-sprint command
- `plugins/canopy/commands/pm-autonomous-loop.md` — looping wrapper command
- `plugins/canopy/skills/product-management/templates/autonomous/cycle.md` — Phases A–E procedure (the heart of the skill)
- `plugins/canopy/skills/product-management/templates/autonomous/config-schema.md` — `autonomous.yaml` schema doc + example
- `plugins/canopy/skills/product-management/templates/autonomous/convince-self-gate.md` — the gate procedure (3a–3d)
- `plugins/canopy/skills/product-management/templates/autonomous/email-format.md` — email body template
- `plugins/canopy/skills/product-management/scripts/__init__.py` — empty (pkg marker, lets pytest import scripts as a module)
- `plugins/canopy/skills/product-management/scripts/secret_scan.py` — secret-leak scanner (stdin: diff text, exit 0=clean, 1=leak)
- `plugins/canopy/skills/product-management/scripts/diff_size_check.py` — diff-size cap (stdin: `git diff --stat` output, exit 0/1)
- `plugins/canopy/skills/product-management/scripts/validate_autonomous_config.py` — Phase 0 config validator (CLI: `python validate_autonomous_config.py <path>`)
- `tests/skills/test_pm_autonomous_secret_scan.py` — secret-scan tests
- `tests/skills/test_pm_autonomous_diff_size.py` — diff-size tests
- `tests/skills/test_pm_autonomous_config.py` — config validator tests
- `tests/skills/test_pm_autonomous_skill_structure.py` — skill+command structural tests
- `tests/skills/test_pm_scout_regression.py` — anchors the unchanged human-gated flow

**Modified files:**
- `plugins/canopy/skills/product-management/SKILL.md` — adds an "Autonomous mode" section and links to the new templates; existing content preserved
- `plugins/canopy/.claude-plugin/plugin.json` — patch bump (currently `0.2.50` → `0.2.51`)
- `VERSION` — same bump

**Out of scope (separate ace-web PR):**
- `ace-web/.claude/pm/autonomous.yaml` — concrete first adopter config (per spec constraint)

## Resolved open questions (from spec §"Open questions for implementation")

1. **`/loop` long-tail timeout default** — 24h, hardcoded in the loop command's instructions. Not exposed as config in v1; revisit only if a second project complains.
2. **"Keep going" detection** — any user message resumes the loop UNLESS the message body matches (case-insensitive, whole-word) `^(stop|pause|halt)\b`. Documented in `pm-autonomous-loop.md`.
3. **Canopy PR opening** — keep the existing `Self-Improvement Protocol` exactly as-is (clone `jjackson/canopy` to a temp dir per PR). Reusing it makes the autonomous canopy-PR flow identical to the human flow already in the skill — no new mechanism to test, and no shared mutable directory to corrupt.
4. **Email rendering of screenshots** — the autonomous skill writes the email body in markdown referencing screenshot files at relative paths under `.claude/pm/sent-emails/<slug>/screenshots/`, then invokes the configured `sender_skill` with `subject`, `body_markdown`, and an explicit `attachments` list of absolute screenshot paths. The sender skill is responsible for handling attachments. If `ace:email-communicator` does not currently support attachments, the autonomous skill notes the limitation in the run log and sends body-only (recipient can browse the repo to see screenshots). That follow-up belongs in the ace-web adoption PR, not this one.

---

## Task 1: Secret-leak scanner script + tests

**Files:**
- Create: `plugins/canopy/skills/product-management/scripts/__init__.py` (empty)
- Create: `plugins/canopy/skills/product-management/scripts/secret_scan.py`
- Create: `tests/skills/test_pm_autonomous_secret_scan.py`

**What it does:** Reads diff text from stdin (the output of `git diff --staged` typically). Exits 1 with a human-readable message if any of the hardcoded leak patterns match, plus checks any value present in a `.env` file in the repo root (passed via `--env-file` flag). Exits 0 otherwise. Hardcoded patterns are NOT configurable per spec §3a.

- [ ] **Step 1: Write failing tests**

```python
# tests/skills/test_pm_autonomous_secret_scan.py
"""Tests for the secret-leak scanner used by the autonomous PM gate."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent.parent
    / "plugins"
    / "canopy"
    / "skills"
    / "product-management"
    / "scripts"
    / "secret_scan.py"
)


def _run(stdin: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_diff_passes() -> None:
    diff = "+def hello():\n+    return 'world'\n"
    result = _run(diff)
    assert result.returncode == 0, result.stderr


def test_aws_access_key_blocks() -> None:
    diff = "+aws_key = 'AKIAIOSFODNN7EXAMPLE'\n"
    result = _run(diff)
    assert result.returncode == 1
    assert "AWS access key" in result.stderr


def test_anthropic_key_blocks() -> None:
    diff = "+ANTHROPIC_API_KEY=sk-ant-abcDEF_123-xyz\n"
    result = _run(diff)
    assert result.returncode == 1
    assert "Anthropic" in result.stderr


def test_github_token_blocks() -> None:
    diff = "+TOKEN = 'ghp_" + "A" * 36 + "'\n"
    result = _run(diff)
    assert result.returncode == 1
    assert "GitHub" in result.stderr


def test_env_value_substring_blocks(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("DB_PASSWORD=hunter2supersecret\nUNSET=\n")
    diff = "+config = {'pwd': 'hunter2supersecret'}\n"
    result = _run(diff, "--env-file", str(env))
    assert result.returncode == 1
    assert ".env value" in result.stderr


def test_env_value_empty_lines_ignored(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("UNSET=\n# comment\n\n")
    diff = "+nothing = 'fine'\n"
    result = _run(diff, "--env-file", str(env))
    assert result.returncode == 0


def test_restricted_filename_blocks() -> None:
    diff = (
        "diff --git a/secrets/gws-sa-key.json b/secrets/gws-sa-key.json\n"
        "+{\"private_key\": \"...\"}\n"
    )
    result = _run(diff)
    assert result.returncode == 1
    assert "restricted file" in result.stderr.lower()


def test_debug_leftover_in_source_blocks() -> None:
    diff = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "+def f():\n"
        "+    print('debug')\n"
    )
    result = _run(diff)
    assert result.returncode == 1
    assert "debug" in result.stderr.lower()


def test_debug_in_test_file_allowed() -> None:
    diff = (
        "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
        "+def test_x():\n"
        "+    print('ok')\n"
    )
    result = _run(diff)
    assert result.returncode == 0
```

- [ ] **Step 2: Run tests; verify they fail with `FileNotFoundError` (script missing)**

Run: `uv run pytest tests/skills/test_pm_autonomous_secret_scan.py -v`
Expected: FAIL — script does not exist yet.

- [ ] **Step 3: Implement `secret_scan.py`**

```python
# plugins/canopy/skills/product-management/scripts/secret_scan.py
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
    r"^\+\+\+ b/.*("
    r"\.env(\.[^/]+)?$"
    r"|.*\.key$"
    r"|.*\.pem$"
    r"|credentials\.json$"
    r"|gws-sa-key\.json$"
    r"|.*-secret\..*"
    r")",
    re.MULTILINE,
)

DEBUG_PATTERNS = [
    re.compile(r"^\+.*\bprint\("),
    re.compile(r"^\+.*\bconsole\.log\("),
    re.compile(r"^\+.*\bbreakpoint\(\)"),
    re.compile(r"^\+.*\bdebugger;"),
]


def _split_by_file(diff: str) -> list[tuple[str, str]]:
    """Split a unified diff into (path, hunk_text) pairs, keyed by 'b/' path."""
    chunks: list[tuple[str, str]] = []
    current_path: str | None = None
    current_lines: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path is not None:
                chunks.append((current_path, "".join(current_lines)))
            current_path = None
            current_lines = [line]
        elif line.startswith("+++ b/"):
            current_path = line[len("+++ b/") :].rstrip("\n")
            current_lines.append(line)
        else:
            current_lines.append(line)
    if current_path is not None:
        chunks.append((current_path, "".join(current_lines)))
    return chunks


def _is_test_path(path: str) -> bool:
    parts = path.split("/")
    return any(p in ("tests", "test", "__tests__") or p.startswith("test_") for p in parts)


def scan(diff: str, env_values: list[str]) -> list[str]:
    failures: list[str] = []

    for label, pattern in LEAK_PATTERNS:
        if pattern.search(diff):
            failures.append(f"{label} pattern matched in diff")

    if RESTRICTED_FILENAMES.search(diff):
        failures.append("restricted filename present in diff")

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
    for raw in env_path.read_text().splitlines():
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
```

- [ ] **Step 4: Run tests; verify pass**

Run: `uv run pytest tests/skills/test_pm_autonomous_secret_scan.py -v`
Expected: PASS — all eight tests green.

- [ ] **Step 5: Commit**

```bash
git add plugins/canopy/skills/product-management/scripts/__init__.py \
         plugins/canopy/skills/product-management/scripts/secret_scan.py \
         tests/skills/test_pm_autonomous_secret_scan.py
git commit -m "feat(pm): secret-leak scanner for autonomous gate"
```

---

## Task 2: Diff-size check script + tests

**Files:**
- Create: `plugins/canopy/skills/product-management/scripts/diff_size_check.py`
- Create: `tests/skills/test_pm_autonomous_diff_size.py`

**What it does:** Reads `git diff --stat` output on stdin (or a raw diff with `--mode raw`). Sums changed lines. Exits 1 if total exceeds the configured limit (default 1500), prints summary on stderr.

- [ ] **Step 1: Write failing tests**

```python
# tests/skills/test_pm_autonomous_diff_size.py
"""Tests for the diff-size cap used by the autonomous PM gate."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent.parent
    / "plugins"
    / "canopy"
    / "skills"
    / "product-management"
    / "scripts"
    / "diff_size_check.py"
)


def _run(stdin: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


SMALL_STAT = (
    " src/a.py | 12 ++++++------\n"
    " src/b.py | 30 +++++++++++++++++++++---------\n"
    " 2 files changed, 25 insertions(+), 17 deletions(-)\n"
)

LARGE_STAT = (
    " src/big.py | 1600 ++++++++++++++++++++++++++++++++++\n"
    " 1 file changed, 1500 insertions(+), 100 deletions(-)\n"
)


def test_small_diff_passes() -> None:
    result = _run(SMALL_STAT, "--limit", "1500")
    assert result.returncode == 0, result.stderr


def test_large_diff_blocks() -> None:
    result = _run(LARGE_STAT, "--limit", "1500")
    assert result.returncode == 1
    assert "1600" in result.stderr or "exceeds" in result.stderr.lower()


def test_default_limit_is_1500() -> None:
    just_over = (
        " src/x.py | 1501 +\n"
        " 1 file changed, 1501 insertions(+), 0 deletions(-)\n"
    )
    result = _run(just_over)
    assert result.returncode == 1


def test_at_limit_passes() -> None:
    at = (
        " src/x.py | 1500 +\n"
        " 1 file changed, 1500 insertions(+), 0 deletions(-)\n"
    )
    result = _run(at)
    assert result.returncode == 0


def test_empty_input_passes() -> None:
    result = _run("")
    assert result.returncode == 0
```

- [ ] **Step 2: Run tests; verify FAIL (script missing)**

Run: `uv run pytest tests/skills/test_pm_autonomous_diff_size.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `diff_size_check.py`**

```python
# plugins/canopy/skills/product-management/scripts/diff_size_check.py
"""Diff-size cap for the autonomous PM gate (spec §3a).

Reads `git diff --stat` output on stdin. Looks for the summary line
'N files changed, X insertions(+), Y deletions(-)' and fails if X+Y
exceeds the limit (default 1500).
"""
from __future__ import annotations

import argparse
import re
import sys

SUMMARY_RE = re.compile(
    r"(\d+)\s+insertions?\(\+\).*?(\d+)\s+deletions?\(-\)"
)


def total_changed_lines(stat_output: str) -> int:
    total = 0
    for line in stat_output.splitlines():
        m = SUMMARY_RE.search(line)
        if m:
            total += int(m.group(1)) + int(m.group(2))
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1500)
    args = parser.parse_args()

    stat = sys.stdin.read()
    total = total_changed_lines(stat)
    if total > args.limit:
        print(
            f"diff-size: {total} changed lines exceeds limit {args.limit}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests; verify pass**

Run: `uv run pytest tests/skills/test_pm_autonomous_diff_size.py -v`
Expected: PASS — all five tests green.

- [ ] **Step 5: Commit**

```bash
git add plugins/canopy/skills/product-management/scripts/diff_size_check.py \
         tests/skills/test_pm_autonomous_diff_size.py
git commit -m "feat(pm): diff-size cap for autonomous gate"
```

---

## Task 3: `autonomous.yaml` config validator + tests

**Files:**
- Create: `plugins/canopy/skills/product-management/scripts/validate_autonomous_config.py`
- Create: `tests/skills/test_pm_autonomous_config.py`

**What it does:** Phase 0 validator. Loads `.claude/pm/autonomous.yaml`, checks all required keys exist with sane types. Prints a single human-readable "ready" line on success or a list of validation errors on failure. Exits 0/1.

Required keys per spec §2:

```
email.to (str), email.from (str), email.subject_prefix (str), email.sender_skill (str)
shipping.branch_prefix (str), shipping.pr_label (str), shipping.merge ("squash"|"merge"|"rebase"),
shipping.deploy_command (str), shipping.deploy_workflow (str),
shipping.post_deploy_health (list[str], min 1)
testing.unit (str), testing.lint (str), testing.types (str)
testing.dogfood.base_url (str), testing.dogfood.start_command (str),
testing.dogfood.wait_for (str), testing.dogfood.headless_browser_skill (str)
guardrails.one_pr_in_flight (bool), guardrails.diff_size_limit_lines (int > 0),
guardrails.max_fix_forward_attempts (int > 0)
theme_detection.lens_rotation (list[str], min 1)
```

- [ ] **Step 1: Write failing tests**

```python
# tests/skills/test_pm_autonomous_config.py
"""Tests for the autonomous.yaml validator (spec §2 + Phase 0)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = (
    Path(__file__).parent.parent.parent
    / "plugins"
    / "canopy"
    / "skills"
    / "product-management"
    / "scripts"
    / "validate_autonomous_config.py"
)

VALID = {
    "email": {
        "to": "user@example.com",
        "from": "bot@example.com",
        "subject_prefix": "[proj]",
        "sender_skill": "ace:email-communicator",
    },
    "shipping": {
        "branch_prefix": "proj/auto/",
        "pr_label": "autonomous",
        "merge": "squash",
        "deploy_command": "gh workflow run deploy.yml",
        "deploy_workflow": "deploy.yml",
        "post_deploy_health": ["https://example.com/health"],
    },
    "testing": {
        "unit": "pytest -q",
        "lint": "ruff check .",
        "types": "tsc -b",
        "dogfood": {
            "base_url": "http://localhost:8000",
            "start_command": "docker compose up -d",
            "wait_for": "http://localhost:8000/health",
            "headless_browser_skill": "gstack",
        },
    },
    "guardrails": {
        "one_pr_in_flight": True,
        "diff_size_limit_lines": 1500,
        "max_fix_forward_attempts": 3,
    },
    "theme_detection": {
        "lens_rotation": ["user-value", "tech-debt"],
    },
}


def _run(cfg_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(cfg_path)],
        capture_output=True,
        text=True,
        check=False,
    )


def _write(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "autonomous.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


def test_valid_config_passes(tmp_path: Path) -> None:
    p = _write(tmp_path, VALID)
    result = _run(p)
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stdout.lower()


def test_missing_file_fails(tmp_path: Path) -> None:
    result = _run(tmp_path / "nope.yaml")
    assert result.returncode == 1
    assert "not found" in result.stderr.lower() or "no such" in result.stderr.lower()


def test_missing_email_to_fails(tmp_path: Path) -> None:
    cfg = {**VALID, "email": {**VALID["email"]}}
    del cfg["email"]["to"]
    p = _write(tmp_path, cfg)
    result = _run(p)
    assert result.returncode == 1
    assert "email.to" in result.stderr


def test_bad_merge_value_fails(tmp_path: Path) -> None:
    cfg = {**VALID, "shipping": {**VALID["shipping"], "merge": "bogus"}}
    p = _write(tmp_path, cfg)
    result = _run(p)
    assert result.returncode == 1
    assert "shipping.merge" in result.stderr


def test_negative_diff_limit_fails(tmp_path: Path) -> None:
    cfg = {
        **VALID,
        "guardrails": {**VALID["guardrails"], "diff_size_limit_lines": -1},
    }
    p = _write(tmp_path, cfg)
    result = _run(p)
    assert result.returncode == 1
    assert "diff_size_limit_lines" in result.stderr


def test_empty_lens_rotation_fails(tmp_path: Path) -> None:
    cfg = {**VALID, "theme_detection": {"lens_rotation": []}}
    p = _write(tmp_path, cfg)
    result = _run(p)
    assert result.returncode == 1
    assert "lens_rotation" in result.stderr


def test_missing_dogfood_block_fails(tmp_path: Path) -> None:
    cfg = {**VALID, "testing": {k: v for k, v in VALID["testing"].items() if k != "dogfood"}}
    p = _write(tmp_path, cfg)
    result = _run(p)
    assert result.returncode == 1
    assert "dogfood" in result.stderr


def test_post_deploy_health_must_be_nonempty_list(tmp_path: Path) -> None:
    cfg = {
        **VALID,
        "shipping": {**VALID["shipping"], "post_deploy_health": []},
    }
    p = _write(tmp_path, cfg)
    result = _run(p)
    assert result.returncode == 1
    assert "post_deploy_health" in result.stderr


def test_malformed_yaml_fails(tmp_path: Path) -> None:
    p = tmp_path / "autonomous.yaml"
    p.write_text("email: : :\n")
    result = _run(p)
    assert result.returncode == 1
    assert "yaml" in result.stderr.lower() or "parse" in result.stderr.lower()
```

- [ ] **Step 2: Run tests; verify FAIL (script missing)**

Run: `uv run pytest tests/skills/test_pm_autonomous_config.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement validator**

```python
# plugins/canopy/skills/product-management/scripts/validate_autonomous_config.py
"""Validate `.claude/pm/autonomous.yaml` (spec §2, Phase 0).

Usage: python validate_autonomous_config.py <path/to/autonomous.yaml>

Exit 0 + 'ready: <project>' on success.
Exit 1 + per-error stderr on failure.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

REQUIRED: list[tuple[str, type | tuple[type, ...]]] = [
    ("email.to", str),
    ("email.from", str),
    ("email.subject_prefix", str),
    ("email.sender_skill", str),
    ("shipping.branch_prefix", str),
    ("shipping.pr_label", str),
    ("shipping.merge", str),
    ("shipping.deploy_command", str),
    ("shipping.deploy_workflow", str),
    ("testing.unit", str),
    ("testing.lint", str),
    ("testing.types", str),
    ("testing.dogfood.base_url", str),
    ("testing.dogfood.start_command", str),
    ("testing.dogfood.wait_for", str),
    ("testing.dogfood.headless_browser_skill", str),
    ("guardrails.one_pr_in_flight", bool),
    ("guardrails.diff_size_limit_lines", int),
    ("guardrails.max_fix_forward_attempts", int),
]

ALLOWED_MERGE = {"squash", "merge", "rebase"}


def _get(cfg: Any, dotted: str) -> Any:
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


_MISSING = object()


def validate(cfg: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return ["root: expected mapping"]

    for key, expected_type in REQUIRED:
        value = _get(cfg, key)
        if value is _MISSING:
            errors.append(f"{key}: missing")
            continue
        if not isinstance(value, expected_type):
            errors.append(
                f"{key}: expected {expected_type.__name__}, got {type(value).__name__}"
            )

    merge = _get(cfg, "shipping.merge")
    if isinstance(merge, str) and merge not in ALLOWED_MERGE:
        errors.append(
            f"shipping.merge: must be one of {sorted(ALLOWED_MERGE)}, got {merge!r}"
        )

    diff_limit = _get(cfg, "guardrails.diff_size_limit_lines")
    if isinstance(diff_limit, int) and diff_limit <= 0:
        errors.append("guardrails.diff_size_limit_lines: must be positive")

    fix_attempts = _get(cfg, "guardrails.max_fix_forward_attempts")
    if isinstance(fix_attempts, int) and fix_attempts <= 0:
        errors.append("guardrails.max_fix_forward_attempts: must be positive")

    health = _get(cfg, "shipping.post_deploy_health")
    if health is _MISSING or not isinstance(health, list) or not health:
        errors.append("shipping.post_deploy_health: must be a non-empty list of URLs")
    elif not all(isinstance(h, str) and h for h in health):
        errors.append("shipping.post_deploy_health: every entry must be a non-empty string")

    lenses = _get(cfg, "theme_detection.lens_rotation")
    if lenses is _MISSING or not isinstance(lenses, list) or not lenses:
        errors.append("theme_detection.lens_rotation: must be a non-empty list")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()

    if not args.path.exists():
        print(f"validate-config: file not found: {args.path}", file=sys.stderr)
        return 1
    try:
        cfg = yaml.safe_load(args.path.read_text())
    except yaml.YAMLError as exc:
        print(f"validate-config: yaml parse error: {exc}", file=sys.stderr)
        return 1

    errors = validate(cfg)
    if errors:
        for e in errors:
            print(f"validate-config: {e}", file=sys.stderr)
        return 1

    print(f"ready: {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests; verify pass**

Run: `uv run pytest tests/skills/test_pm_autonomous_config.py -v`
Expected: PASS — all nine tests green.

- [ ] **Step 5: Commit**

```bash
git add plugins/canopy/skills/product-management/scripts/validate_autonomous_config.py \
         tests/skills/test_pm_autonomous_config.py
git commit -m "feat(pm): autonomous.yaml validator for Phase 0"
```

---

## Task 4: Autonomous templates — config-schema, convince-self-gate, email-format

**Files:**
- Create: `plugins/canopy/skills/product-management/templates/autonomous/config-schema.md`
- Create: `plugins/canopy/skills/product-management/templates/autonomous/convince-self-gate.md`
- Create: `plugins/canopy/skills/product-management/templates/autonomous/email-format.md`

These are reference docs the SKILL.md autonomous-mode section points at. Keep them tight and prescriptive. No pseudo-code that pretends to be running code — these are read by Claude as instructions.

- [ ] **Step 1: Write `config-schema.md`**

```markdown
# autonomous.yaml — Schema and Example

This file is required at `.claude/pm/autonomous.yaml` for any project that adopts the autonomous mode of `canopy:product-management`. Without it, `/canopy:pm-autonomous` and `/canopy:pm-autonomous-loop` refuse to run.

The skill is deliberately project-agnostic — every project-specific knob lives here.

## Required keys

| Key | Type | Notes |
|-----|------|-------|
| `email.to` | str | Recipient address (single user) |
| `email.from` | str | Sender address; must match the `sender_skill`'s configured account |
| `email.subject_prefix` | str | Wraps every release-notes subject, e.g. `[ace-web]` |
| `email.sender_skill` | str | Fully-qualified skill name — autonomous mode invokes this skill to send mail |
| `shipping.branch_prefix` | str | All autonomous PRs branch from this prefix, e.g. `ace-web/auto/` |
| `shipping.pr_label` | str | GitHub label applied to every autonomous PR (visibility) |
| `shipping.merge` | str | One of `squash`, `merge`, `rebase` |
| `shipping.deploy_command` | str | Shell command to trigger deploy after merge |
| `shipping.deploy_workflow` | str | Workflow filename, used to poll deploy status |
| `shipping.post_deploy_health` | list[str] | Non-empty list of URLs polled after deploy |
| `testing.unit` / `testing.lint` / `testing.types` | str | Mechanical-check commands run by the gate |
| `testing.dogfood.start_command` | str | Brings the local stack up |
| `testing.dogfood.wait_for` | str | URL polled until the stack is ready |
| `testing.dogfood.base_url` | str | Root URL the headless browser drives |
| `testing.dogfood.headless_browser_skill` | str | Skill name (`gstack`, `browse`, etc.) |
| `guardrails.one_pr_in_flight` | bool | Hardcoded `true` for v1; rejecting any other value is fine |
| `guardrails.diff_size_limit_lines` | int > 0 | Cap fed to `diff_size_check.py` |
| `guardrails.max_fix_forward_attempts` | int > 0 | After this many failed cycles on the same red signal, the sprint logs "stuck" and stops |
| `theme_detection.lens_rotation` | list[str] | Starting lenses for Phase A scout; the sprint is free to mix |

## Validation

Run the validator manually with:

```bash
PLUGIN_PATH=$(python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); print(d['plugins']['canopy@canopy'][0]['installPath'])")
python3 "$PLUGIN_PATH/skills/product-management/scripts/validate_autonomous_config.py" .claude/pm/autonomous.yaml
```

Phase 0 of `cycle.md` calls this script automatically.

## Canonical example (ace-web — for illustration; the actual file lives in the ace-web repo, not here)

```yaml
email:
  to: jjackson@dimagi.com
  from: ace@dimagi-ai.com
  subject_prefix: "[ace-web]"
  sender_skill: ace:email-communicator

shipping:
  branch_prefix: ace-web/auto/
  pr_label: autonomous
  merge: squash
  deploy_command: gh workflow run deploy-labs.yml --ref main -f run_migrations=false
  deploy_workflow: deploy-labs.yml
  post_deploy_health:
    - https://labs.connect.dimagi.com/ace/api/health

testing:
  unit:    .venv/bin/python -m pytest -q
  lint:    .venv/bin/python -m ruff check .
  types:   bash -c "cd frontend && node_modules/.bin/tsc -b"
  dogfood:
    base_url: http://localhost:8000/ace
    start_command: docker compose up -d
    wait_for: http://localhost:8000/ace/api/health
    headless_browser_skill: gstack

guardrails:
  one_pr_in_flight: true
  diff_size_limit_lines: 1500
  max_fix_forward_attempts: 3

theme_detection:
  lens_rotation:
    - user-value
    - adoption-blockers
    - integration-depth
    - trust-reliability
    - tech-debt
```
```

- [ ] **Step 2: Write `convince-self-gate.md`**

```markdown
# Convince-self-it's-clean gate

Runs before each PR opens during a `/canopy:pm-autonomous` sprint. Goal: not just "tests pass" but "I'd defend this in a code review." Layered, in order. Any layer failing means the proposal is dropped (logged in the cycle log under `self-review-blocked`) and the loop moves on to re-derive the corresponding email highlight.

Don't skip layers. Don't merge them. The point of the gate is to make a different kind of mistake visible at each stage.

## 3a. Mechanical checks

Run all of these against the staged change. They are CHEAP — run them in parallel where possible.

1. `testing.unit` — must exit 0
2. `testing.lint` — must exit 0
3. `testing.types` — must exit 0
4. **Secret-leak scan (hardcoded patterns; not configurable):**

   ```bash
   git diff --staged | python3 "$PLUGIN_PATH/skills/product-management/scripts/secret_scan.py" --env-file .env
   ```

   Where `$PLUGIN_PATH` is resolved via `installed_plugins.json` as in `config-schema.md`. The `--env-file` flag is omitted if `.env` does not exist.

5. **Diff-size cap:**

   ```bash
   git diff --staged --stat | python3 "$PLUGIN_PATH/skills/product-management/scripts/diff_size_check.py" --limit "$DIFF_LIMIT"
   ```

   Where `$DIFF_LIMIT` is `guardrails.diff_size_limit_lines` from `autonomous.yaml`.

If any mechanical check fails, abandon this proposal — log it in the run log, do NOT try to "fix" by reducing scope. The cycle simply re-derives the email highlight.

## 3b. Self-review pass — five questions, written to the cycle log

Re-read the diff. Answer each question IN WRITING in the cycle log. The act of writing the answer is the gate — vague or evasive answers fail.

1. **What invariant did I just change?** Name a specific contract — input format, return semantics, side-effect ordering, persisted-state shape. Answer "none" or "I don't know" → FAIL.
2. **What's the riskiest line in this diff?** Quote a specific line. "Nothing is risky" on any non-trivial change → FAIL.
3. **What would a senior eng object to in code review?** Name a concrete objection, even if you disagree with it. "Nothing comes to mind" on a non-trivial change → FAIL (probable blind spot).
4. **Did I touch a test that codifies a behavior I'm changing?** If yes, did I update the test's *intent* or merely patch its assertions to match? "Patched the assertions" → FAIL.
5. **Would I be comfortable if this shipped while I was on vacation?** Hesitation → FAIL.

A failure on any question DROPS the proposal — write the question number and a one-sentence reason in the run log under `self-review-blocked`, then re-derive the corresponding email highlight (see Phase C in `cycle.md`).

## 3c. Dogfood pass — required for any "Try it" feature

For any change that's named in a `Try it:` line of the target email:

1. Start the local stack: `bash -lc "$(yq '.testing.dogfood.start_command' .claude/pm/autonomous.yaml)"`
2. Wait until `testing.dogfood.wait_for` returns 200, polling every 5s with a 5-min ceiling
3. Drive the change in the configured `headless_browser_skill` — actually click through, verify the expected behavior visibly happens
4. Capture a sequence of screenshots: a "before" (revert briefly OR feature-flag OR describe-from-memory if no clean before-state exists) and an "after". Save under `.claude/pm/sent-emails/<sprint-slug>/screenshots/`
5. Reference them in the email body per `email-format.md`

A purely backend change (no user-visible surface) can SKIP dogfood, but then it CANNOT appear as a "Try it" highlight — only in the internal `*` section.

## 3d. Post-deploy health check

After deploy:

1. Poll each `shipping.post_deploy_health` URL with backoff (5s, 10s, 20s, 40s, 80s — total ~5 min) for 200 OK
2. If any URL fails or stays 5xx: do NOT auto-revert. The broken-prod state becomes the next scout finding; switch into a fix-forward investigation cycle (still autonomous)
3. If still red after `guardrails.max_fix_forward_attempts` cycles: log "stuck" in the run log, send a minimal "no email this sprint, here's why" note via `email.sender_skill`, stop.
```

- [ ] **Step 3: Write `email-format.md`**

```markdown
# Working-backwards email — format

Sent at the end of each `/canopy:pm-autonomous` sprint. The body is generated programmatically from the cycle log and screenshot directory — NOT freehand-written. This keeps the email honest: no inventing wins, no hand-waving past failures.

Save the rendered body to `.claude/pm/sent-emails/<YYYY-MM-DD-theme-slug>/email.md` before sending.

## Subject

```
<email.subject_prefix> Release notes — <theme summary> — <YYYY-MM-DD>
```

Theme summary is 2-5 words; come from the surviving highlights, not from `theme_detection.lens_rotation`.

## Body

```markdown
# What's new in <product>

> 2-3 sentence customer pitch — what's better today than yesterday, framed as the value to the user / LLO / contributor. No PR numbers, no jargon.

## Highlights

- **<Feature 1>** — One sentence. Why it matters to you.

  ![<feature-1 screenshot>](screenshots/feature-1-after.png)

  *Try it:* one-line instruction with a clickable URL.

- **<Feature 2>** — …

  ![<feature-2 walkthrough>](screenshots/feature-2-walkthrough.png)

  *Try it:* …

(3-6 highlights; one per shipped item or grouped if they tell one story. Every "Try it" highlight has at least one screenshot from the dogfood pass.)

## Walkthrough

> Optional: if multiple features compose into a single user journey, embed a 2-4 panel sequence showing the journey end-to-end.

---

## * Internal notes

**Sprint summary:** <theme>, <N PRs>, <X cycles>, <Y minutes wall-clock>.

**What shipped (engineering view):**

| PR  | Lens                | Title                       | Self-review verdict   |
|-----|---------------------|-----------------------------|-----------------------|
| #143 | user-value         | …                           | "would defend in CR"  |
| #144 | adoption-blockers  | …                           | "would defend in CR"  |

**Self-review blocks (proposals dropped before PR):**
- `<title>` — blocked on Q3 ("can't name what a senior would object to" → blind spot suspected in <area>)
- `<title>` — blocked on Q5 ("hesitated on vacation-test")

**Deploy / health:**
- N deploys, all green
- (or: "deploy-X failed at <step>, fixed forward in PR#Y, see cycle log")

**What I'd do next** (suggestion, not commitment):
- One or two specific lenses or surfaces with the most untapped value.

---

## ** Canopy self-improvement notes

Process improvements I made to the `canopy:product-management` skill this sprint (separately committed PRs to `jjackson/canopy`):

- **<insight>** — link to canopy PR#Z. What changed and why.
- **<insight>** — …

If no canopy improvements this sprint, this section says "No new universal lessons this sprint."
```

## Sender invocation

Invoke `email.sender_skill` with:

- `subject` (string)
- `body_markdown` (the rendered file above)
- `attachments` (list of absolute paths, one per screenshot referenced in the body)

If the configured sender skill does not support attachments, log that limitation in the run log and send body-only — the recipient can browse the repo to see screenshots. Do NOT block on attachment support; the email is still worth sending.

## Stuck-state alternative

If the sprint never converged on a passable email (Phase D failed three critique passes), send a minimal alternative body:

```markdown
# Sprint <date> — no release notes this time

The autonomous PM sprint did not converge on something worth shipping in customer voice today. Cycle log: `.claude/pm/runs/<file>.md`. The next sprint will scout against <suggested lens> first.

Why this happened (one paragraph):
<the actual reason from the run log — self-review blocks, repeated CI red, etc.>
```

This is a feature, not a failure mode. The whole point of the email-and-stop loop is to refuse to ship weak content.
```

- [ ] **Step 4: Commit**

```bash
git add plugins/canopy/skills/product-management/templates/autonomous/
git commit -m "feat(pm): autonomous-mode reference templates"
```

---

## Task 5: Autonomous templates — `cycle.md` (Phases A–E procedure)

**Files:**
- Create: `plugins/canopy/skills/product-management/templates/autonomous/cycle.md`

This is the long one — it's what Claude actually follows when running a sprint. Reference the other templates by relative path.

- [ ] **Step 1: Write `cycle.md`**

```markdown
# Autonomous PM cycle (Phases A–E)

This is the procedure executed by `/canopy:pm-autonomous` for one sprint. Read it top-to-bottom before starting; do NOT improvise the order.

The sprint succeeds when a customer-quality release-notes email is sent. It fails (gracefully) when convergence isn't possible — in which case a minimal "stuck" email is sent and the sprint stops.

This skill is project-agnostic. ALL project-specific knobs live in `.claude/pm/autonomous.yaml` — see `config-schema.md`.

## Phase 0 — Pre-flight

Run sequentially, NOT in parallel (`.claude/pm/` may not exist yet on a fresh project):

1. Resolve `$PLUGIN_PATH` once and reuse:

   ```bash
   PLUGIN_PATH=$(python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); print(d['plugins']['canopy@canopy'][0]['installPath'])")
   ```

2. Validate `.claude/pm/autonomous.yaml`:

   ```bash
   python3 "$PLUGIN_PATH/skills/product-management/scripts/validate_autonomous_config.py" .claude/pm/autonomous.yaml
   ```

   Refuse to run on non-zero exit. Print the validator stderr and stop.

3. Read `.claude/pm/context.md` and `.claude/pm/learnings.md`. If `context.md` is missing, run the existing skill's bootstrap flow first (see SKILL.md "Bootstrapping: Building context.md"), THEN re-enter Phase 0.

4. Confirm git state: clean working tree, on `main`, fully up-to-date (`git fetch && git status`).

5. Confirm only ONE autonomous PR is in flight (per `guardrails.one_pr_in_flight`). Query:

   ```bash
   gh pr list --label "$PR_LABEL" --state open --json number,headRefName
   ```

   If a previous autonomous PR is still open, RESUME that PR instead of starting fresh — pick up at Phase C and drive it to merge before opening anything new.

## Phase A — Working-backwards draft (5–10 min)

Goal: produce a target email DRAFT that can pass three critiques before any engineering happens.

1. Quick scout pass across the lenses in `theme_detection.lens_rotation`. Just enough breadth to see what's ripe — no deep dives.
2. Draft the target email using `email-format.md`'s template, AS IF IT WERE ALREADY TRUE. Specific feature names. Specific value statements. No placeholders.
3. Self-critique against three tests; write the verdict for each into the cycle log:
   - **Clear:** Could a non-technical user read this and know what's better today than yesterday? Does each highlight name a concrete thing they can click on?
   - **Testable:** For each highlight, can I write a one-line "Try it" instruction that proves it works? If a highlight doesn't survive a "go click this URL and see X happen" test, it's vapor — drop it.
   - **Impressive:** Does this move the product forward in a way the user would *care about*? Not "code is cleaner" — "you can now do thing-Y you couldn't before, or thing-Z is meaningfully nicer." If the most exciting highlight is "polished some copy," the answer is no.
4. If any critique fails, LOOP on Phase A. Scout deeper. Swap a weak highlight. Change the theme. Expand scope. Do NOT proceed to Phase B with a draft that is not yet impressive — that is the single most important rule of this skill.

The approved draft, with every critique annotated PASS, is the input to Phase B.

## Phase B — Derive the work

For each highlight in the approved draft, write one or more concrete proposals — title, files, expected diff shape, validation. Estimate effort per proposal. If the total estimated effort exceeds ~6 hours of cycle time, TRIM: keep the most "Try it"-able subset and save the rest as a future-sprint note in the run log.

## Phase C — Ship

For each proposal, in order, ONE PR IN FLIGHT AT A TIME:

1. Create branch: `git checkout -b "$BRANCH_PREFIX$(slug-of title)"`
2. Implement the change. Use TDD where it fits (`superpowers:test-driven-development`); skip TDD only when the change is purely a behavior-of-no-test-yet thing and a test would be theatre.
3. Stage the change: `git add -A`
4. Run the full convince-self-it's-clean gate per `convince-self-gate.md` — sections 3a then 3b then 3c (if this proposal corresponds to a "Try it" highlight).
5. If the gate drops the proposal, log it under `self-review-blocked` in the run log AND re-derive the corresponding email highlight (try a different angle, or drop it and scout for a replacement). The email must remain impressive or it's not worth sending.
6. Open PR: `gh pr create --label "$PR_LABEL" --base main --title "<title>" --body "<body>"`. Body cites the email highlight this proposal makes true.
7. Wait for CI. On red:
   - Up to 2 fix-forward attempts on the same PR (re-run the gate each time)
   - Beyond that, switch this PR to a fix-forward investigation cycle. Track attempts against `guardrails.max_fix_forward_attempts`.
   - If exhausted, log "stuck", call Phase E with a stuck-state email, exit.
8. On CI green: merge per `shipping.merge`.
9. Run `shipping.deploy_command`.
10. Poll deploy status (use `gh run list --workflow="$DEPLOY_WORKFLOW"` until completion).
11. Run section 3d post-deploy health check.
12. Update the cycle log with: branch, PR number, gate verdicts (each Q answered), deploy status, health-check status.
13. Move to the next proposal.

## Phase D — Reality reconciliation

Reality always diverges from plan. Before sending the email:

1. Rewrite the email body based on what ACTUALLY shipped — which highlights survived, what new value emerged, what got cut.
2. Re-run the three critiques (Clear, Testable, Impressive) on the rewritten version.
3. **Also ask:** "What did I learn about the PM process itself this sprint?" Universal lessons (NOT project-specific) become a separate canopy PR per `SKILL.md`'s Self-Improvement Protocol. Link them in the email's `**` section.
4. If the rewritten email still passes → proceed to Phase E.
5. If it doesn't → do NOT send a weak email. Log "sprint failed to converge on a great email" in the run log, build the stuck-state email body per `email-format.md`, proceed to Phase E.

## Phase E — Send + stop

1. Render the email body to `.claude/pm/sent-emails/<YYYY-MM-DD-theme-slug>/email.md`. Copy the dogfood screenshots into the same directory's `screenshots/` subfolder.
2. Invoke `email.sender_skill` with `subject`, `body_markdown`, `attachments`.
3. Stop the loop. Exit cleanly.
4. The `/canopy:pm-autonomous-loop` wrapper, if it invoked us, will sleep until "keep going" or 24h timeout — see `pm-autonomous-loop.md`.

## State that persists across sprints

```
.claude/pm/
├── context.md
├── learnings.md
├── autonomous.yaml
├── runs/
│   └── YYYY-MM-DD-<theme-slug>.md   ← cycle log + self-review verdicts
└── sent-emails/
    └── YYYY-MM-DD-<theme-slug>/
        ├── email.md
        └── screenshots/
            └── *.png
```

`sent-emails/` exists so future sprints can avoid repeating "we shipped X" claims and so the user has a browseable archive of what's been said in their voice.
```

- [ ] **Step 2: Commit**

```bash
git add plugins/canopy/skills/product-management/templates/autonomous/cycle.md
git commit -m "feat(pm): autonomous cycle template (Phases A-E)"
```

---

## Task 6: SKILL.md — add "Autonomous mode" section, preserve human-gated flow

**Files:**
- Modify: `plugins/canopy/skills/product-management/SKILL.md`

The existing content stays untouched. Add a new section near the top that describes both modes, then a section near the end that points at the autonomous templates. The Phase 1–6 description stays exactly as-is — that's the human-gated path that `/canopy:pm-scout` rides on.

- [ ] **Step 1: Edit SKILL.md to add the mode-overview section after the existing Architecture block**

Add immediately after line 32 (after the "Why This Structure" bullets), before "## Project State Convention":

```markdown
## Two modes

This skill has two operating modes. The phases below describe the **human-gated** mode in detail — that is the original and default behavior.

- **Human-gated** (the Phase 0–6 procedure below). Entry point: `/canopy:pm-scout`. Phase 3 stops on `AskUserQuestion` for per-proposal disposition. Single sprint, exits when dispositions are recorded. **Unchanged.**
- **Autonomous.** Entry points: `/canopy:pm-autonomous` (one sprint) and `/canopy:pm-autonomous-loop` (sprint → wait → repeat). Auto-approves its own proposals. Runs a multi-layer convince-self-it's-clean gate, auto-merges on green CI, auto-deploys, and ends each sprint by sending a working-backwards release-notes email. Requires `.claude/pm/autonomous.yaml`. See **Autonomous mode** below.

When in doubt, the human-gated mode is the right default. Autonomous mode is opt-in per project via the config file.
```

Use the Edit tool with `old_string` matching:

```
- **Learning accumulates** in files that persist across sessions

## Project State Convention
```

and `new_string`:

```
- **Learning accumulates** in files that persist across sessions

## Two modes

This skill has two operating modes. The phases below describe the **human-gated** mode in detail — that is the original and default behavior.

- **Human-gated** (the Phase 0–6 procedure below). Entry point: `/canopy:pm-scout`. Phase 3 stops on `AskUserQuestion` for per-proposal disposition. Single sprint, exits when dispositions are recorded. **Unchanged.**
- **Autonomous.** Entry points: `/canopy:pm-autonomous` (one sprint) and `/canopy:pm-autonomous-loop` (sprint → wait → repeat). Auto-approves its own proposals. Runs a multi-layer convince-self-it's-clean gate, auto-merges on green CI, auto-deploys, and ends each sprint by sending a working-backwards release-notes email. Requires `.claude/pm/autonomous.yaml`. See **Autonomous mode** below.

When in doubt, the human-gated mode is the right default. Autonomous mode is opt-in per project via the config file.

## Project State Convention
```

- [ ] **Step 2: Append a full "Autonomous mode" section just before "## Self-Improvement Protocol"**

The autonomous section delegates the procedure to the templates. Keep the SKILL.md surface short; the depth lives in templates.

Use Edit to find:

```
**2. Evaluate for universal improvements** (see Self-Improvement Protocol below).

## Self-Improvement Protocol
```

and replace with:

```
**2. Evaluate for universal improvements** (see Self-Improvement Protocol below).

## Autonomous mode

The procedure for autonomous sprints lives in template files. Read them in order at the start of every autonomous run:

1. `templates/autonomous/config-schema.md` — `.claude/pm/autonomous.yaml` schema and example
2. `templates/autonomous/cycle.md` — Phases A–E (the working-backwards sprint)
3. `templates/autonomous/convince-self-gate.md` — the multi-layer gate that runs before every PR
4. `templates/autonomous/email-format.md` — body template for the working-backwards release-notes email

These templates are read using the Read tool from the cached plugin path:

```bash
PLUGIN_PATH=$(python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); print(d['plugins']['canopy@canopy'][0]['installPath'])")
ls "$PLUGIN_PATH/skills/product-management/templates/autonomous/"
```

The autonomous mode does NOT modify the human-gated Phase 0–6 procedure above. `/canopy:pm-scout` still runs the human-gated path verbatim.

### Hard rules for autonomous mode

1. **No proposal advances without passing the convince-self gate.** Mechanical checks, five self-review questions, dogfood (when applicable), post-deploy health.
2. **No weak emails.** Phase A loops until the email draft passes Clear/Testable/Impressive. Phase D refuses to send if reality diverged into something not worth sending — it sends a stuck-state note instead.
3. **One autonomous PR in flight at a time.** Resume an open one before opening a new one.
4. **No auto-revert on broken prod.** Fix forward, up to `guardrails.max_fix_forward_attempts` cycles, then stop with a stuck-state email.
5. **The skill stays project-agnostic.** Every project-specific value (deploy command, health URLs, sender skill, branch prefix, test commands) lives in `.claude/pm/autonomous.yaml`. Never hardcode them in SKILL.md or templates.

## Self-Improvement Protocol
```

- [ ] **Step 3: Sanity-check edits**

Run: `grep -c "AskUserQuestion" plugins/canopy/skills/product-management/SKILL.md`
Expected: at least 2 (the human-gated Phase 3 still references it).

Run: `grep -c "Autonomous mode" plugins/canopy/skills/product-management/SKILL.md`
Expected: at least 2.

- [ ] **Step 4: Commit**

```bash
git add plugins/canopy/skills/product-management/SKILL.md
git commit -m "feat(pm): document autonomous mode in SKILL.md"
```

---

## Task 7: New command — `/canopy:pm-autonomous`

**Files:**
- Create: `plugins/canopy/commands/pm-autonomous.md`

This is a colliding-name pattern (no — actually `pm-autonomous` is a NEW name; the skill is named `product-management`, so there's no collision and Pattern B does not apply). It can call the skill normally and tell the skill which mode to run.

- [ ] **Step 1: Write `pm-autonomous.md`**

```markdown
---
description: Run one autonomous PM sprint — scout, draft email, ship, send-and-stop. Requires .claude/pm/autonomous.yaml.
allowed-tools: [Read, Glob, Grep, Bash, Agent, Write, Edit]
---

# /canopy:pm-autonomous

Run a SINGLE autonomous product-management sprint on the current project.

This command does NOT prompt for per-proposal approval — it auto-approves its own work, runs a multi-layer convince-self-it's-clean gate before each PR, and ends with a working-backwards release-notes email sent to the address configured in `.claude/pm/autonomous.yaml`.

The existing human-gated `/canopy:pm-scout` is the right command if you want per-proposal control.

## Process

1. Resolve the plugin install path:

   ```bash
   PLUGIN_PATH=$(python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); print(d['plugins']['canopy@canopy'][0]['installPath'])")
   ```

2. Read the autonomous templates IN ORDER:
   - `$PLUGIN_PATH/skills/product-management/templates/autonomous/config-schema.md`
   - `$PLUGIN_PATH/skills/product-management/templates/autonomous/cycle.md`
   - `$PLUGIN_PATH/skills/product-management/templates/autonomous/convince-self-gate.md`
   - `$PLUGIN_PATH/skills/product-management/templates/autonomous/email-format.md`

3. Invoke the `canopy:product-management` skill (Skill tool) so its preamble runs.

4. Execute the autonomous cycle in `cycle.md` — Phase 0 first (validates config; refuses to run if invalid), then A → B → C → D → E.

5. Exit when Phase E sends the email (or the stuck-state email).

## Failure modes (none of these are bugs)

- Config invalid → Phase 0 prints the validator errors, refuses to run. Fix `.claude/pm/autonomous.yaml` and try again.
- No `.claude/pm/context.md` → bootstrap interactively first (the human-gated bootstrap flow still applies), then re-run.
- Phase A can't converge on an impressive email after deep scouting → sends a one-paragraph "no release notes this time" email and stops.
- Convince-self gate drops every candidate proposal → same as above.
- Post-deploy health stays red after `guardrails.max_fix_forward_attempts` cycles → "stuck" email, stop.

These are all features. The whole point is to refuse to ship weak content.
```

- [ ] **Step 2: Commit**

```bash
git add plugins/canopy/commands/pm-autonomous.md
git commit -m "feat(pm): /canopy:pm-autonomous command"
```

---

## Task 8: New command — `/canopy:pm-autonomous-loop`

**Files:**
- Create: `plugins/canopy/commands/pm-autonomous-loop.md`

Wraps `/canopy:pm-autonomous` in `/loop` (self-pacing dynamic mode). Each sprint exits with email + stop, then the loop sleeps until the user resumes or a 24h timeout fires.

- [ ] **Step 1: Write `pm-autonomous-loop.md`**

```markdown
---
description: Run autonomous PM sprints in a self-pacing loop — sprint, send email, wait for "keep going" or 24h timeout, repeat.
allowed-tools: [Read, Glob, Grep, Bash, Agent, Write, Edit]
---

# /canopy:pm-autonomous-loop

Wraps `/canopy:pm-autonomous` so it can run sprint-after-sprint without you re-typing the command each time.

## Process

1. Run one full sprint by invoking `/canopy:pm-autonomous` end-to-end (read `pm-autonomous.md` for the full procedure).

2. After the sprint sends its email and exits, ENTER WAIT STATE:
   - Sleep until either:
     - The user sends a follow-up message that does NOT match (case-insensitive, anchored at start, whole-word) `^(stop|pause|halt)\b`, OR
     - 24 hours have passed since the email was sent.
   - Use the `loop` skill (`/loop`) in dynamic mode for the wait — pass `<<autonomous-loop-dynamic>>` per its docs so the runtime resolves it.

3. On wake (either case):
   - If the wake was due to the 24h timeout, send a single non-nagging reminder ping via `email.sender_skill`: `Subject: <prefix> Autonomous PM sprint paused — say 'keep going' to resume`. Then re-enter wait state for another 24h cycle.
   - If the wake was a user message that DOES match `^(stop|pause|halt)\b`, exit cleanly — print "Stopping autonomous loop. Run /canopy:pm-autonomous-loop again to resume." and end.
   - Otherwise, treat ANY user message as resume. Start the next sprint.

4. Repeat from step 1 until exit.

## Notes

- The 24h timeout is hardcoded for v1. If you want a different cadence, run `/canopy:pm-autonomous` directly (single sprint, no loop) and re-invoke this command on your own schedule.
- The loop only ever holds ONE autonomous PR in flight at a time (enforced by the autonomous cycle's Phase 0).
- If the user-supplied resume message contains substantive guidance ("focus on adoption-blockers next", "skip the dogfood pass"), the next sprint should treat it as a high-priority hint in Phase A scouting.
```

- [ ] **Step 2: Commit**

```bash
git add plugins/canopy/commands/pm-autonomous-loop.md
git commit -m "feat(pm): /canopy:pm-autonomous-loop wrapper command"
```

---

## Task 9: Skill structural tests + pm-scout regression test

**Files:**
- Create: `tests/skills/test_pm_autonomous_skill_structure.py`
- Create: `tests/skills/test_pm_scout_regression.py`

These pin down two invariants:
1. The autonomous mode artifacts (templates, commands, scripts) all exist and have the right shape.
2. The human-gated `/canopy:pm-scout` flow remains intact — Phase 3 still uses `AskUserQuestion`, the command file's frontmatter and process steps haven't shifted.

- [ ] **Step 1: Write `test_pm_autonomous_skill_structure.py`**

```python
"""Structural invariants for the autonomous PM mode.

These tests don't exercise behavior — they pin down that the artifacts
(templates, commands, scripts) exist and contain the load-bearing strings
the cycle relies on. Drift here is a bug.
"""
from __future__ import annotations

import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent.parent / "plugins" / "canopy"
SKILL_DIR = PLUGIN_ROOT / "skills" / "product-management"
TEMPLATES = SKILL_DIR / "templates" / "autonomous"
SCRIPTS = SKILL_DIR / "scripts"
COMMANDS = PLUGIN_ROOT / "commands"


def test_templates_exist() -> None:
    expected = {"cycle.md", "config-schema.md", "convince-self-gate.md", "email-format.md"}
    actual = {p.name for p in TEMPLATES.glob("*.md")}
    assert expected <= actual, f"missing templates: {expected - actual}"


def test_scripts_exist() -> None:
    for name in ("secret_scan.py", "diff_size_check.py", "validate_autonomous_config.py"):
        assert (SCRIPTS / name).exists(), f"missing script: {name}"


def test_pm_autonomous_command_exists() -> None:
    assert (COMMANDS / "pm-autonomous.md").exists()


def test_pm_autonomous_loop_command_exists() -> None:
    assert (COMMANDS / "pm-autonomous-loop.md").exists()


def test_skill_md_describes_autonomous_mode() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text()
    assert "## Autonomous mode" in content
    assert "templates/autonomous/" in content
    # Both modes named:
    assert "Human-gated" in content
    assert "Autonomous" in content


def test_cycle_template_mentions_all_phases() -> None:
    content = (TEMPLATES / "cycle.md").read_text()
    for phase in ("Phase 0", "Phase A", "Phase B", "Phase C", "Phase D", "Phase E"):
        assert phase in content, f"cycle.md missing {phase}"


def test_gate_template_lists_five_self_review_questions() -> None:
    content = (TEMPLATES / "convince-self-gate.md").read_text()
    # Each of the five questions is numbered 1.–5. in spec §3b.
    bullets = re.findall(r"^\d+\.\s+\*\*", content, re.MULTILINE)
    assert len(bullets) >= 5, f"expected >=5 numbered self-review items, got {len(bullets)}"


def test_email_template_has_three_sections() -> None:
    content = (TEMPLATES / "email-format.md").read_text()
    assert "## Highlights" in content
    assert "## * Internal notes" in content
    assert "## ** Canopy self-improvement notes" in content


def test_pm_autonomous_command_frontmatter() -> None:
    content = (COMMANDS / "pm-autonomous.md").read_text()
    assert content.startswith("---\n")
    assert "description:" in content.split("---", 2)[1]


def test_pm_autonomous_loop_references_loop_skill() -> None:
    content = (COMMANDS / "pm-autonomous-loop.md").read_text()
    assert "/loop" in content or "loop skill" in content.lower()
    assert "stop|pause|halt" in content
```

- [ ] **Step 2: Write `test_pm_scout_regression.py`**

```python
"""Regression test for the human-gated /canopy:pm-scout flow.

Per the autonomous-mode spec, the existing human-gated path MUST keep
working unchanged. These tests pin down the structural elements the flow
depends on so the autonomous-mode work cannot silently weaken them.
"""
from __future__ import annotations

from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent.parent / "plugins" / "canopy"
SKILL = PLUGIN_ROOT / "skills" / "product-management" / "SKILL.md"
PM_SCOUT = PLUGIN_ROOT / "commands" / "pm-scout.md"


def test_pm_scout_command_exists() -> None:
    assert PM_SCOUT.exists()


def test_pm_scout_invokes_product_management_skill() -> None:
    content = PM_SCOUT.read_text()
    assert "product-management" in content


def test_pm_scout_arguments_lens() -> None:
    content = PM_SCOUT.read_text()
    assert "argument-hint" in content or "lens" in content.lower()


def test_skill_keeps_phase_3_askuserquestion() -> None:
    content = SKILL.read_text()
    # Phase 3 in the human-gated flow is the AskUserQuestion menu.
    assert "AskUserQuestion" in content
    assert "Phase 3" in content


def test_skill_keeps_disposition_options() -> None:
    content = SKILL.read_text()
    for option in ("Do it", "Backlog", "Close", "Redirect"):
        assert option in content, f"disposition option missing from SKILL.md: {option}"


def test_skill_keeps_six_human_phases() -> None:
    content = SKILL.read_text()
    for phase in (
        "Phase 0",
        "Phase 1",
        "Phase 2",
        "Phase 3",
        "Phase 4",
        "Phase 5",
        "Phase 6",
    ):
        assert phase in content, f"human-gated {phase} missing"


def test_skill_keeps_lens_rotation() -> None:
    content = SKILL.read_text()
    for lens in (
        "user-value",
        "adoption-blockers",
        "integration-depth",
        "trust-reliability",
        "tech-debt",
    ):
        assert lens in content, f"lens missing: {lens}"
```

- [ ] **Step 3: Run all new tests; verify pass**

Run: `uv run pytest tests/skills/ -v`
Expected: every test in the new files passes; pre-existing tests unaffected.

- [ ] **Step 4: Run the full suite; verify no regressions**

Run: `uv run pytest`
Expected: all 420+ tests still green.

- [ ] **Step 5: Commit**

```bash
git add tests/skills/test_pm_autonomous_skill_structure.py \
         tests/skills/test_pm_scout_regression.py
git commit -m "test(pm): structural + regression tests for autonomous mode"
```

---

## Task 10: Version bump

**Files:**
- Modify: `VERSION`
- Modify: `plugins/canopy/.claude-plugin/plugin.json`

Per CLAUDE.md, this is the #1 missed step. Bump patch only.

- [ ] **Step 1: Determine current and next version**

Run: `cat VERSION` → `0.2.50`
Next: `0.2.51` (patch + 1).

NOTE: if `main` has advanced while this branch was in flight, use the higher of (this branch, origin/main) + 1. Confirm with:

```bash
git fetch origin main
git show origin/main:VERSION
```

If origin/main shows a higher number, take that + 1.

- [ ] **Step 2: Edit `VERSION`**

Use Edit tool. Change `0.2.50` to `0.2.51` (or the resolved next number).

- [ ] **Step 3: Edit `plugins/canopy/.claude-plugin/plugin.json`**

Update the `version` field to match.

- [ ] **Step 4: Verify they match**

Run: `python3 -m orchestrator.cli version verify` (or `canopy version verify` if installed)
Expected: PASS — both files agree.

If the canopy CLI is not on PATH:

```bash
diff <(cat VERSION) <(python3 -c "import json; print(json.load(open('plugins/canopy/.claude-plugin/plugin.json'))['version'])")
```

Expected: empty diff.

- [ ] **Step 5: Commit**

```bash
git add VERSION plugins/canopy/.claude-plugin/plugin.json
git commit -m "chore: bump to 0.2.51 for autonomous-mode release"
```

---

## Task 11: Final smoke test + manual regression check

**Files:** none

This is the live-fire check. The unit tests confirm the artifacts exist and have the right shape. This step confirms that nothing about the existing human-gated workflow has broken.

- [ ] **Step 1: Full pytest run**

Run: `uv run pytest`
Expected: all green.

- [ ] **Step 2: Skim SKILL.md end-to-end**

Run: `wc -l plugins/canopy/skills/product-management/SKILL.md && head -60 plugins/canopy/skills/product-management/SKILL.md`
Expected: file grew (was 352 lines), still starts with the preamble + name/description frontmatter.

- [ ] **Step 3: Manual regression check on `/canopy:pm-scout` content**

Read `plugins/canopy/skills/product-management/SKILL.md` end-to-end. Verify by eye:
- The "Phase 1: Scout" → "Phase 6: Learn" sequence is intact and unchanged.
- The Phase 3 `AskUserQuestion` example block is intact.
- Lens rotation list is intact.
- `pm-scout.md` was not modified by this PR.

If anything in the original human-gated flow has shifted, revert that part — the spec is explicit that the existing path must not change.

- [ ] **Step 4: List the diff one last time**

Run: `git diff --stat main`
Expected: only the files this plan said to add/modify. No surprise changes.

- [ ] **Step 5: Push to origin**

```bash
git push -u origin emdash/pm-autonomous-3jjfv
```

- [ ] **Step 6: Open PR (use the dev-utils:create-pr skill)**

After the PR opens, IMMEDIATELY return to the user with: "PR opened, ready for review. Once it's merged to main, run `/canopy:update` per the CLAUDE.md update protocol."

DO NOT merge to main from the agent — the user merges canopy PRs manually so they can review the autonomous-mode logic before it lands.

DO NOT bundle ace-web's `autonomous.yaml` into this PR — that's a separate PR per spec constraint #7.

---

## Self-review (writing-plans skill — done before saving)

**1. Spec coverage:** every section accounted for —
- §1 overall shape → SKILL.md "Two modes" + cycle.md + commands (Tasks 5, 6, 7, 8)
- §2 autonomous.yaml → config-schema.md + validator + tests (Tasks 3, 4)
- §3a mechanical checks → secret_scan.py + diff_size_check.py + tests (Tasks 1, 2)
- §3b self-review questions → convince-self-gate.md (Task 4) + structural test (Task 9)
- §3c dogfood → convince-self-gate.md (Task 4)
- §3d post-deploy health → convince-self-gate.md (Task 4) + cycle.md Phase C (Task 5)
- §4–§5 working-backwards cycle (Phases A–E) → cycle.md (Task 5)
- §6 commands → Tasks 7, 8
- Open questions resolved up front in this plan
- Acceptance criteria 1 covered by manual run after merge (out of unit-test scope; documented as Task 11 follow-up); 2 by Task 9 regression test; 3 by Task 1 tests; 4 by Task 2 tests; 5 by Task 9's structural test for the gate template + the gate's wording in convince-self-gate.md; 6 by Task 6.

**2. Placeholder scan:** no TBD/TODO/etc. left in. Code blocks contain the actual code engineers run.

**3. Type consistency:** script names match across plan, tests, and template references. `validate_autonomous_config.py`, `secret_scan.py`, `diff_size_check.py` used identically everywhere. Plugin path resolution code is identical wherever it appears.

Plan saved.
