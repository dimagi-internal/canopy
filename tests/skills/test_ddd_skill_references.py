"""Drift gate: every scripts.* module / attribute / rubric file referenced by a
plugin markdown file must actually resolve (canopy#265 item 5).

The per-skill structure tests pin load-bearing STRINGS ("SKILL.md mentions
scripts.ddd.spec_qa") but never check the reference RESOLVES — renaming a
scripts.ddd module or a rubric file keeps every structure test green while the
skill breaks at runtime. This test closes that gap:

  * ``python -m scripts.x.y`` invocations  -> module must import
  * ``from scripts.x.y import a, b``       -> module must import AND expose a, b
  * ``skills/<name>/rubric.yaml`` paths    -> file must exist in the repo plugin

Scope: all markdown under plugins/canopy/ (skills, commands, agents).
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "canopy"

# `python -m scripts.x.y`, `python3 -m ...`, `uv run python -m ...`
_DASH_M_RE = re.compile(r"python3?\s+-m\s+(scripts(?:\.[A-Za-z0-9_]+)+)")
# `from scripts.x.y import a, b` — single-line, or parenthesized multi-line
_FROM_IMPORT_RE = re.compile(
    r"from[ \t]+(scripts(?:\.[A-Za-z0-9_]+)+)[ \t]+import[ \t]+"
    r"(\([^)]+\)|[A-Za-z0-9_,\t ]+)"
)
# `skills/<name>/rubric.yaml`
_RUBRIC_RE = re.compile(r"skills/([a-z0-9-]+)/rubric\.yaml")


def _module_refs(text: str) -> set[str]:
    """All `python -m scripts.*` module paths referenced in *text*."""
    return {m.rstrip(".") for m in _DASH_M_RE.findall(text)}


def _import_refs(text: str) -> list[tuple[str, list[str]]]:
    """All `from scripts.* import ...` references as (module, [attrs])."""
    refs = []
    for module, attrs_raw in _FROM_IMPORT_RE.findall(text):
        attrs = [a.strip() for a in attrs_raw.strip("()").split(",") if a.strip()]
        refs.append((module.rstrip("."), attrs))
    return refs


def _rubric_refs(text: str) -> set[str]:
    """All skill names whose rubric.yaml is referenced in *text*."""
    return set(_RUBRIC_RE.findall(text))


def _check_text(text: str) -> list[str]:
    """Return a list of unresolvable-reference problems found in *text*."""
    problems: list[str] = []
    for module in sorted(_module_refs(text)):
        try:
            importlib.import_module(module)
        except ImportError as exc:
            problems.append(f"`python -m {module}` does not import: {exc}")
    for module, attrs in _import_refs(text):
        try:
            mod = importlib.import_module(module)
        except ImportError as exc:
            problems.append(f"`from {module} import ...` does not import: {exc}")
            continue
        for attr in attrs:
            if hasattr(mod, attr):
                continue
            # `from scripts.ddd import gates` names a SUBMODULE, which is not an
            # attribute of the package until something imports it.
            try:
                importlib.import_module(f"{module}.{attr}")
            except ImportError:
                problems.append(f"`from {module} import {attr}`: no such attribute")
    for skill in sorted(_rubric_refs(text)):
        if not (PLUGIN / "skills" / skill / "rubric.yaml").exists():
            problems.append(f"referenced skills/{skill}/rubric.yaml does not exist")
    return problems


def _md_files() -> list[Path]:
    files = sorted(PLUGIN.glob("skills/*/SKILL.md"))
    files += sorted(PLUGIN.glob("commands/*.md"))
    files += sorted(PLUGIN.glob("agents/*.md"))
    return files


# ---------------------------------------------------------------------------
# Extractor / checker unit tests (prove the gate can actually catch drift)
# ---------------------------------------------------------------------------


def test_extracts_dash_m_modules() -> None:
    text = "run `uv run python -m scripts.ddd.spec_qa spec.yaml` then python3 -m scripts.ddd.why_qa"
    assert _module_refs(text) == {"scripts.ddd.spec_qa", "scripts.ddd.why_qa"}


def test_dash_m_placeholder_does_not_break_extraction() -> None:
    # docs write `python -m scripts.ddd.<mod>` — the placeholder must degrade to
    # the importable parent package, not a bogus module path
    assert _module_refs("python -m scripts.ddd.<mod>") == {"scripts.ddd"}


def test_extracts_from_imports_with_attrs() -> None:
    text = "from scripts.ddd.run_pipeline import assemble_run_state, compute_convergence"
    assert _import_refs(text) == [
        ("scripts.ddd.run_pipeline", ["assemble_run_state", "compute_convergence"])
    ]


def test_extracts_rubric_paths() -> None:
    text = "read skills/ddd-why-eval/rubric.yaml for anchors"
    assert _rubric_refs(text) == {"ddd-why-eval"}


def test_detects_unresolvable_module() -> None:
    assert _check_text("python -m scripts.ddd.does_not_exist_xyz")


def test_detects_missing_attribute() -> None:
    assert _check_text("from scripts.ddd.run_pipeline import no_such_function_xyz")


def test_submodule_import_resolves() -> None:
    assert not _check_text("from scripts.ddd import gates")


def test_detects_missing_submodule() -> None:
    assert _check_text("from scripts.ddd import no_such_module_xyz")


def test_detects_missing_rubric() -> None:
    assert _check_text("see skills/no-such-skill-xyz/rubric.yaml")


# ---------------------------------------------------------------------------
# The sweep: every plugin markdown file's references must resolve
# ---------------------------------------------------------------------------


def test_all_plugin_markdown_references_resolve() -> None:
    failures: list[str] = []
    for md in _md_files():
        for problem in _check_text(md.read_text()):
            failures.append(f"{md.relative_to(REPO)}: {problem}")
    assert not failures, "unresolvable references:\n  " + "\n  ".join(failures)
