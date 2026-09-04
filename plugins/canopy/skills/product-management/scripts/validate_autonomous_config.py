# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Validate `<repo>/.canopy/pm/autonomous.yaml` (spec §2, Phase 0).

Usage: uv run --script validate_autonomous_config.py <path/to/autonomous.yaml>

Exit 0 + 'ready: <project>' on success.
Exit 1 + per-error stderr on failure.

The PEP 723 inline-metadata block above lets `uv run --script` resolve PyYAML
on the fly, so the autonomous skill can invoke this from any project without
assuming the user's system Python has yaml installed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    print(
        "validate-config: PyYAML not available. Run via `uv run --script` "
        "(uv reads the inline metadata at the top of this file), or install "
        "pyyaml into the python on PATH.",
        file=sys.stderr,
    )
    sys.exit(1)

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

_MISSING = object()


def _get(cfg: Any, dotted: str) -> Any:
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def validate(cfg: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return ["root: expected mapping"]

    for key, expected_type in REQUIRED:
        value = _get(cfg, key)
        if value is _MISSING:
            errors.append(f"{key}: missing")
            continue
        # Guard: bool is a subclass of int in Python, so `isinstance(True, int)` is
        # True. For int fields that must not be booleans, reject bool values first.
        if expected_type is int and isinstance(value, bool):
            errors.append(
                f"{key}: expected int, got bool"
            )
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
    if isinstance(diff_limit, int) and not isinstance(diff_limit, bool) and diff_limit <= 0:
        errors.append("guardrails.diff_size_limit_lines: must be positive")

    fix_attempts = _get(cfg, "guardrails.max_fix_forward_attempts")
    if isinstance(fix_attempts, int) and not isinstance(fix_attempts, bool) and fix_attempts <= 0:
        errors.append("guardrails.max_fix_forward_attempts: must be positive")

    health = _get(cfg, "shipping.post_deploy_health")
    if health is _MISSING or not isinstance(health, list) or not health:
        errors.append("shipping.post_deploy_health: must be a non-empty list of URLs")
    elif not all(isinstance(h, str) and h for h in health):
        errors.append("shipping.post_deploy_health: every entry must be a non-empty string")

    lenses = _get(cfg, "theme_detection.lens_rotation")
    if lenses is _MISSING or not isinstance(lenses, list) or not lenses:
        errors.append("theme_detection.lens_rotation: must be a non-empty list")

    prepare = _get(cfg, "testing.prepare")
    if prepare is not _MISSING and not (isinstance(prepare, str) and prepare):
        errors.append("testing.prepare: if present, must be a non-empty string")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()

    if not args.path.exists():
        print(f"validate-config: file not found: {args.path}", file=sys.stderr)
        return 1
    try:
        cfg = yaml.safe_load(args.path.read_text(encoding="utf-8"))
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
