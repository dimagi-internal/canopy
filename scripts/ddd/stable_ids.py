"""Undo per-render ``${var}`` substitution so two renders can be compared.

A state-mutating narrative reseeds its data every take (``setup: rerun:
per_render``), so every minted id changes between renders: ``${round2_tender_id}``
is 92 in one take and 94 in the next. Anything that compares one render with the
previous one — the judge-scope fingerprints, the regression guard — saw every
id-bearing action and page as "changed", which meant a state-mutating narrative
could never reuse a single judged cell (canopy 0.2.528, supply-sophie-rutf: 7/7
scenes re-judged every iteration) and the regression guard warned on ~10
"disappeared" actions every iteration that had not gone anywhere.

The fix is to compare what the SPEC said, not what it resolved to. The render
report already carries the substitution map — ``setup.variables`` (minted by the
setup command) plus every ``capture`` action's ``capture_var``/``capture_value``
(bound mid-render) — so a resolved string can be turned back into its spec form
by replacing each bound value with its ``${name}`` placeholder.

Fidelity rule: this can only ever make two renders look MORE alike, so every
replacement is conservative.

* Only whole tokens are replaced (``94`` in ``tenders/94/`` — never the ``94`` in
  ``1942``), and only values of at least two characters.
* Page TEXT (what a judge reads) is normalised for id-shaped variables only
  (``*_id`` / ``*id``): a dashboard figure that happens to equal a date or a
  count variable must still register as a change. Action targets (selectors and
  URLs the recorder resolved from the spec) are normalised for every variable.
* Screenshots are NOT normalised: pixels cannot be un-substituted, so a scene that
  shows a reseeded id on screen is re-judged every iteration. That is the safe
  failure (a re-judge costs tokens; a false reuse ships a stale score).

Stdlib only.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

MIN_VALUE_LEN = 2
_ID_NAME = re.compile(r"(?:^|_)(?:id|ID|Id)s?$|[a-z0-9]Ids?$")


def _scalar(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, str)):
        text = str(value).strip()
        return text if len(text) >= MIN_VALUE_LEN else None
    return None


def resolved_vars(report: dict | None) -> dict[str, str]:
    """``{name: value}`` bound during a render, from its run report.

    ``setup.variables`` first, then each ``capture`` action's output (a capture
    that re-binds a setup name wins, matching the recorder's late binding).
    Non-scalar and too-short values are dropped — they cannot be matched safely.
    """
    out: dict[str, str] = {}
    if not isinstance(report, dict):
        return out
    setup = report.get("setup") or {}
    for name, value in ((setup.get("variables") or {}) if isinstance(setup, dict) else {}).items():
        text = _scalar(value)
        if text is not None:
            out[str(name)] = text
    for action in report.get("actions") or []:
        if not isinstance(action, dict) or not action.get("capture_var"):
            continue
        text = _scalar(action.get("capture_value"))
        if text is not None:
            out[str(action["capture_var"])] = text
    return out


def scene_vars(report: dict | None, scene: int | str) -> dict[str, str]:
    """The bindings scene ``scene`` was FILMED with.

    A scene-scoped capture (canopy#785) re-films a few scenes and carries the
    rest from an earlier take, whose reseed bound different ids; the merged
    report keeps each carried scene's bindings in ``scene_variables``. Every
    other scene was filmed with this take's :func:`resolved_vars`.
    """
    per = (report or {}).get("scene_variables") if isinstance(report, dict) else None
    if isinstance(per, dict) and isinstance(per.get(str(scene)), dict):
        return {str(k): str(v) for k, v in per[str(scene)].items()}
    return resolved_vars(report)


def id_vars(variables: dict[str, str]) -> dict[str, str]:
    """The subset of *variables* whose NAME says it holds an id."""
    return {k: v for k, v in variables.items() if _ID_NAME.search(k)}


def _ordered(variables: dict[str, str]) -> Iterable[tuple[str, str]]:
    # Longest value first so "1094" is placed before "94"; name breaks ties so
    # two renders that bind the same value to two names normalise identically.
    return sorted(variables.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def unsubstitute(text: Any, variables: dict[str, str]) -> Any:
    """Replace each whole-token occurrence of a bound value with ``${name}``.

    Non-strings pass through unchanged. Placeholders already present are left
    alone (a value is never matched inside ``${...}``).
    """
    if not isinstance(text, str) or not variables:
        return text
    out = text
    for name, value in _ordered(variables):
        pattern = re.compile(
            r"(?<![A-Za-z0-9_{])" + re.escape(value) + r"(?![A-Za-z0-9_}])"
        )
        out = pattern.sub("${" + name + "}", out)
    return out


def unsubstitute_deep(obj: Any, variables: dict[str, str]) -> Any:
    """:func:`unsubstitute` over every string inside a JSON-shaped value."""
    if isinstance(obj, str):
        return unsubstitute(obj, variables)
    if isinstance(obj, list):
        return [unsubstitute_deep(x, variables) for x in obj]
    if isinstance(obj, dict):
        return {k: unsubstitute_deep(v, variables) for k, v in obj.items()}
    return obj


__all__ = ["id_vars", "resolved_vars", "scene_vars", "unsubstitute", "unsubstitute_deep"]
