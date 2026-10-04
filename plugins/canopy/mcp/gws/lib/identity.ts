/**
 * Per-agent identity + write-scope resolution for the canopy-gws MCP server.
 *
 * One server, many agents: the subprocess resolves WHO it acts as and WHERE
 * it may write entirely from session environment variables, exported per
 * agent (settings/env or canopy provision). There is deliberately NO
 * fallback to any default identity — if the identity env is absent the
 * server must fail loudly at startup rather than act as a shared account.
 *
 * Contract (jjackson/canopy#262):
 *   GWS_IDENTITY_MODE     — "sa" | "gog". Required.
 *   GWS_SA_KEY_PATH       — sa mode: absolute path to the service-account
 *                           JSON key. Required in sa mode.
 *   GWS_GOG_ACCOUNT       — gog mailbox used by the read_personal_drive_doc
 *                           atom (subprocess shell-out; independent of the
 *                           googleapis client identity).
 *   GWS_GOG_CLIENT        — optional override; absent, the client comes from
 *                           `canopy email client` (the fleet's one rule).
 *   GWS_ROOT_FOLDER_ID    — optional: the agent's default working root folder.
 *                           Surfaced via drive_diagnose; not enforced per-call.
 *   GWS_ALLOWED_DRIVE_IDS — optional: comma-separated Shared Drive IDs the
 *                           server may write to. When set, every write probe
 *                           rejects parents outside the allowlist.
 *
 * gog mode for the googleapis client itself is NOT implemented yet (gog
 * stores OAuth tokens in its own credential store; bridging them into
 * google-auth-library is a tracked follow-up). GWS_IDENTITY_MODE=gog
 * therefore fails at startup with a message saying so.
 */
import fs from 'fs';

export type GwsIdentityMode = 'sa' | 'gog';

export interface GwsIdentity {
  mode: GwsIdentityMode;
  /** Absolute path to the SA JSON key (sa mode). */
  saKeyPath?: string;
}

export class GwsIdentityError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'GwsIdentityError';
  }
}

const HELP =
  'canopy-gws resolves its Google identity from per-agent session env. ' +
  'Set GWS_IDENTITY_MODE=sa and GWS_SA_KEY_PATH=/path/to/sa-key.json in the ' +
  "agent's environment (settings env block or `canopy provision`). " +
  'There is no default identity fallback by design.';

/**
 * Resolve the server's Google identity from env. Throws GwsIdentityError
 * with an actionable message naming the exact vars when misconfigured.
 *
 * @param env  injectable for tests (defaults to process.env)
 * @param exists  injectable fs.existsSync for tests
 */
export function resolveIdentityFromEnv(
  env: Record<string, string | undefined> = process.env,
  exists: (p: string) => boolean = fs.existsSync,
): GwsIdentity {
  const mode = env.GWS_IDENTITY_MODE;
  if (!mode) {
    throw new GwsIdentityError(`GWS_IDENTITY_MODE is not set. ${HELP}`);
  }
  if (mode === 'sa') {
    const keyPath = env.GWS_SA_KEY_PATH;
    if (!keyPath) {
      throw new GwsIdentityError(
        `GWS_IDENTITY_MODE=sa requires GWS_SA_KEY_PATH (path to the service-account JSON key). ${HELP}`,
      );
    }
    if (!exists(keyPath)) {
      throw new GwsIdentityError(
        `GWS_SA_KEY_PATH points at a file that does not exist: ${keyPath}. ${HELP}`,
      );
    }
    return { mode: 'sa', saKeyPath: keyPath };
  }
  if (mode === 'gog') {
    throw new GwsIdentityError(
      'GWS_IDENTITY_MODE=gog is not implemented yet for the googleapis client — ' +
        'use sa mode (GWS_IDENTITY_MODE=sa + GWS_SA_KEY_PATH). gog-mode auth is a ' +
        'tracked follow-up. (The read_personal_drive_doc atom independently shells ' +
        'out to the gog CLI via GWS_GOG_ACCOUNT/GWS_GOG_CLIENT and works regardless.)',
    );
  }
  throw new GwsIdentityError(
    `Unrecognized GWS_IDENTITY_MODE "${mode}" (expected "sa" or "gog"). ${HELP}`,
  );
}

/**
 * Parse the optional write-scope allowlist. Returns null when unset/blank
 * (no restriction beyond the Shared-Drive requirement); otherwise a
 * non-empty list of Shared Drive IDs.
 */
export function parseAllowedDriveIds(
  env: Record<string, string | undefined> = process.env,
): string[] | null {
  const raw = env.GWS_ALLOWED_DRIVE_IDS;
  if (!raw) return null;
  const ids = raw
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
  return ids.length > 0 ? ids : null;
}
