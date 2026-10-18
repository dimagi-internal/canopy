"""`canopy kb mcp` — one knowledge base as MCP tools over stdio (newline-delimited JSON-RPC).

Stdlib only, like the rest of canopy's runtime: the protocol surface a tool server needs
is four methods (initialize, tools/list, tools/call, ping), and taking an SDK dependency
for them would put a new package in every agent's CLI install.

Tools: `kb_info`, `search`, `get`, `list_items`, `whats_new`, `table`, plus one tool per
table the manifest declares (e.g. `grants`), whose arguments are that table's
filter_columns — so a curator adding a table gives every reader a named tool for it.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Callable

from orchestrator.kb import KB, KBError

PROTOCOL = "2025-06-18"

_STR = {"type": "string"}
_INT = {"type": "integer"}


def _base_tools() -> list[dict]:
    return [
        {"name": "kb_info", "description": "What this knowledge base holds: publisher, licence, item counts by type, tables, build date.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "search", "description": "Best-matching passages for a question across every source item, each with its source URL and date. Put exact phrases in double quotes.",
         "inputSchema": {"type": "object", "required": ["query"], "properties": {
             "query": _STR, "types": {"type": "array", "items": _STR, "description": "limit to item types (see kb_info)"},
             "since": {**_STR, "description": "YYYY-MM-DD"}, "until": {**_STR, "description": "YYYY-MM-DD"},
             "limit": {**_INT, "default": 10}}}},
        {"name": "get", "description": "One item's full text and metadata, by id or URL (from search results). Long items are truncated; pass passage to read one passage.",
         "inputSchema": {"type": "object", "required": ["ref"], "properties": {
             "ref": _STR, "max_chars": {**_INT, "default": 20000}, "passage": _INT,
             "grep": {**_STR, "description": "regex; return only matching lines with context (cheap way to verify a quote or number)"},
             "context": {**_INT, "default": 1}}}},
        {"name": "list_items", "description": "Items newest first, optionally by type and date.",
         "inputSchema": {"type": "object", "properties": {
             "types": {"type": "array", "items": _STR}, "since": _STR, "limit": {**_INT, "default": 50}}}},
        {"name": "whats_new", "description": "Items that appeared or changed at the publisher since a date (the refresh history).",
         "inputSchema": {"type": "object", "properties": {"since": _STR, "limit": {**_INT, "default": 100}}}},
        {"name": "table", "description": "Query any structured table by name (see kb_info): substring matches per column, date range, minimum amount, sort.",
         "inputSchema": {"type": "object", "required": ["name"], "properties": {
             "name": _STR, "match": {"type": "object", "additionalProperties": _STR}, "text": _STR,
             "since": _STR, "until": _STR, "min_amount": {"type": "number"}, "sort": _STR,
             "limit": {**_INT, "default": 50}}}},
    ]


def _tool_name(table: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", table)[:60]


def table_tools(kb: KB) -> list[dict]:
    out = []
    for name, spec in (kb.manifest.get("tables") or {}).items():
        props = {c: {**_STR, "description": f"substring of {c}"} for c in spec.get("filter_columns") or []}
        props.update({"text": {**_STR, "description": "substring anywhere in the row"},
                      "sort": {**_STR, "description": "column name; prefix - for descending"},
                      "limit": {**_INT, "default": 50}})
        if spec.get("date_column"):
            props.update({"since": {**_STR, "description": f"{spec['date_column']} >= YYYY-MM-DD"},
                          "until": {**_STR, "description": f"{spec['date_column']} <= YYYY-MM-DD"}})
        if spec.get("amount_column"):
            props["min_amount"] = {"type": "number", "description": f"{spec['amount_column']} at least"}
        out.append({"name": _tool_name(name), "description": spec.get("description") or f"Rows of the {name} table.",
                    "inputSchema": {"type": "object", "properties": props}, "_table": name})
    return out


def call_tool(kb: KB, name: str, args: dict, tables: dict[str, dict]):
    if name == "kb_info":
        return kb.info()
    if name == "search":
        return kb.search(args["query"], args.get("types"), args.get("since", ""), args.get("until", ""), int(args.get("limit", 10)))
    if name == "get":
        return kb.get(args["ref"], int(args.get("max_chars", 20000)), args.get("passage"),
                      args.get("grep", ""), int(args.get("context", 1)))
    if name == "list_items":
        return kb.list_items(args.get("types"), args.get("since", ""), int(args.get("limit", 50)))
    if name == "whats_new":
        return kb.whats_new(args.get("since", ""), int(args.get("limit", 100)))
    if name == "table":
        return kb.table(args["name"], args.get("match"), args.get("text", ""), args.get("since", ""), args.get("until", ""),
                        args.get("min_amount"), args.get("sort", ""), int(args.get("limit", 50)))
    if name in tables:
        tbl = tables[name]["_table"]
        cols = (kb.manifest["tables"][tbl].get("filter_columns") or [])
        match = {c: args[c] for c in cols if args.get(c)}
        return kb.table(tbl, match, args.get("text", ""), args.get("since", ""), args.get("until", ""),
                        args.get("min_amount"), args.get("sort", ""), int(args.get("limit", 50)))
    raise KBError(f"unknown tool {name!r}")


def serve(opener: Callable[[], tuple[KB, str]], name: str = "", stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    state: dict = {}

    def kb() -> KB:
        if "kb" not in state:
            state["kb"], state["warning"] = opener()
        return state["kb"]

    def send(msg: dict) -> None:
        stdout.write(json.dumps(msg, ensure_ascii=False, default=str) + "\n")
        stdout.flush()

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        rid, method, params = req.get("id"), req.get("method", ""), req.get("params") or {}
        if rid is None:  # notification (initialized, cancelled, …)
            continue
        try:
            if method == "initialize":
                try:
                    server = name or kb().manifest.get("name") or "kb"
                except Exception:
                    server = name or "kb"
                result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": f"canopy-kb-{server}", "version": "1"},
                          "instructions": "A cited knowledge base of one publisher's public material. "
                                          "Cite the url on every row you use."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                tools = _base_tools() + [{k: v for k, v in t.items() if k != "_table"} for t in table_tools(kb())]
                result = {"tools": tools}
            elif method == "tools/call":
                k = kb()
                tables = {t["name"]: t for t in table_tools(k)}
                try:
                    out = call_tool(k, params.get("name", ""), params.get("arguments") or {}, tables)
                    if state.get("warning"):
                        out = {"warning": state["warning"], "result": out}
                    result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}]}
                except (KBError, KeyError, ValueError, TypeError) as e:
                    result = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}})
                continue
            send({"jsonrpc": "2.0", "id": rid, "result": result})
        except Exception as e:  # never die mid-session; report and keep serving
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)[:500]}})
