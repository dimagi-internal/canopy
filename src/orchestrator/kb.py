"""Knowledge bases: a shared, cited library an agent curates and every agent queries.

A knowledge base (KB) is everything one publisher has made public — a funder's grants,
reports, podcast transcripts, cost-effectiveness models — kept as dated copies, with
the tables we compute with alongside. It belongs to a workspace PROJECT, not to any one
agent: the curator agent builds it, and every agent in the workspace reads the same
copy through the same tools (`canopy kb …` or `canopy kb mcp`).

Two artifacts, one direction:

  bundle/  (what a curator's adapters write — plain files, diffable)
    manifest.json        name, publisher, license, tables{name: {description, date_column,
                         amount_column, filter_columns}}, search_boost{type: factor}
                         (rank the publisher's own analysis above documents it merely hosts)
    items.jsonl          one source item per line: id, type, title, url, date, text,
                         meta, sha256, fetched_at
    tables/<name>.jsonl  structured rows (grants, model parameters, …)

  kb.sqlite (what `build` makes from a bundle and every reader queries)
    items, chunks + chunks_fts (FTS5, bm25), table_rows, changes, meta

`build` diffs against the previous kb.sqlite by each item's sha256, so `changes`
accumulates a dated history of what appeared or changed — that is what `whats_new`
answers, and it survives rebuilds because each build carries the old rows forward.

Every answer carries the item's source URL. A KB holds only what the publisher has
published; relationship notes about the publisher live elsewhere and never merge in.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

CHUNK_CHARS = 1800
# Passages are stored ONCE (they are the item's text: `get` rejoins them) and indexed by an
# external-content FTS table, so a passage costs its text plus its index entry. Storing the
# item's text, then each chunk, then FTS's own copy made the first GiveWell build 1.04 GB.
SCHEMA_VERSION = 2


class KBError(RuntimeError):
    pass


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = 0) -> list[str]:
    """Split on paragraph boundaries into ~size-char passages (optionally overlapping)."""
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    paras = re.split(r"\n\s*\n", text)
    chunks, cur = [], ""
    for p in paras:
        p = p.strip()
        if not p:
            continue
        while len(p) > size:  # one huge paragraph (a transcript, a PDF page)
            head, p = p[:size], p[size - overlap:]
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(head)
        if len(cur) + len(p) + 2 > size and cur:
            chunks.append(cur)
            cur = cur[-overlap:] + "\n\n" + p if overlap else p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def _read_jsonl(path: Path) -> Iterable[dict]:
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise KBError(f"{path}:{n}: not JSON ({e})") from e


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE items (
  id TEXT PRIMARY KEY, type TEXT, title TEXT, url TEXT, date TEXT,
  sha256 TEXT, fetched_at TEXT, first_seen TEXT, meta TEXT, chars INTEGER);
CREATE INDEX items_type ON items(type);
CREATE INDEX items_date ON items(date);
CREATE TABLE chunks (id INTEGER PRIMARY KEY, item_id TEXT, ord INTEGER, title TEXT, body TEXT);
CREATE INDEX chunks_item ON chunks(item_id, ord);
CREATE VIRTUAL TABLE chunks_fts USING fts5(title, body, content='chunks', content_rowid='id',
                                           tokenize='porter unicode61');
CREATE TABLE table_rows (tbl TEXT, n INTEGER, data TEXT);
CREATE INDEX table_rows_tbl ON table_rows(tbl);
CREATE TABLE changes (at TEXT, item_id TEXT, change TEXT, type TEXT, title TEXT, url TEXT, date TEXT);
CREATE INDEX changes_at ON changes(at);
"""


def build(bundle: Path, out: Path, previous: Path | None = None) -> dict:
    """Build kb.sqlite from a bundle directory. Returns a summary dict."""
    bundle = Path(bundle)
    manifest_path = bundle / "manifest.json"
    if not manifest_path.exists():
        raise KBError(f"{bundle}: no manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items_path = bundle / "items.jsonl"
    if not items_path.exists():
        raise KBError(f"{bundle}: no items.jsonl")

    prev: dict[str, tuple[str, str]] = {}
    prev_changes: list[tuple] = []
    if previous and Path(previous).is_file() and Path(previous).stat().st_size:
        pc = sqlite3.connect(f"file:{previous}?mode=ro", uri=True)
        try:
            prev = {r[0]: (r[1], r[2]) for r in pc.execute("SELECT id, sha256, first_seen FROM items")}
            prev_changes = list(pc.execute("SELECT at, item_id, change, type, title, url, date FROM changes"))
        except sqlite3.DatabaseError as e:
            raise KBError(f"--previous {previous} is not a knowledge base ({e})") from e
        finally:
            pc.close()
    else:
        previous = None

    tmp = Path(str(out) + ".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    now = _now()
    counts: dict[str, int] = {}
    new = changed = 0
    seen: set[str] = set()
    for it in _read_jsonl(items_path):
        iid = it.get("id")
        if not iid or iid in seen:
            continue
        seen.add(iid)
        text = it.get("text") or ""
        sha = it.get("sha256") or ""
        old = prev.get(iid)
        first_seen = old[1] if old else now
        if previous and not old:
            new += 1
            prev_changes.append((now, iid, "new", it.get("type"), it.get("title"), it.get("url"), it.get("date")))
        elif old and sha and old[0] != sha:
            changed += 1
            prev_changes.append((now, iid, "changed", it.get("type"), it.get("title"), it.get("url"), it.get("date")))
        db.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)", (
            iid, it.get("type", ""), it.get("title", ""), it.get("url", ""), it.get("date", "") or "",
            sha, it.get("fetched_at", ""), first_seen, json.dumps(it.get("meta") or {}), len(text)))
        # the title is indexed on the first passage only: a match on it should surface the
        # item once, not lift every passage of a long document
        db.executemany("INSERT INTO chunks(item_id, ord, title, body) VALUES (?,?,?,?)",
                       [(iid, i, it.get("title", "") if i == 0 else "", ch)
                        for i, ch in enumerate(chunk_text(text) or [""])])
        counts[it.get("type", "")] = counts.get(it.get("type", ""), 0) + 1

    tables: dict[str, int] = {}
    tdir = bundle / "tables"
    for name in (manifest.get("tables") or {}):
        p = tdir / f"{name}.jsonl"
        if not p.exists():
            raise KBError(f"manifest declares table {name!r} but {p} is missing")
        rows = list(_read_jsonl(p))
        db.executemany("INSERT INTO table_rows VALUES (?,?,?)", [(name, i, json.dumps(r)) for i, r in enumerate(rows)])
        tables[name] = len(rows)

    db.executemany("INSERT INTO changes VALUES (?,?,?,?,?,?,?)", prev_changes)
    summary = {"schema": SCHEMA_VERSION, "built_at": now, "name": manifest.get("name", ""),
               "items": len(seen), "by_type": counts, "tables": tables,
               "new": new, "changed": changed, "baseline": not bool(previous and prev)}
    db.executemany("INSERT INTO meta VALUES (?,?)", [
        ("manifest", json.dumps(manifest)), ("summary", json.dumps(summary))])
    db.commit()
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    tmp.replace(out)
    return summary


class KB:
    """Read side. Every method returns plain dicts with the source URL on each row."""

    def __init__(self, path: Path):
        path = Path(path)
        if not path.exists():
            raise KBError(f"{path}: no such knowledge base (run `canopy kb pull` first)")
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        m = dict(self.db.execute("SELECT key, value FROM meta").fetchall())
        self.manifest = json.loads(m.get("manifest", "{}"))
        self.summary = json.loads(m.get("summary", "{}"))

    # -- discovery -------------------------------------------------------------------
    def info(self) -> dict:
        return {"manifest": self.manifest, "summary": self.summary}

    # -- search ----------------------------------------------------------------------
    @staticmethod
    def _fts_query(q: str, mode: str) -> str:
        phrases = re.findall(r'"([^"]+)"', q)
        rest = re.sub(r'"[^"]+"', " ", q)
        terms = [t for t in re.findall(r"[\w$%.-]+", rest) if len(t) > 1 or t.isdigit()]
        parts = ['"' + p.replace('"', "") + '"' for p in phrases]
        parts += ['"' + t.strip(".-") + '"' for t in terms if t.strip(".-")]
        return f" {mode} ".join(parts)

    def search(self, query: str, types: list[str] | None = None, since: str = "",
               until: str = "", limit: int = 10) -> list[dict]:
        """Best passages for a question. AND first; falls back to OR when that is too narrow."""
        if not query.strip():
            raise KBError("empty query")
        out: list[dict] = []
        seen: set[tuple] = set()
        for mode in ("AND", "OR"):
            fq = self._fts_query(query, mode)
            if not fq:
                break
            sql = ("SELECT c.item_id, c.ord, i.type, i.title, i.url, i.date, "
                   "snippet(chunks_fts, 1, '«', '»', ' … ', 40) AS snip, bm25(chunks_fts, 5.0, 1.0) AS score "
                   "FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid JOIN items i ON i.id = c.item_id "
                   "WHERE chunks_fts MATCH ?")
            args: list[Any] = [fq]
            if types:
                sql += f" AND i.type IN ({','.join('?' * len(types))})"
                args += types
            if since:
                sql += " AND i.date >= ?"
                args.append(since)
            if until:
                sql += " AND i.date <= ?"
                args.append(until)
            boost = self.manifest.get("search_boost") or {}
            if boost:  # bm25 is negative (lower = better), so a factor > 1 ranks a type higher
                case = " ".join("WHEN ? THEN ?" for _ in boost)
                sql = f"SELECT * FROM ({sql}) ORDER BY score * (CASE type {case} ELSE 1.0 END) LIMIT ?"
                for t, f in boost.items():
                    args += [t, float(f)]
            else:
                sql += " ORDER BY score LIMIT ?"
            args.append(limit * 4)
            for r in self.db.execute(sql, args):
                key = (r["item_id"],)
                if key in seen:
                    continue  # one passage per item keeps the list diverse
                seen.add(key)
                out.append({"id": r["item_id"], "passage": r["ord"], "type": r["type"], "title": r["title"],
                            "url": r["url"], "date": r["date"], "snippet": r["snip"]})
                if len(out) >= limit:
                    return out
        return out

    # -- items -----------------------------------------------------------------------
    def get(self, ref: str, max_chars: int = 20000, passage: int | None = None,
            grep: str = "", context: int = 1) -> dict:
        r = self.db.execute("SELECT * FROM items WHERE id = ? OR url = ? LIMIT 1", (ref, ref)).fetchone()
        if not r:
            raise KBError(f"no item {ref!r}")
        d = dict(r)
        d["meta"] = json.loads(d["meta"] or "{}")
        if passage is not None:
            c = self.db.execute("SELECT body FROM chunks WHERE item_id = ? AND ord = ?", (d["id"], passage)).fetchone()
            d["text"] = c[0] if c else ""
        else:
            d["text"] = "\n\n".join(b for (b,) in self.db.execute(
                "SELECT body FROM chunks WHERE item_id = ? ORDER BY ord", (d["id"],)))
        d["passages"] = self.db.execute("SELECT COUNT(*) FROM chunks WHERE item_id = ?", (d["id"],)).fetchone()[0]
        if grep:  # verify a quote or a number without pulling the whole item into context
            try:
                rx = re.compile(grep, re.I)
            except re.error as e:
                raise KBError(f"bad --grep pattern: {e}") from e
            lines = (d["text"] or "").splitlines()
            hits = [i for i, line in enumerate(lines) if rx.search(line)]
            d["matches"] = [{"line": i + 1, "text": "\n".join(lines[max(0, i - context): i + context + 1])}
                            for i in hits[:50]]
            d["match_count"] = len(hits)
            del d["text"]
            return d
        full = len(d["text"] or "")
        if max_chars and full > max_chars:
            d["text"] = d["text"][:max_chars]
            d["truncated"] = f"{max_chars} of {full} chars; pass a larger max_chars or a passage number"
        return d

    def list_items(self, types: list[str] | None = None, since: str = "", limit: int = 50) -> list[dict]:
        sql, args = "SELECT id, type, title, url, date FROM items WHERE 1=1", []
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            args += types
        if since:
            sql += " AND date >= ?"
            args.append(since)
        sql += " ORDER BY date DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args)]

    # -- tables ----------------------------------------------------------------------
    def tables(self) -> dict:
        return {name: {**spec, "rows": self.summary.get("tables", {}).get(name)}
                for name, spec in (self.manifest.get("tables") or {}).items()}

    def table(self, name: str, match: dict[str, str] | None = None, text: str = "", since: str = "",
              until: str = "", min_amount: float | None = None, sort: str = "", limit: int = 50) -> dict:
        spec = (self.manifest.get("tables") or {}).get(name)
        if spec is None:
            raise KBError(f"no table {name!r}; have {sorted(self.manifest.get('tables') or {})}")
        date_col, amt_col = spec.get("date_column"), spec.get("amount_column")
        rows = [json.loads(r[0]) for r in self.db.execute("SELECT data FROM table_rows WHERE tbl = ? ORDER BY n", (name,))]

        def ok(row: dict) -> bool:
            for col, needle in (match or {}).items():
                if needle.lower() not in str(row.get(col, "")).lower():
                    return False
            if text and text.lower() not in json.dumps(row, ensure_ascii=False).lower():
                return False
            d = str(row.get(date_col, ""))[:10] if date_col else ""
            if since and (not d or d < since):
                return False
            if until and (not d or d > until):
                return False
            if min_amount is not None and amt_col:
                try:
                    if float(row.get(amt_col) or 0) < min_amount:
                        return False
                except (TypeError, ValueError):
                    return False
            return True

        hits = [r for r in rows if ok(r)]
        if sort:
            col, rev = sort.lstrip("-"), sort.startswith("-")

            def key(r):
                v = r.get(col)
                return (0, v) if isinstance(v, (int, float)) else (1, str(v or ""))
            hits.sort(key=key, reverse=rev)
        total = None
        if amt_col:
            try:
                total = round(sum(float(r.get(amt_col) or 0) for r in hits), 2)
            except (TypeError, ValueError):
                total = None
        return {"table": name, "matched": len(hits), "total_amount": total, "rows": hits[:limit],
                "truncated": len(hits) > limit}

    # -- change history --------------------------------------------------------------
    def whats_new(self, since: str = "", limit: int = 100) -> dict:
        sql, args = "SELECT * FROM changes", []
        if since:
            sql += " WHERE at >= ?"
            args.append(since)
        sql += " ORDER BY at DESC LIMIT ?"
        args.append(limit)
        rows = [dict(r) for r in self.db.execute(sql, args)]
        note = ""
        if self.summary.get("baseline") and not rows:
            note = (f"baseline build at {self.summary.get('built_at')}: everything is new; "
                    "change history starts with the next refresh")
        return {"built_at": self.summary.get("built_at"), "changes": rows, "note": note}
