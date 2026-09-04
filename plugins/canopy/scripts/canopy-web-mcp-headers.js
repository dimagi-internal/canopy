#!/usr/bin/env node
// headersHelper for the canopy-web remote MCP server.
//
// Claude Code runs this at MCP connect time and reads a JSON object of header
// key/value pairs from stdout (10s timeout). We emit the bearer auth header from
// the per-user PAT that `/canopy:canopy-web-pat-mint` writes to
// ~/.claude/canopy/workbench-token — so there is no env var to export and no token
// in any config file. Re-minting rotates the token automatically (this runs fresh
// on each connect).
//
// WHY THIS IS JAVASCRIPT AND NOT THE .sh IT REPLACES
// --------------------------------------------------
// `headersHelper` is ONE string for every platform, so a .cmd sibling cannot help.
// Claude Code spawns it through the platform shell, and cmd.exe cannot execute a
// .sh — it returns EMPTY with EXIT 0. That is the worst possible failure shape:
// no error, no output, indistinguishable from success.
//
// What the user actually sees is three steps removed from the cause. No auth header
// is sent, so canopy-web correctly returns 401; Claude Code then falls back to OAuth
// discovery and dynamic client registration; Labs has no such endpoint, so it serves
// its own 404 HTML page, and the connection dies with:
//
//     Dynamic Client Registration rejected (HTTP 404): <!DOCTYPE html> … Page not found
//
// That 404 is a red herring. It reads as a canopy-web routing problem or an expired
// login, and it sends people to re-mint a token that was fine all along. Measured on
// a Windows machine 2026-09-01→03: six MCP logs, the same failure in all six, and the
// server had never once connected there. The same file under Git Bash emits the
// header correctly, which is exactly why it looks fine to anyone testing it by hand.
//
// Node is guaranteed present, because Claude Code itself runs on it.
//
// ESM, not CommonJS: plugins/canopy/scripts/package.json declares "type": "module",
// so a `.js` here IS an ES module and `require` is not defined in it.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// Mirror the skill's resolution order.
const tokenFile =
  process.env.CANOPY_WORKBENCH_TOKEN ||
  path.join(os.homedir(), ".claude", "canopy", "workbench-token");

let headers = {};
try {
  const token = fs.readFileSync(tokenFile, "utf8").trim();
  if (token) {
    headers = { Authorization: `Bearer ${token}` };
  }
} catch (err) {
  // Missing/unreadable token file: emit no auth header. canopy-web then returns 401
  // and /mcp surfaces the server as needing auth — the cue to run
  // /canopy:canopy-web-pat-mint. Never throw: a helper that crashes produces the same
  // empty-and-silent result this file exists to eliminate.
  headers = {};
}

process.stdout.write(JSON.stringify(headers));
