"""`canopy kb mcp` — one knowledge base as MCP tools over stdio (newline-delimited JSON-RPC).

The tools are the engine's own (`canopy_kb.tools`): the same list and the same answers
canopy-web serves at `/api/mcp/projects/<workspace>/<handle>/`, so an offline copy reads
exactly like the published one. The protocol surface is four methods (initialize,
tools/list, tools/call, ping), kept stdlib-only rather than taking an SDK dependency.
"""
from __future__ import annotations

import json
import sys
from typing import Callable

from orchestrator.kb_engine import tools as engine_tools

PROTOCOL = "2025-06-18"


def serve(opener: Callable[[], tuple[object, str]], name: str = "", stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    state: dict = {}

    def kb():
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
                          "instructions": "A cited knowledge base of one publisher's public material. Start with "
                                          "kb_info. Cite the url on every fact you use."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": engine_tools().tool_specs(kb())}
            elif method == "tools/call":
                tools = engine_tools()
                try:
                    out = tools.run(kb(), params.get("name", ""), params.get("arguments") or {})
                    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
                    if state.get("warning"):
                        text = f"warning: {state['warning']}\n\n{text}"
                    result = {"content": [{"type": "text", "text": text}]}
                except Exception as e:  # the engine's KBError, or a bad argument
                    if not (type(e).__name__ == "KBError" or isinstance(e, (KeyError, ValueError, TypeError))):
                        raise
                    result = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}})
                continue
            send({"jsonrpc": "2.0", "id": rid, "result": result})
        except Exception as e:  # never die mid-session; report and keep serving
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)[:500]}})
