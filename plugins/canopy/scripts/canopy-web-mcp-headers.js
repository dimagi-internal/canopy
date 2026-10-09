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
// What the user saw was three steps removed from the cause. No auth header was sent,
// so canopy-web returned 401; Claude Code fell back to OAuth discovery and dynamic
// client registration, which canopy-web did not serve then, and the connection died
// with "Dynamic Client Registration rejected (HTTP 404)". Measured on a Windows
// machine 2026-09-01→03: six MCP logs, the same failure in all six. The same file
// under Git Bash emits the header correctly, which is exactly why it looked fine to
// anyone testing it by hand.
//
// NO TOKEN IS NOW A WORKING STATE, NOT A FAILURE (canopy-web #1034, 2026-09-30).
// canopy-web serves the MCP OAuth flow, so when this helper emits no header the 401
// leads Claude Code to a browser sign-in on canopy-web, and the person is connected
// as themselves. The token sources below still come first, because every one of them
// is a session with NO person at a browser — a confined caller's session, an agent's
// own turn, a headless runner — or an operator who minted a token on purpose.
//
// Node is guaranteed present, because Claude Code itself runs on it.
//
// ESM, not CommonJS: plugins/canopy/scripts/package.json declares "type": "module",
// so a `.js` here IS an ES module and `require` is not defined in it.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

// A CONFINED session — a caller's, not the agent owner's — must never reach canopy
// with the owner's PAT. The runner leaves the session's own credential (a caller
// token, `cct_…`, which canopy runs as the caller and scopes to their capability)
// in the session's profile, found either from CANOPY_PROFILE (a runner that spawns
// claude itself) or from the `emdash-cx-<task>-<suffix>` directory emdash starts
// the session in. A confined session with no token gets an invalid header: the
// server refuses it, which is correct — the PAT would not be.
const CONFINED_NO_TOKEN = { Authorization: "Bearer cct_missing-confined-session-token" };

function readProfile(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

function confinedProfile() {
  if (process.env.CANOPY_PROFILE) {
    return { confined: true, profile: readProfile(process.env.CANOPY_PROFILE) };
  }
  // Any component of the working directory: emdash starts the session in its
  // worktree, and a helper run from a subdirectory must reach the same answer.
  const parts = process.cwd().split(path.sep);
  const marked = parts.filter((p) => p.startsWith("emdash-cx-"));
  if (marked.length === 0) return { confined: false, profile: null };
  const leaf = marked[0].slice("emdash-".length);           // cx-<subject>-<disc>-<suffix>
  const root = path.join(os.homedir(), ".canopy", "profiles");
  // emdash appends a random `-<suffix>` to the task name; try with and without it.
  for (const name of [leaf, leaf.replace(/-[a-z0-9]+$/, "")]) {
    if (!/^cx-[a-z0-9-]{1,200}$/.test(name)) continue;
    const p = readProfile(path.join(root, `${name}.json`));
    if (p) return { confined: true, profile: p };
  }
  return { confined: true, profile: null };
}

// ARCHIVE / MARKETPLACE MODE (`--archive`, canopy#851). `/canopy:setup` registers a
// workspace's agent marketplace (`/w/<ws>/marketplace.json`, canopy-web#1376) in USER
// settings with this helper as its `headersHelper`, so the catalog fetch and every
// archive download carry the person's own canopy-web token. Claude Code runs it from
// the config dir (~/.claude), in the background, for the PERSON — never for a confined
// caller, an agent turn, or a chat. So this mode skips every session-scoped source
// (confined profile, scoped caller token, agent PAT, chat key) and answers with the
// person's token alone: $CANOPY_WEB_PAT, else the workbench-token file. No token →
// `{}` (the fetch 401s and setup tells the person to sign in), never a crash.
if (process.argv.includes("--archive")) {
  let out = {};
  try {
    let token = (process.env.CANOPY_WEB_PAT || "").trim();
    if (!token) {
      const tokenFile =
        process.env.CANOPY_WORKBENCH_TOKEN ||
        path.join(os.homedir(), ".claude", "canopy", "workbench-token");
      token = fs.readFileSync(tokenFile, "utf8").trim();
    }
    if (token) out = { Authorization: `Bearer ${token}` };
  } catch {
    out = {};
  }
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
}

const { confined, profile } = confinedProfile();
if (confined) {
  const token = profile && typeof profile.mcp_token === "string" ? profile.mcp_token : "";
  process.stdout.write(JSON.stringify(token ? { Authorization: `Bearer ${token}` } : CONFINED_NO_TOKEN));
  process.exit(0);
}

// A COLLEAGUE'S FULL-PROFILE session (canopy-web#1332, who-is-asking phase 5): the
// agent's whole profile, asked by someone who is not its owner, an admin or canopy
// (a `full:` rule, an editor). It is not confined, but its canopy tools must run as
// the ASKER — with the box's PAT they read every other conversation as the box's
// owner. The runner leaves the asker's caller token where this helper can look
// without a secret-looking variable (Claude Code strips those from this environment):
//   cloud:  ~/.canopy/scoped/turn/<CANOPY_SCOPED_TURN>.token   (a turn id, not a secret)
//   laptop: ~/.canopy/scoped/task/<emdash task>.token, the task read off the
//           session's worktree path the way the chat key is found below.
// Once a scoped session is identified the helper NEVER falls back to a PAT: on the
// cloud the variable alone makes it scoped, so a missing file sends an invalid
// header, which the server refuses — correct, where the box's PAT would not be.
const SCOPED_ROOT = path.join(os.homedir(), ".canopy", "scoped");

function scopedSession() {
  const turn = (process.env.CANOPY_SCOPED_TURN || "").trim();
  if (turn) {
    if (!/^[0-9a-fA-F-]{8,64}$/.test(turn)) return { scoped: true, token: "" };
    return { scoped: true, token: readScoped(path.join(SCOPED_ROOT, "turn", `${turn}.token`)) };
  }
  const worktrees = path.join(os.homedir(), "emdash", "worktrees") + path.sep;
  const cwd = process.cwd() + path.sep;
  if (!cwd.startsWith(worktrees)) return { scoped: false, token: "" };
  for (const part of cwd.slice(worktrees.length).split(path.sep).filter(Boolean)) {
    for (const name of [part, part.startsWith("emdash-") ? part.slice("emdash-".length) : ""]) {
      for (const cand of [name, name.replace(/-[0-9a-z]+$/, "")]) {
        if (!cand || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$/.test(cand)) continue;
        const token = readScoped(path.join(SCOPED_ROOT, "task", `${cand}.token`));
        if (token) return { scoped: true, token };
      }
    }
  }
  return { scoped: false, token: "" };
}

function readScoped(file) {
  try {
    const token = fs.readFileSync(file, "utf8").trim();
    return token.startsWith("cct_") ? token : "";
  } catch {
    return "";
  }
}

const scoped = scopedSession();
if (scoped.scoped && !scoped.token) {
  process.stdout.write(JSON.stringify(CONFINED_NO_TOKEN));
  process.exit(0);
}

// WHO this session is, in the same order the canopy CLI resolves it
// (src/orchestrator/canopy_web.py::resolve_token): CANOPY_WEB_PAT, then the
// agent's OWN PAT from ~/.<slug>/.env, and only then the operator's
// workbench-token file.
//
// This used to read the file alone. A headless cloud runner has no such file by
// design, so it sent no header, canopy-web 401'd, and Claude Code went looking
// for OAuth metadata at the ORIGIN — where connect-labs, sharing the domain,
// answered with its own: "Protected resource https://labs.connect.dimagi.com/mcp/
// does not match expected" (cloud-ec2-1, 2026-09-22). On a laptop the same gap
// was silent the other way round: every agent's MCP calls ran as the operator.

// HOW CLAUDE CODE ACTUALLY RUNS THIS (measured on cloud-ec2-1, 2026-09-22): from
// the PLUGIN's own directory, not the session's, and with secret-looking variables
// stripped from its environment — CANOPY_WEB_PAT does not arrive, while a plain
// variable like HAL_GMAIL_ACCOUNT does. So neither the env PAT nor the walk-up
// below can identify an agent session here. CANOPY_AGENT can: a slug is not a
// secret, so it survives, and the PAT is then read from that agent's own file.
// The cloud runner sets it for every agent turn. (The other two legs still serve
// anything that runs this helper directly, as the canopy CLI's rules do.)
function slugEnvPat(slug) {
  if (!/^[a-z0-9][a-z0-9_-]*$/i.test(slug || "")) return "";
  try {
    for (const raw of fs.readFileSync(path.join(os.homedir(), `.${slug}`, ".env"), "utf8").split("\n")) {
      const line = raw.trim();
      if (line.startsWith("CANOPY_WEB_PAT=")) {
        return line.slice("CANOPY_WEB_PAT=".length).trim().replace(/^["']|["']$/g, "");
      }
    }
  } catch {
    return "";
  }
  return "";
}

// The agent's own PAT: walk up from cwd for `.claude-plugin/plugin.json`, take
// its `name` as the slug, read CANOPY_WEB_PAT from that agent's env file.
function agentEnvPat() {
  let dir = process.cwd();
  for (;;) {
    const manifest = path.join(dir, ".claude-plugin", "plugin.json");
    if (fs.existsSync(manifest)) {
      let slug = "";
      try {
        slug = (JSON.parse(fs.readFileSync(manifest, "utf8")) || {}).name || "";
      } catch {
        return "";
      }
      if (!/^[a-z0-9][a-z0-9_-]*$/i.test(slug)) return "";
      try {
        for (const raw of fs.readFileSync(path.join(os.homedir(), `.${slug}`, ".env"), "utf8").split("\n")) {
          const line = raw.trim();
          if (line.startsWith("CANOPY_WEB_PAT=")) {
            return line.slice("CANOPY_WEB_PAT=".length).trim().replace(/^["']|["']$/g, "");
          }
        }
      } catch {
        return "";
      }
      return "";
    }
    const parent = path.dirname(dir);
    if (parent === dir) return "";
    dir = parent;
  }
}

function fileToken() {
  const tokenFile =
    process.env.CANOPY_WORKBENCH_TOKEN ||
    path.join(os.homedir(), ".claude", "canopy", "workbench-token");
  return fs.readFileSync(tokenFile, "utf8").trim();
}

// WHICH CHAT this session is driving, when it drives one. canopy mints a chat key
// when a runner claims a chat's turn (canopy-web `ChatKey`); sent as
// X-Canopy-Chat-Key beside the bearer token, it narrows the page tools to THAT
// chat's page — the bearer alone is the agent's login, which is in every chat
// the agent is in. The runner leaves it where this helper can look without a
// secret-looking variable (Claude Code strips those from this environment):
//   cloud:  ~/.canopy/chat/chat/<CANOPY_CHAT_SESSION>.key   (that variable is a
//           chat id, not a secret, so it survives)
//   laptop: ~/.canopy/chat/task/<emdash task>.key, the task read off the
//           session's worktree path the way the confined profile is found above.
// No key found = no header, and the server answers the way it did before keys.
function readKey(file) {
  try {
    const key = fs.readFileSync(file, "utf8").trim();
    return key.startsWith("chk_") ? key : "";
  } catch {
    return "";
  }
}

const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$/;

function chatKey() {
  const root = path.join(os.homedir(), ".canopy", "chat");
  const chat = (process.env.CANOPY_CHAT_SESSION || "").trim();
  if (/^[0-9a-fA-F-]{8,64}$/.test(chat)) {
    const key = readKey(path.join(root, "chat", `${chat}.key`));
    if (key) return key;
  }
  const worktrees = path.join(os.homedir(), "emdash", "worktrees") + path.sep;
  const cwd = process.cwd() + path.sep;
  if (!cwd.startsWith(worktrees)) return "";
  // Over-generating names is safe: only a task the runner wrote a key for has a file.
  for (const part of cwd.slice(worktrees.length).split(path.sep).filter(Boolean)) {
    for (const name of [part, part.startsWith("emdash-") ? part.slice("emdash-".length) : ""]) {
      for (const cand of [name, name.replace(/-[0-9a-z]+$/, "")]) {
        if (!cand || !SAFE_NAME.test(cand)) continue;
        const key = readKey(path.join(root, "task", `${cand}.key`));
        if (key) return key;
      }
    }
  }
  return "";
}

// PROVENANCE — what client this is and which turn/session it came from, the MCP
// side of src/orchestrator/provenance.py (same headers, same sources). Sent only
// beside a bearer: with no token the 401 must lead to the browser sign-in exactly
// as before. Computed once per connect, so on a laptop it reflects the by-task
// record the caller_context hook had written by then. Never throws.
const UUIDISH = /^[0-9a-fA-F-]{8,64}$/;
const PRINTABLE = /^[\x20-\x7e]{1,256}$/;

function pluginVersion() {
  try {
    const here = path.dirname(fileURLToPath(import.meta.url));
    return JSON.parse(fs.readFileSync(path.join(here, "..", ".claude-plugin", "plugin.json"), "utf8")).version || "unknown";
  } catch {
    return "unknown";
  }
}

function taskCandidates() {
  const out = [];
  const parts = process.cwd().split(path.sep);
  for (let i = 0; i < parts.length; i++) {
    if (parts[i] !== "worktrees") continue;
    for (const p of parts.slice(i + 1, i + 3)) {
      if (!p.startsWith("emdash-")) continue;
      const leaf = p.slice("emdash-".length);
      for (const n of [leaf.replace(/-[0-9a-z]+$/i, ""), leaf]) {
        if (n && SAFE_NAME.test(n) && !out.includes(n)) out.push(n);
      }
    }
  }
  return out;
}

function provenanceHeaders() {
  const h = { "X-Canopy-Client": `canopy-mcp/${pluginVersion()}` };
  try {
    const env = process.env;
    const ok = (v, re) => (typeof v === "string" && PRINTABLE.test(v.trim()) && (!re || re.test(v.trim())) ? v.trim() : "");
    let turn = ok(env.CANOPY_TURN_ID, UUIDISH);
    let session = ok(env.CANOPY_SESSION_ID, UUIDISH);
    let claude = ok(env.CLAUDE_CODE_SESSION_ID || env.CLAUDE_SESSION_ID, UUIDISH);
    if ((!turn || !session) && env.CANOPY_CALLER) {
      const c = readProfile(env.CANOPY_CALLER) || {};
      turn = turn || ok(c.turn_id, UUIDISH);
      session = session || ok((c.conversation || {}).session_id, UUIDISH);
    }
    // The task name only LOCATES this machine's by-task record; it is never sent —
    // canopy-web derives runner, runner type and host from the parent turn.
    if (!turn || !session || !claude) {
      for (const n of taskCandidates()) {
        const rec = readProfile(path.join(os.homedir(), ".canopy", "caller", "by-task", `${n}.json`));
        if (!rec) continue;
        turn = turn || ok(rec.turn_id, UUIDISH);
        session = session || ok(rec.session_id, UUIDISH);
        claude = claude || ok(rec.claude_session_id, UUIDISH);
        break;
      }
    }
    if (turn) h["X-Canopy-Parent-Turn"] = turn;
    if (session) h["X-Canopy-Parent-Session"] = session;
    if (claude) h["X-Canopy-Claude-Session"] = claude;
  } catch {
    // best-effort: the client header alone is still true
  }
  return h;
}

let headers = {};
try {
  const token =
    scoped.token ||
    (process.env.CANOPY_WEB_PAT || "").trim() ||
    slugEnvPat(process.env.CANOPY_AGENT) ||
    agentEnvPat() ||
    fileToken();
  if (token) {
    headers = { ...provenanceHeaders(), Authorization: `Bearer ${token}` };
    const key = chatKey();
    if (key) headers["X-Canopy-Chat-Key"] = key;
  }
} catch (err) {
  // Missing/unreadable token file: emit no auth header. canopy-web returns 401 and
  // Claude Code offers the browser sign-in (/mcp shows the server as needing auth).
  // Never throw: a helper that crashes produces the same empty-and-silent result
  // this file exists to eliminate.
  headers = {};
}

process.stdout.write(JSON.stringify(headers));
