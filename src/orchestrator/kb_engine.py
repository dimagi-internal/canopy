"""The knowledge-base engine behind `canopy kb build` and `canopy kb mcp`.

There is one engine, canopy-web's `packages/canopy_kb` (stdlib-only): canopy-web serves
every published KB with it, and these two commands import it. canopy used to carry its
own copy, and the two drifted (passage size, schema, tools). Only a curator who builds,
or someone serving a KB offline, needs it installed; `push` and `pull` only move a file,
and check it here without the engine.
"""
from __future__ import annotations

import functools
import json
import sqlite3
from pathlib import Path

INSTALL_HINT = (
    "the knowledge-base engine (canopy_kb, canopy-web's packages/canopy_kb) is not installed in canopy's "
    "environment. Install it alongside canopy, e.g.\n"
    "  uv tool install --reinstall <canopy checkout or package> "
    "--with 'canopy-kb @ git+ssh://git@github.com/dimagi-internal/canopy-web.git#subdirectory=packages/canopy_kb'\n"
    "or, from a canopy-web checkout: uv tool install --reinstall <canopy> --with <canopy-web>/packages/canopy_kb")


class KBError(RuntimeError):
    pass


def engine():
    """The `canopy_kb` package, or a KBError that says how to install it."""
    try:
        import canopy_kb
    except ImportError as e:
        raise KBError(INSTALL_HINT) from e
    return canopy_kb


def tools():
    """`canopy_kb.tools`: the MCP tool specs and dispatcher canopy-web serves."""
    engine()
    import canopy_kb.tools as t
    return t


def errors_as_kberror(fn):
    """The engine raises its own KBError; the CLI reports ours."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except KBError:
            raise
        except Exception as e:
            if type(e).__name__ == "KBError":
                raise KBError(str(e)) from e
            raise
    return wrapper


@errors_as_kberror
def build(bundle: Path, out: Path, previous: Path | None = None) -> dict:
    return engine().build(bundle, out, previous)


@errors_as_kberror
def open_file(path: Path):
    return engine().KB(path)


def summary(path: Path) -> dict:
    """A built KB's summary, read without the engine: enough to refuse a file that is
    not a knowledge base before uploading it or replacing a cache with it."""
    try:
        db = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
        try:
            row = db.execute("SELECT value FROM meta WHERE key = 'summary'").fetchone()
        finally:
            db.close()
    except sqlite3.DatabaseError as e:
        raise KBError(f"{path} is not a knowledge base ({e})") from e
    if not row:
        raise KBError(f"{path} is not a knowledge base (no build summary)")
    return json.loads(row[0])
