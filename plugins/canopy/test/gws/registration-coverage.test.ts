/**
 * Registration-coverage drift gate for the canopy-gws MCP server.
 *
 * Catches the "tool added to a handler module but not registered on the
 * MCP server" class of bug — and its inverse. `mcp/gws-server.ts` calls
 * `server.tool('name', ...)` for every atom it exposes; this test parses
 * those calls statically and asserts:
 *
 *   1. Snapshot count: the number of `server.tool(...)` calls matches an
 *      explicit expected count. Update intentionally when shipping atoms.
 *   2. Prefix allowlist: every tool name starts with one of the prefixes
 *      the server is allowed to register.
 *   3. No duplicate tool registrations.
 *   4. The server file is actually wired into the canopy plugin's
 *      plugin.json `mcpServers` map (a server file that exists on disk but
 *      is not registered is silently unreachable by agents).
 *   5. Session identity (canopy#850): the session gate is installed before
 *      the first tool, so every call acts as the session's agent only after
 *      `canopy cred check` allows it — never a fallback to a default identity.
 *
 * Parses statically (never imports the server module) so no MCP transport
 * or Google auth is touched. Ported from ACE's registration-coverage gate.
 */
import { describe, it, expect } from 'vitest';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

// plugins/canopy/ — the plugin root this test suite lives under.
const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

function extractToolRegistrations(relpath: string): string[] {
  const src = fs.readFileSync(path.join(PLUGIN_ROOT, relpath), 'utf-8');
  // Multiline-tolerant: matches `server.tool(\n  'name',` and `server.tool('name',`.
  const re = /\bserver\.tool\s*\(\s*['"]([a-z][a-z0-9_]*)['"]/g;
  const out: string[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) out.push(m[1]);
  return out;
}

// Snapshot of registered atoms (32 as of the initial port from ACE —
// all drive_*/docs_*/sheets_*/slides_* atoms plus the forms reader and the
// gog-CLI personal-Drive fallback; the ACE-aware atoms stayed in ACE).
// Update intentionally when shipping a new atom.
const SERVER_FILE = 'mcp/gws-server.ts';
const EXPECTED_COUNT = 32;
const ALLOWED_PREFIXES = [
  'drive_',
  'sheets_',
  'docs_',
  'slides_',
  'read_personal_drive_doc',
  'get_google_form_definition',
];

describe('canopy-gws tool registration', () => {
  const tools = extractToolRegistrations(SERVER_FILE);

  it('registers the expected number of tools (update snapshot when shipping atoms)', () => {
    expect(tools, `${SERVER_FILE} actual tools: ${JSON.stringify(tools)}`)
      .toHaveLength(EXPECTED_COUNT);
  });

  it('uses only the allowed prefixes for this server', () => {
    const offenders = tools.filter(
      (t) => !ALLOWED_PREFIXES.some((p) => t === p || t.startsWith(p)),
    );
    expect(offenders, `tools in ${SERVER_FILE} with unrecognized prefix`).toEqual([]);
  });

  it('has no duplicate tool names', () => {
    const seen = new Set<string>();
    const dupes: string[] = [];
    for (const t of tools) {
      if (seen.has(t)) dupes.push(t);
      seen.add(t);
    }
    expect(dupes, `${SERVER_FILE}: duplicate tool registrations`).toEqual([]);
  });

  it('does not register any ACE-aware atom (those live in the ACE plugin)', () => {
    const aceOnly = [
      'resolve_opp_path',
      'resolve_current_run_id',
      'validate_run_state',
      'classify_phase_writeback',
      'verify_phase_products',
      'verify_phase_artifacts',
      'render_run_readme',
      'render_decisions_log',
      'generate_inputs_manifest',
      'update_yaml_file',
    ];
    const leaked = tools.filter((t) => aceOnly.includes(t) || t.startsWith('decisions_'));
    expect(leaked, 'ACE-aware atoms must not be registered on canopy-gws').toEqual([]);
  });
});

describe('canopy-gws plugin.json wiring', () => {
  // Unregistered on purpose (2026-10-09): no agent or session had ever called a
  // canopy-gws tool — agents reach Google through gog and `canopy gdoc` — and no
  // agent env carries the GWS_* identity it needs, so registering it only added a
  // dead server to every session. The code and these tests stay; re-registering is
  // the one plugin.json entry this test names. Flip it when an agent needs it.
  it('mcp/gws-server.ts is NOT registered in .claude-plugin/plugin.json mcpServers', () => {
    const pluginJson = JSON.parse(
      fs.readFileSync(path.join(PLUGIN_ROOT, '.claude-plugin/plugin.json'), 'utf-8'),
    );
    const registered = new Set<string>();
    for (const entry of Object.values(pluginJson.mcpServers ?? {}) as Array<{
      args?: string[];
    }>) {
      for (const arg of entry.args ?? []) {
        const m = arg.match(/mcp\/[A-Za-z0-9_-]+-server\.ts$/);
        if (m) registered.add(m[0]);
      }
    }
    expect(
      registered.has('mcp/gws-server.ts'),
      'canopy-gws is deliberately unregistered — re-enable it with a ' +
        '"canopy-gws": {"command": "npx", "args": ["tsx", "${CLAUDE_PLUGIN_ROOT}/mcp/gws-server.ts"]} ' +
        'entry AND flip this test, once an agent env carries GWS_IDENTITY_MODE / GWS_SA_KEY_PATH.',
    ).toBe(false);
  });
});

describe('canopy-gws session identity', () => {
  it('installs the session gate after the server is built and before any tool', () => {
    const src = fs.readFileSync(path.join(PLUGIN_ROOT, SERVER_FILE), 'utf-8');
    const install = src.indexOf('installSessionGate(server as never, createSessionGate(applySessionIdentity));');
    expect(install).toBeGreaterThan(src.indexOf('new McpServer('));
    expect(install).toBeLessThan(src.search(/\bserver\.tool\s*\(/));
  });

  it('startup never resolves identity (the gate does it lazily at the first call)', () => {
    const src = fs.readFileSync(path.join(PLUGIN_ROOT, SERVER_FILE), 'utf-8');
    const main = src.slice(src.indexOf('async function main()'));
    expect(main.slice(0, main.indexOf('\n}\n'))).not.toMatch(/resolveIdentityFromEnv|process\.exit/);
  });

  it('never reads a non-GWS credential env var (no ACE_/GOOGLE_APPLICATION_CREDENTIALS fallback)', () => {
    const serverSrc = fs.readFileSync(path.join(PLUGIN_ROOT, SERVER_FILE), 'utf-8');
    const identitySrc = fs.readFileSync(
      path.join(PLUGIN_ROOT, 'mcp/gws/lib/identity.ts'),
      'utf-8',
    );
    for (const src of [serverSrc, identitySrc]) {
      expect(src).not.toMatch(/GOOGLE_APPLICATION_CREDENTIALS/);
      expect(src).not.toMatch(/\bACE_[A-Z_]+/);
    }
  });
});
