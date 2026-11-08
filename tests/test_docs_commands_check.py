"""The docs-commands CI gate: commands shown in end-user pages exist in the CLI.

The last test runs the gate on the real pages and the real CLI, so a flag or
command that disappears from the CLI while a page still shows it fails here and
in the `docs-commands` job.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import click
import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PATH = _ROOT / ".github" / "scripts" / "docs_commands_check.py"
_spec = importlib.util.spec_from_file_location("docs_commands_check", _PATH)
assert _spec is not None and _spec.loader is not None
dcc = importlib.util.module_from_spec(_spec)
sys.modules["docs_commands_check"] = dcc
_spec.loader.exec_module(dcc)


@click.group()
def _root():
    pass


@_root.group()
@click.option("--verbose", is_flag=True)
def agent():
    pass


@agent.command()
@click.option("--dry-run/--no-dry-run", default=False)
@click.option("--slug")
def move(dry_run, slug):
    pass


def _texts(md: str) -> list[str]:
    return [f.text for f in dcc.extract_commands("x.md", md)]


def test_extracts_inline_fenced_wrapped_and_prompt_commands():
    md = (
        "Run `canopy agent move --slug x` now, then `uv run canopy doctor`.\n"
        "A span that wraps `canopy vault zk set\n--agent a NAME` lines.\n\n"
        "```bash\n$ canopy agent move \\\n  --slug x --dry-run\ncanopy login | tee out\n# canopy nope\n```\n"
        "```text\nRun `canopy agent use <slug>` then stop\n```\n"
    )
    assert _texts(md) == [
        "canopy agent move --slug x",
        "canopy doctor",
        "canopy vault zk set --agent a NAME",
        "canopy agent move --slug x --dry-run",
        "canopy login",
        "canopy agent use <slug>",
    ]


def test_prose_is_not_a_command():
    assert _texts("canopy itself misbehaving; the canopy plugin. `not canopy agent`\n") == []


def test_resolves_commands_and_flags():
    ok = lambda s: dcc.resolve(_root, s.split()[1:])[0]
    assert ok("canopy agent move --slug x --dry-run <slug>") is None
    assert ok("canopy agent move --no-dry-run") is None
    assert ok("canopy agent --verbose move") is None
    assert "no command `gone`" in ok("canopy agent gone")
    assert "--old-flag" in ok("canopy agent move --old-flag")
    assert "no command `nope`" in ok("canopy nope")


def test_allowlist_needs_a_reason(tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text("- command: canopy x y\n")
    with pytest.raises(SystemExit):
        dcc.load_allowlist(f)
    f.write_text("- command: canopy  x   y\n  reason: a placeholder\n")
    assert dcc.load_allowlist(f) == {"canopy x y": "a placeholder"}


def test_every_allowlist_entry_has_a_reason_and_is_used():
    from orchestrator.cli import main

    allow = dcc.load_allowlist()
    _errors, used = dcc.check(main, allow)
    assert set(allow) <= used, f"stale allowlist entries: {sorted(set(allow) - used)}"


def test_end_user_pages_show_only_commands_the_cli_has():
    from orchestrator.cli import main

    errors, _used = dcc.check(main, dcc.load_allowlist())
    assert not errors, "\n".join(errors)
