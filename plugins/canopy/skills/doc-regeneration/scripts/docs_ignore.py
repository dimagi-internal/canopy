"""The ignore file both regen scripts read: `.docs-ignore` at the repo root.

check_refs.py and find_history.py cannot tell a broken reference from a file that
lives in another repo, or a history marker that must stay (an event name, a quoted
identifier). A
person says so once, in `.docs-ignore`, with the reason:

    # <tool>  <pattern>     # <reason>
    check_refs   packages/canopy_kb                 # lives in canopy-web
    check_refs   docs/old.md::dist/*                # build output, generated on deploy
    find_history docs/a.md                          # a whole doc whose dates are data
    find_history docs/a.md::2026-06-19              # only lines containing this text

  tool      check_refs | find_history | * (both)
  pattern   check_refs:   [DOC_GLOB::]TOKEN_GLOB  the token as written in the doc
                          (an `fnmatch` glob; no DOC_GLOB means every doc)
            find_history: DOC_GLOB[::TEXT]        a whole doc, or only the lines that
                          contain TEXT
  reason    required, after ` # `. An entry without one is an error.

Paths are relative to the repo root, with `/`. Stdlib only.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

NAME = ".docs-ignore"
TOOLS = {"check_refs", "find_history", "*"}
REASON = re.compile(r"\s+#\s+(\S.*)$")


@dataclass
class Rule:
    tool: str
    doc_glob: str
    pattern: str
    reason: str
    line: int
    hits: int = 0


class IgnoreError(ValueError):
    pass


class Ignores:
    def __init__(self, rules: list[Rule] | None = None, root: Path | None = None):
        self.rules = rules or []
        self.root = root

    def rel(self, doc: Path) -> str:
        try:
            return doc.resolve().relative_to(self.root).as_posix() if self.root else doc.as_posix()
        except ValueError:
            return doc.as_posix()

    def _applies(self, tool: str, doc_rel: str):
        for r in self.rules:
            if r.tool in (tool, "*") and fnmatch.fnmatchcase(doc_rel, r.doc_glob):
                yield r

    def ref(self, doc_rel: str, token: str) -> bool:
        """True if check_refs should skip this token in this doc."""
        bare = token.strip().rstrip(".,;:)")
        for r in self._applies("check_refs", doc_rel):
            if r.pattern and not any(fnmatch.fnmatchcase(t, r.pattern) for t in (token, bare)):
                continue
            r.hits += 1
            return True
        return False

    def history(self, doc_rel: str, line: str) -> bool:
        """True if find_history should skip this line of this doc."""
        for r in self._applies("find_history", doc_rel):
            if r.pattern and r.pattern not in line:
                continue
            r.hits += 1
            return True
        return False

    def unused(self, tool: str) -> list[Rule]:
        return [r for r in self.rules if r.tool in (tool, "*") and r.hits == 0]


def parse(text: str, root: Path | None = None) -> Ignores:
    rules: list[Rule] = []
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = REASON.search(raw)
        if not m:
            raise IgnoreError(f"{NAME}:{n}: every entry needs a reason after ' # ': {raw.strip()!r}")
        head = raw[: m.start()].split(None, 1)
        if len(head) != 2 or head[0] not in TOOLS:
            raise IgnoreError(f"{NAME}:{n}: expected '<check_refs|find_history|*> <pattern>  # <reason>': {raw.strip()!r}")
        tool, spec = head[0], head[1].strip()
        doc_glob, sep, pattern = spec.partition("::")
        if tool == "check_refs" and not sep:
            doc_glob, pattern = "*", spec  # a bare token applies to every doc
        rules.append(Rule(tool, doc_glob, pattern, m.group(1).strip(), n))
    return Ignores(rules, root)


def load(root: Path, path: Path | None = None) -> Ignores:
    """The ignore file at `path`, else `<root>/.docs-ignore`, else no rules."""
    f = path or root / NAME
    if not f.is_file():
        return Ignores([], root)
    return parse(f.read_text(encoding="utf-8"), root)


def report(ign: Ignores, tool: str) -> list[str]:
    """Lines to print after a run: what was ignored, and entries nothing needed."""
    out = []
    hits = sum(r.hits for r in ign.rules if r.tool in (tool, "*"))
    if ign.rules:
        out.append(f"ignored {hits} (per {NAME})")
    for r in ign.unused(tool):
        out.append(f"UNUSED {NAME}:{r.line} `{r.doc_glob}::{r.pattern}` — nothing matched; delete it if it is stale")
    return out
