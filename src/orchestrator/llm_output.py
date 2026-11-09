"""Parse an LLM's stdout into the YAML list the caller asked it for.

FRAMEWORK tier (see `TIERS.md`): generic, stdlib+yaml only, agent-agnostic. Every
`claude -p` gate in the tree asks for "a YAML list" and then has to cope with what a
model actually returns, which is routinely *a sentence, then a fenced block*.

This module is the one place that coping logic lives, shared by
`agent_review.parse_findings`, `analyzer.parse_analysis_output`,
`proposer.parse_proposal_output`, `verify_findings._parse_verdict_output` and
`harvest._looks_like_empty_yaml_list`. Stripping a fence only when the response
*starts* with it (`text.startswith("```")`) is not enough: a preamble before the block
would send the whole response to `yaml.safe_load` as prose, which returns a str, fails
the `isinstance(result, list)` check, and discards the completed verdict.

Why that matters enough to centralise: the callers are *gates*. A gate
that cannot read its own verdict does not fail loudly-and-wrongly, it declines to run —
`agent-review` then hands findings to the caller stamped UNVERIFIED. The gate's own
docstring calls a silent no-op "worse than no gate", and a parser that required a leading
fence would produce exactly that on the single most common answer shape there is: a verify
pass that did the work and reached a verdict would be thrown away because it introduced
the block with one sentence.

The tolerance is deliberately ordered, most-explicit first, so a well-formed answer is
never reinterpreted by a looser rule:

  1. the contents of each fenced block, in order of appearance;
  2. the whole response with fence lines removed (so a bare fenced answer still parses);
  3. the tail starting at the first YAML list item, for an answer that was never fenced.

Across those candidates a **list of mappings** wins over any other list, because that is
what every caller consumes; it is what separates a real verdict block from a prose
bullet list that merely happens to parse. Only if no candidate yields mappings do we
accept a bare list, which keeps the established contract (including a genuine empty `[]`,
which `harvest` must still be able to tell apart from garbage).

What is NOT tolerated, on purpose: a response with no parseable list at all still
returns `[]`, so every caller's loud "did not parse" path stays intact. The
point is to stop discarding verdicts that are *present*, never to invent one.
"""
from __future__ import annotations

import re

import yaml

__all__ = ["parse_yaml_list", "strip_code_fences"]

# A fenced block: ```<optional lang> … ``` — non-greedy so several blocks in one
# response are found separately rather than swallowed as a single span.
_FENCE_BLOCK_RE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.DOTALL | re.MULTILINE)


def strip_code_fences(text: str) -> str:
    """Drop every code-fence line, keeping the content between them.

    The legacy behaviour of the five call sites, minus the `startswith` guard that
    made it conditional on the fence coming first.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("```")
    ).strip()


def _load_list(text: str) -> list | None:
    """`yaml.safe_load` narrowed to lists. None = not a list / not parseable."""
    if not text:
        return None
    try:
        result = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return result if isinstance(result, list) else None


def _candidates(text: str) -> list[str]:
    """Substrings that might hold the list, most-explicit first (see module docstring)."""
    out: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip()
        if candidate and candidate not in out:
            out.append(candidate)

    for match in _FENCE_BLOCK_RE.finditer(text):
        add(match.group(1))

    defenced = strip_code_fences(text)
    add(defenced)

    # Un-fenced preamble: resume at the first line that opens a YAML list item. Scanned
    # over the DEFENCED text so a trailing ``` can't ride along and break the parse.
    lines = defenced.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "-" or stripped.startswith("- "):
            add("\n".join(lines[i:]))
            break

    return out


def parse_yaml_list(output: str) -> list[dict]:
    """Extract the YAML list an LLM was asked for, tolerating a preamble before it.

    Returns `[]` when nothing parseable is present — callers rely on that to
    distinguish "no verdict" from a verdict, and surface it loudly.
    """
    text = (output or "").strip()
    if not text:
        return []

    candidates = _candidates(text)
    parsed = [(_load_list(c)) for c in candidates]

    # A list of mappings is what every caller consumes — prefer it over a list that
    # merely parsed (e.g. prose bullets), regardless of where each was found.
    for result in parsed:
        if result and all(isinstance(item, dict) for item in result):
            return result

    # Legacy contract: any list, including a genuine empty one.
    for result in parsed:
        if result is not None:
            return result

    return []
