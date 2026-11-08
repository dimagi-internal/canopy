"""What `canopy ... --help` prints, and the docstring at the top of a CLI module, is current state.

Same rule as CLAUDE.md (tests/test_claude_md_budget.py): a date, a PR/issue reference or a
person's name says WHEN or WHO decided something, which is history, and history lives in the
PR description and the commit log. Help text is read by people and agents deciding what to
run; a note that "Shayoni hit this on 2026-09-29" teaches them nothing they can act on.

Covers, for every command in the tree: its help, its short help, each option's help (and its
shown default). And the module docstring of every module that defines a command or opens with
a "`canopy ...`" line (agent_use.py has no command of its own).
"""
from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re

import click
import pytest

import orchestrator
from orchestrator.cli import main

# CLAUDE.md's rule, plus ISO dates anywhere and the people this repo's history names.
HISTORY = re.compile(
    r"\b(?:19|20)\d\d-\d\d-\d\d\b|\(#\d+\)|\bPR #?\d+|[\w-]+#\d{2,}|\bissue #\d+|\bcanopy-web #\d+"
)
PEOPLE = re.compile(
    r"\b(?:Jonathan|Jon|Jackson|Shayoni|Mazumdar|Matthew|Matt|Hayto|Theis|Mary|Rocheleau|"
    r"Gillian|Javetski|Amie|Vaccaro|Andrea)\b|\bjj-mbp[\w-]*\b"
)


def _walk(cmd: click.Command, path: tuple[str, ...]):
    yield path, cmd
    if isinstance(cmd, click.Group):
        ctx = click.Context(cmd)
        for name in cmd.list_commands(ctx):
            yield from _walk(cmd.get_command(ctx, name), (*path, name))


def _help_texts():
    for path, cmd in _walk(main, ("canopy",)):
        label = " ".join(path)
        yield f"{label} (help)", cmd.help or ""
        yield f"{label} (short help)", cmd.short_help or ""
        for p in cmd.params:
            yield f"{label} {p.opts[0] if p.opts else p.name} (option help)", getattr(p, "help", None) or ""
            if getattr(p, "show_default", False) and isinstance(getattr(p, "default", None), str):
                yield f"{label} {p.opts[0]} (shown default)", p.default


def _cli_modules() -> dict[str, str]:
    mods: dict[str, str] = {}
    for _path, cmd in _walk(main, ("canopy",)):
        cb = getattr(cmd, "callback", None)
        if cb is not None and cb.__module__.startswith("orchestrator."):
            mods[cb.__module__] = ""
    for info in pkgutil.iter_modules(orchestrator.__path__):
        name = f"orchestrator.{info.name}"
        try:
            doc = ast.get_docstring(ast.parse(inspect.getsource(importlib.import_module(name)))) or ""
        except Exception:  # an optional dependency missing: not a CLI module we can read
            continue
        if doc.lstrip().startswith("`canopy "):
            mods[name] = ""
    return dict.fromkeys(sorted(mods))


def _offences(where: str, text: str) -> list[str]:
    return [f"{where}: {m.group(0)!r}" for rx in (HISTORY, PEOPLE) for m in rx.finditer(text)]


def test_help_text_is_current_state():
    bad = [o for where, text in _help_texts() for o in _offences(where, text)]
    assert not bad, (
        "`--help` text names a date, a PR/issue or a person (history, not current behaviour):\n  "
        + "\n  ".join(bad)
        + "\nState the rule as it holds now; put when, why and who in the PR description."
    )


def test_cli_module_docstrings_are_current_state():
    bad = []
    for name in _cli_modules():
        doc = ast.get_docstring(ast.parse(inspect.getsource(importlib.import_module(name)))) or ""
        bad += _offences(f"{name} (module docstring)", doc)
    assert not bad, (
        "A CLI module's docstring names a date, a PR/issue or a person:\n  " + "\n  ".join(bad)
        + "\nState the rule as it holds now; put when, why and who in the PR description."
    )


def test_agent_use_is_covered():
    assert "orchestrator.agent_use" in _cli_modules()


@pytest.mark.parametrize("text", ["fixed (Shayoni, 2026-09-29)", "see canopy#516", "on 2026-07-25", "jj-mbp-cdp"])
def test_the_patterns_catch_what_they_are_for(text):
    assert _offences("x", text)


@pytest.mark.parametrize("text", ["the agent's Settings → Security tab", "version 1.2.3", "run `canopy agent use x`"])
def test_the_patterns_leave_current_state_alone(text):
    assert not _offences("x", text)
