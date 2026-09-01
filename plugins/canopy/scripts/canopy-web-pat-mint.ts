#!/usr/bin/env tsx
/**
 * canopy-web Personal Access Token minter (gh-style loopback flow).
 *
 * Backs the `/canopy:canopy-web-pat-mint` slash command. Mints a
 * canopy-web PersonalToken bound to the human operator (the one signed
 * into canopy-web in their normal browser), then writes the raw token
 * to `~/.claude/canopy/workbench-token` (chmod 600). The canopy plugin's
 * post_tool_use hook + walkthrough-share/upload.py + canopy-doctor all
 * read from that path already.
 *
 * Replaces the previous shared-secret WORKBENCH_WRITE_TOKEN bootstrap
 * (which required ops to fetch the value from 1Password / Secret
 * Manager) with a per-machine, per-human one-time browser flow.
 *
 * Flow (gh / fly / gcloud loopback pattern, ported verbatim from
 * the ace plugin's scripts/ace-web-pat-mint.ts):
 *   1. Bind 127.0.0.1:RANDOM with a one-shot HTTP listener.
 *   2. Generate a state nonce (32 bytes urlsafe).
 *   3. Open the operator's browser to
 *        ${CANOPY_WEB_BASE}/auth/cli/authorize/
 *          ?cb=http://127.0.0.1:NNNN/cb&state=<nonce>&label=<label>
 *   4. canopy-web (after @login_required bounce if needed) shows a
 *      one-click "Authorize" page. On click, it mints a PersonalToken
 *      and 302-redirects to <cb>?token=<raw>&state=<state>.
 *   5. Listener verifies state, extracts token, writes to
 *      ~/.claude/canopy/workbench-token with mode 600, returns
 *      "OK you can close this tab" page, shuts down.
 *
 * Usage:
 *   npx --yes tsx scripts/canopy-web-pat-mint.ts [label]
 *     label  defaults to `<hostname>-YYYY-MM-DD`
 *
 * Env:
 *   CANOPY_WEB_API_URL  base URL (default https://labs.connect.dimagi.com/canopy).
 *                       CANOPY_WEB_BASE is accepted as a legacy alias.
 *   TOKEN_FILE_OVERRIDE override the default ~/.claude/canopy/workbench-token path
 *
 * Exit codes:
 *   0 success — token written
 *   1 timeout (15 min) — operator never approved
 *   2 state mismatch — possible race with another mint invocation
 *   3 listener error or browser-open failure
 *   4 token-file write error
 */

import { createServer } from 'node:http';
import { randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import { hostname, platform, homedir } from 'node:os';
import { promises as fs } from 'node:fs';
import { dirname } from 'node:path';
import { pathToFileURL } from 'node:url';

const CANOPY_WEB_BASE = (
  process.env.CANOPY_WEB_API_URL || process.env.CANOPY_WEB_BASE ||
  'https://labs.connect.dimagi.com/canopy'
).replace(/\/$/, '');
// 15 minutes, not 5. A first-time operator spends this window signing in to
// Google, possibly picking an account and clearing MFA. Five minutes expired
// twice on the first Windows bring-up (@smazumdar, 2026-09-01).
const TIMEOUT_MS = 15 * 60 * 1000;
const DEFAULT_TOKEN_FILE = `${homedir()}/.claude/canopy/workbench-token`;
const TOKEN_FILE = process.env.TOKEN_FILE_OVERRIDE || DEFAULT_TOKEN_FILE;

function defaultLabel(): string {
  const date = new Date().toISOString().slice(0, 10);
  return `${hostname().split('.')[0]}-${date}`;
}

/**
 * Open the authorize URL in the operator's default browser, best-effort.
 *
 * Failing to open a browser must NEVER be fatal: the URL is already printed
 * above, so the operator can paste it. That makes the error handling here the
 * load-bearing part, and on Windows all three obvious spellings are wrong:
 *
 *   - `spawn('start', [url])` — `start` is a cmd.exe BUILTIN, not an
 *     executable, so it fails ENOENT.
 *   - `spawn('cmd', ['/c', 'start', url])` — works, but cmd treats `&` as a
 *     command separator and silently TRUNCATES the URL at the first one. Our
 *     URL carries `?cb=...&state=...&label=...`, so the server receives no
 *     state and renders "missing state", which reads like a server bug.
 *   - a bare try/catch around either — spawn reports ENOENT ASYNCHRONOUSLY as
 *     an 'error' event, so the catch structurally cannot see it, and an
 *     unhandled 'error' event on a ChildProcess is thrown: it takes down the
 *     loopback listener we are in the middle of waiting on.
 *
 * `rundll32 url.dll,FileProtocolHandler` takes the URL as a single argv entry
 * with no shell parsing, so `&` survives. The 'error' listener is what makes
 * the failure non-fatal, on every platform.
 * (Reported by @smazumdar, first Windows bring-up, 2026-09-01.)
 */
export function browserOpenCommand(url: string, plat: string = platform()): [string, string[]] {
  if (plat === 'darwin') return ['open', [url]];
  if (plat === 'win32') return ['rundll32.exe', ['url.dll,FileProtocolHandler', url]];
  return ['xdg-open', [url]];
}

/**
 * True when this module is being run as a script rather than imported.
 *
 * Pure and platform-parameterised so the win32 branch is testable from a Mac —
 * there is no Windows machine in the fleet, and this is precisely the code that
 * failed SILENTLY there.
 */
export function isDirectInvocation(metaUrl: string, argv1: string | undefined): boolean {
  if (!argv1) return false;
  return metaUrl === pathToFileURL(argv1).href;
}

function openInBrowser(url: string): void {
  const [cmd, args] = browserOpenCommand(url);
  const warn = (msg: string) =>
    console.error(`warn: failed to auto-open browser (${msg}); open the URL above manually`);
  try {
    const child = spawn(cmd, args, { stdio: 'ignore', detached: true });
    // MUST be attached: spawn surfaces ENOENT as an async event, and an
    // unhandled one is thrown and kills the pending loopback listener.
    child.on('error', (e: Error) => warn(e.message));
    child.unref();
  } catch (e) {
    warn((e as Error).message);
  }
}

async function captureToken(label: string): Promise<string> {
  const state = randomBytes(32).toString('base64url');
  return new Promise<string>((resolve, reject) => {
    const server = createServer();
    let timer: NodeJS.Timeout;

    server.once('error', (err) => reject(err));
    server.on('request', (req, res) => {
      if (!req.url?.startsWith('/cb')) {
        res.statusCode = 404;
        res.end('not found');
        return;
      }
      const u = new URL(req.url, 'http://127.0.0.1');
      const t = u.searchParams.get('token');
      const s = u.searchParams.get('state');

      if (s !== state) {
        res.statusCode = 400;
        res.end('state mismatch');
        clearTimeout(timer);
        server.close();
        reject(new Error('state mismatch — possible cross-process race'));
        return;
      }
      if (!t) {
        res.statusCode = 400;
        res.end('no token');
        clearTimeout(timer);
        server.close();
        reject(new Error('callback missing token query param'));
        return;
      }

      res.statusCode = 200;
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end(`<!doctype html><meta charset="utf-8"><title>OK</title>
<style>body{font-family:system-ui;max-width:480px;margin:6rem auto;text-align:center;color:#1a1a1a}
h1{color:#15803d;font-size:1.4rem;margin:0 0 .75rem}
p{color:#555;line-height:1.5}</style>
<h1>Token captured</h1><p>You can close this tab and return to your terminal.</p>`);

      clearTimeout(timer);
      server.close();
      resolve(t);
    });

    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      if (!addr || typeof addr === 'string') {
        reject(new Error('failed to bind loopback port'));
        return;
      }
      const cb = `http://127.0.0.1:${addr.port}/cb`;
      const url = `${CANOPY_WEB_BASE}/auth/cli/authorize/?cb=${encodeURIComponent(cb)}&state=${state}&label=${encodeURIComponent(label)}`;

      console.error(`[1/3] listening on ${cb}`);
      console.error(`[2/3] open this URL in your browser to authorize:\n  ${url}`);
      console.error(`[3/3] waiting up to ${TIMEOUT_MS / 60000} minutes for callback...`);

      openInBrowser(url);

      timer = setTimeout(() => {
        server.close();
        reject(new Error(
          `timeout — no callback received in ${TIMEOUT_MS / 60000} minutes. ` +
          'Nothing is stuck and nothing was consumed: re-run the same command ' +
          'to generate a fresh link.'
        ));
      }, TIMEOUT_MS);
    });
  });
}

/**
 * Write the raw token to the workbench-token file, replacing any prior
 * value. Creates parent directories as needed. mode 0o600.
 */
export async function writeTokenFile(path: string, value: string): Promise<void> {
  await fs.mkdir(dirname(path), { recursive: true });
  await fs.writeFile(path, value + '\n', { mode: 0o600 });
}

async function main(): Promise<number> {
  const label = process.argv[2] || defaultLabel();
  console.error(`[mint] label=${label} canopy_web_base=${CANOPY_WEB_BASE}`);

  let token: string;
  try {
    token = await captureToken(label);
  } catch (e) {
    const msg = (e as Error).message;
    console.error(`error: ${msg}`);
    if (msg.includes('timeout')) return 1;
    if (msg.includes('state mismatch')) return 2;
    return 3;
  }

  try {
    await writeTokenFile(TOKEN_FILE, token);
  } catch (e) {
    console.error(`error: writing ${TOKEN_FILE}: ${(e as Error).message}`);
    return 4;
  }

  console.error(`[done] minted "${label}" (${token.length} chars), wrote token to ${TOKEN_FILE}`);
  console.error(`       /reload-plugins to pick up the new token, then /canopy:canopy-doctor to verify.`);
  return 0;
}

// Only run main when invoked as a script (not when imported by tests).
//
// pathToFileURL, NOT `file://${argv[1]}`. On Windows import.meta.url is
// `file:///C:/...` while the template produces `file://C:...` — they can never
// match, so main() never ran, the script exited 0 and printed NOTHING, which
// looks exactly like success. (Reported by @smazumdar, 2026-09-01.)
if (isDirectInvocation(import.meta.url, process.argv[1])) {
  main().then((code) => process.exit(code));
}
