/**
 * Session identity for canopy-gws (canopy#850): act as the SESSION's agent,
 * resolved through the `canopy cred` broker, never as a fixed account.
 *
 * canopy-gws is one server shared by every agent, so "who am I" can only come
 * from the session. At the first tool call (startup never blocks on it):
 *
 *   1. The session's agent is `$CANOPY_AGENT_SLUG`, else `$CANOPY_AGENT`. If
 *      neither is set this is a human session, and canopy-gws refuses: a human
 *      uses their own Drive path.
 *   2. `canopy cred check --agent <agent>` must exit 0. Exit 3 (refused),
 *      4 (undetermined), 1/2 (error) and a missing `canopy` CLI all refuse,
 *      and the broker's stderr becomes the refusal text.
 *   3. `canopy cred env --agent <agent>` ensures `~/.<agent>/.env` (0600) and
 *      prints its path. The `GWS_*` values in that file are the agent's
 *      Google identity (GWS_IDENTITY_MODE, GWS_SA_KEY_PATH, GWS_ROOT_FOLDER_ID,
 *      GWS_ALLOWED_DRIVE_IDS, GWS_GOG_ACCOUNT, GWS_GOG_CLIENT); they override
 *      any inherited from the process env.
 *   4. `resolveIdentityFromEnv` validates them. No GWS_* identity → refuse,
 *      naming the agent's env file. There is no default identity.
 *
 * An allow is cached for the life of the process (one per session). A refusal
 * is not, so fixing access (`op signin`, adding GWS_* to the agent's
 * .env.tpl) takes effect on the next call. A tool call that fails drops the
 * cached allow, so the next call re-checks.
 *
 * Contract: docs/architecture/session-identity.md.
 */
import { execFile } from 'node:child_process';
import fs from 'node:fs';
import { resolveIdentityFromEnv, GwsIdentityError, type GwsIdentity } from './identity.js';

export const DRIVE_REFUSAL_HINT = 'Use your own Drive path: gog as your own account / canopy gdoc.';

/** The GWS_* keys an agent's env file may supply. */
export const GWS_KEYS = [
  'GWS_IDENTITY_MODE',
  'GWS_SA_KEY_PATH',
  'GWS_ROOT_FOLDER_ID',
  'GWS_ALLOWED_DRIVE_IDS',
  'GWS_GOG_ACCOUNT',
  'GWS_GOG_CLIENT',
] as const;

export interface CredResult {
  status: number | null;
  stdout: string;
  stderr: string;
  error?: NodeJS.ErrnoException;
}

/** Runs `canopy <args>`. Injectable so tests never touch the real CLI. */
export type CredRunner = (args: string[]) => Promise<CredResult>;

/** Long enough for a 1Password unlock prompt or an `op inject`; still a real bound. */
const CRED_TIMEOUT_MS = 120_000;

export const defaultCredRunner: CredRunner = (args) =>
  new Promise((resolve) => {
    execFile(
      'canopy',
      args,
      { encoding: 'utf8', timeout: CRED_TIMEOUT_MS, maxBuffer: 1024 * 1024 },
      (err, stdout, stderr) => {
        const e = err as (NodeJS.ErrnoException & { code?: unknown }) | null;
        if (e && typeof e.code === 'number') {
          resolve({ status: e.code, stdout: String(stdout ?? ''), stderr: String(stderr ?? '') });
          return;
        }
        resolve({
          status: e ? null : 0,
          stdout: String(stdout ?? ''),
          stderr: String(stderr ?? ''),
          ...(e ? { error: e } : {}),
        });
      },
    );
  });

/** The session's agent slug, or '' for a human session. */
export function sessionAgent(env: Record<string, string | undefined> = process.env): string {
  return (env.CANOPY_AGENT_SLUG || env.CANOPY_AGENT || '').trim();
}

/** Turn a failed `canopy cred <cmd>` run into refusal text; null when it succeeded. */
export function credFailure(agent: string, cmd: 'check' | 'env', r: CredResult): string | null {
  if (r.error) {
    const why =
      r.error.code === 'ENOENT'
        ? 'the `canopy` CLI is not installed or not on PATH'
        : `\`canopy cred ${cmd}\` could not run (${r.error.message})`;
    return (
      `Refusing to act as agent '${agent}': ${why}, so this session's identity cannot be ` +
      `confirmed. Install canopy (see /canopy:setup) and re-check with ` +
      `\`canopy cred check --agent ${agent}\`.`
    );
  }
  if (r.status === 0) return null;
  const stderr = r.stderr.trim();
  if (r.status === 3 && stderr) return stderr;
  const label =
    r.status === 3
      ? 'refused'
      : r.status === 4
        ? 'undetermined (treated as refused)'
        : `error (exit ${r.status})`;
  return (
    `Refusing to act as agent '${agent}': \`canopy cred ${cmd} --agent ${agent}\` ${label}` +
    (stderr ? `: ${stderr}` : '.')
  );
}

/** Parse the GWS_* entries of a dotenv-style file (KEY=value, optional quotes / `export`). */
export function parseGwsEnv(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const raw of text.split(/\r?\n/)) {
    const m = raw.match(/^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (!m || !(GWS_KEYS as readonly string[]).includes(m[1])) continue;
    let v = m[2];
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
      v = v.slice(1, -1);
    }
    out[m[1]] = v;
  }
  return out;
}

export type Resolution =
  | { ok: true; agent: string; identity: GwsIdentity; env: Record<string, string> }
  | { ok: false; message: string };

export interface ResolveDeps {
  run?: CredRunner;
  readFile?: (p: string) => string;
  exists?: (p: string) => boolean;
  env?: Record<string, string | undefined>;
}

/** Steps 1–4 above. Pure apart from the injected runner / fs. */
export async function resolveSessionIdentity(deps: ResolveDeps = {}): Promise<Resolution> {
  const run = deps.run ?? defaultCredRunner;
  const readFile = deps.readFile ?? ((p: string) => fs.readFileSync(p, 'utf8'));
  const env = deps.env ?? process.env;
  const agent = sessionAgent(env);
  if (!agent) {
    return {
      ok: false,
      message:
        'canopy-gws acts as the session\'s agent, and this session is not an agent ' +
        '($CANOPY_AGENT_SLUG / $CANOPY_AGENT unset), so it has no Google identity to act as.',
    };
  }
  const checkFail = credFailure(agent, 'check', await run(['cred', 'check', '--agent', agent]));
  if (checkFail) return { ok: false, message: checkFail };

  const envRun = await run(['cred', 'env', '--agent', agent]);
  const envFail = credFailure(agent, 'env', envRun);
  if (envFail) return { ok: false, message: envFail };
  const envPath = envRun.stdout.trim().split(/\r?\n/).pop()?.trim() ?? '';
  let fileEnv: Record<string, string>;
  try {
    fileEnv = parseGwsEnv(readFile(envPath));
  } catch (e) {
    return {
      ok: false,
      message: `Could not read agent '${agent}'s env file (${envPath || 'no path printed'}): ${(e as Error).message}`,
    };
  }
  const inherited: Record<string, string> = {};
  for (const k of GWS_KEYS) {
    const v = env[k];
    if (v) inherited[k] = v;
  }
  const merged = { ...inherited, ...fileEnv };
  try {
    const identity = resolveIdentityFromEnv(merged, deps.exists);
    return { ok: true, agent, identity, env: merged };
  } catch (e) {
    if (!(e instanceof GwsIdentityError)) throw e;
    return {
      ok: false,
      message:
        `Agent '${agent}' may act here, but its env file ${envPath} has no usable Google identity: ` +
        `${e.message} Add the GWS_* values to the agent's .env.tpl (or its canopy-web credentials) ` +
        `and run \`canopy cred refresh --agent ${agent}\`.`,
    };
  }
}

export interface SessionGate {
  /** null when allowed (identity applied); otherwise the refusal text. */
  ensure(): Promise<string | null>;
  invalidate(): void;
}

/**
 * @param apply  called once per successful resolution with the agent's GWS_*
 *               values; the server copies them into process.env and rebuilds
 *               its Google clients.
 */
export function createSessionGate(
  apply: (r: Extract<Resolution, { ok: true }>) => void,
  deps: ResolveDeps = {},
): SessionGate {
  let allowed = false;
  let inflight: Promise<string | null> | null = null;
  return {
    async ensure() {
      if (allowed) return null;
      inflight ??= (async () => {
        try {
          const r = await resolveSessionIdentity(deps);
          if (!r.ok) return `${r.message}\n\n${DRIVE_REFUSAL_HINT}`;
          apply(r);
          allowed = true;
          return null;
        } finally {
          inflight = null;
        }
      })();
      return inflight;
    },
    invalidate() {
      allowed = false;
    },
  };
}

type AnyFn = (...args: unknown[]) => unknown;
interface GateableServer {
  tool?: AnyFn;
  registerTool?: AnyFn;
}

/** Gate every tool registered AFTER this call; install it before the first registration. */
export function installSessionGate(server: GateableServer, gate: SessionGate): void {
  for (const method of ['tool', 'registerTool'] as const) {
    const original = server[method];
    if (typeof original !== 'function') continue;
    const bound = original.bind(server) as AnyFn;
    server[method] = ((...regArgs: unknown[]) => {
      const last = regArgs[regArgs.length - 1];
      if (typeof last === 'function') {
        const handler = last as AnyFn;
        regArgs[regArgs.length - 1] = async (...callArgs: unknown[]) => {
          const refusal = await gate.ensure();
          if (refusal) return { isError: true, content: [{ type: 'text', text: refusal }] };
          try {
            const out = await handler(...callArgs);
            if (out && typeof out === 'object' && (out as { isError?: unknown }).isError === true) {
              gate.invalidate();
            }
            return out;
          } catch (err) {
            gate.invalidate();
            throw err;
          }
        };
      }
      return bound(...regArgs);
    }) as AnyFn;
  }
}
