#!/usr/bin/env python3
"""CI gate: every `canopy ...` command shown in an end-user page must exist.

Extracts `canopy <subcommand> ...` from inline code spans and fenced blocks in the
end-user pages (README.md, CONTRIBUTING.md, docs/*.md; not docs/superpowers,
docs/plans or docs/architecture), resolves each to a click command of the real
CLI, renders that command's `--help`, and fails when

  * the command does not exist, or
  * a long flag the doc shows (`--like-this`) is not in that command's help.

Run it from the repo root with the CLI importable (`uv run python
.github/scripts/docs_commands_check.py`). Placeholders and deliberate
non-commands go in `docs_commands_allowlist.yaml` next to this file, one entry
per command text with a reason.

URLs are not checked: a URL on canopy-web or GitHub is checked only against the
other repo's source, which this job does not have.

The pure parts (`extract_commands`, `resolve`) take plain inputs so the tests in
tests/test_docs_commands_check.py need no subprocess.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import click

REPO = Path(__file__).resolve().parents[2]
ALLOWLIST = Path(__file__).with_name("docs_commands_allowlist.yaml")
PAGES = ["README.md", "CONTRIBUTING.md", "docs/*.md"]

FENCE = re.compile(r"^\s*(```|~~~)")
INLINE = re.compile(r"`([^`\n]+)`")
# A command starts a code span or a line, optionally after a prompt.
START = re.compile(r"^(?:[$>]\s*|PS>\s*)?(?:uv run )?canopy(?=\s|$)")
# The command ends at a pipe, a redirect, a chained command or a substitution.
END = re.compile(r"\s(?:\||&&|;|>|2>|<)\s|\$\(|\s#\s")


@dataclass(frozen=True)
class Found:
    page: str
    line: int
    text: str  # the command as shown, cut at the first pipe/redirect/comment

    @property
    def tokens(self) -> list[str]:
        return self.text.split()[1:]


def _cut(text: str) -> str:
    m = END.search(text)
    return (text[: m.start()] if m else text).strip()


def _commands_in(text: str) -> list[str]:
    out = []
    t = text.strip()
    t = re.sub(r"^(?:[$>]\s*|PS>\s*)", "", t)
    t = re.sub(r"^uv run ", "", t)
    if START.match(text.strip()):
        out.append(_cut(t))
    return out


def extract_commands(page: str, markdown: str) -> list[Found]:
    """Every `canopy ...` command shown in code, with the line it starts on."""
    found: list[Found] = []
    in_fence = False
    pending: tuple[int, str] | None = None  # a fenced command continued with a backslash
    prose: list[tuple[int, str]] = []  # the current paragraph, so a span can wrap a line

    def flush_prose() -> None:
        if prose:
            joined = " ".join(t for _, t in prose)
            for span in INLINE.findall(joined):
                found.extend(Found(page, prose[0][0], c) for c in _commands_in(span))
            prose.clear()

    for n, line in enumerate(markdown.splitlines(), 1):
        if FENCE.match(line):
            flush_prose()
            in_fence = not in_fence
            pending = None
            continue
        if not in_fence:
            if line.strip():
                prose.append((n, line.strip()))
            else:
                flush_prose()
            continue
        stripped = line.strip()
        if pending:
            start, acc = pending
            acc += " " + stripped.rstrip("\\").strip()
            if stripped.endswith("\\"):
                pending = (start, acc)
                continue
            found.extend(Found(page, start, c) for c in _commands_in(acc))
            pending = None
            continue
        if START.match(stripped):
            if stripped.endswith("\\"):
                pending = (n, stripped.rstrip("\\").strip())
                continue
            found.extend(Found(page, n, c) for c in _commands_in(stripped))
        # A prompt pasted into a text block carries its commands in backticks.
        for span in INLINE.findall(line):
            found.extend(Found(page, n, c) for c in _commands_in(span))
    flush_prose()
    return found


def _flags(cmd: click.Command, ctx: click.Context) -> set[str]:
    flags = {"--help"}
    for p in cmd.get_params(ctx):
        flags.update(o for o in (*p.opts, *getattr(p, "secondary_opts", ())) if o.startswith("--"))
    return flags


def resolve(root: click.Group, tokens: list[str]) -> tuple[str | None, str | None]:
    """Walk `tokens` down the command tree.

    Returns (error, help_text). The help text is the resolved command's `--help`.
    """
    ctx = click.Context(root, info_name="canopy")
    cmd: click.Command = root
    path = ["canopy"]
    allowed = _flags(root, ctx)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-"):
            if tok.startswith("--"):
                flag = tok.split("=", 1)[0]
                if flag not in allowed:
                    return (
                        f"flag {flag} is not in `{' '.join(path)} --help`",
                        None,
                    )
            i += 1
            continue
        if isinstance(cmd, click.Group):
            sub = cmd.get_command(ctx, tok)
            if sub is not None:
                path.append(tok)
                ctx = click.Context(sub, info_name=tok, parent=ctx)
                cmd = sub
                allowed |= _flags(cmd, ctx)
                i += 1
                continue
            if re.fullmatch(r"[a-z][a-z0-9_-]*", tok) and len(path) >= 1:
                # A bare word where a subcommand belongs: either the command is gone
                # or this is a prose word / placeholder; the allowlist decides.
                return (f"`{' '.join(path)}` has no command `{tok}`", None)
        # a positional argument or placeholder: the command is resolved
        i += 1
    return None, cmd.get_help(ctx)


def load_allowlist(path: Path = ALLOWLIST) -> dict[str, str]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    out: dict[str, str] = {}
    for entry in data:
        cmd, reason = entry.get("command"), entry.get("reason")
        if not cmd or not reason or not str(reason).strip():
            raise SystemExit(f"{path.name}: every entry needs `command` and a `reason`: {entry!r}")
        out[" ".join(cmd.split())] = reason
    return out


def pages() -> list[Path]:
    out: list[Path] = []
    for pattern in PAGES:
        out.extend(sorted(REPO.glob(pattern)))
    return out


def check(root: click.Group, allow: dict[str, str]) -> tuple[list[str], set[str]]:
    errors: list[str] = []
    used: set[str] = set()
    for p in pages():
        rel = p.relative_to(REPO).as_posix()
        for f in extract_commands(rel, p.read_text(encoding="utf-8")):
            key = " ".join(f.text.split())
            if key in allow:
                used.add(key)
                continue
            err, _help = resolve(root, f.tokens)
            if err:
                errors.append(f"{rel}:{f.line}: `{key}` -> {err}")
    return errors, used


def main() -> int:
    from orchestrator.cli import main as root

    allow = load_allowlist()
    errors, used = check(root, allow)
    stale = sorted(set(allow) - used)
    if errors:
        print("Commands shown in end-user docs that the CLI does not have:\n")
        print("\n".join(f"  {e}" for e in errors))
        print(
            "\nFix the page so it shows what `canopy ... --help` says. A placeholder or a command"
            " that is deliberately not a canopy command goes in"
            f" .github/scripts/{ALLOWLIST.name} with a reason."
        )
    if stale:
        print("\nAllowlist entries no page shows any more (delete them):\n")
        print("\n".join(f"  {s}" for s in stale))
    if errors or stale:
        return 1
    print(f"docs-commands: every canopy command in {len(pages())} end-user pages exists.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
