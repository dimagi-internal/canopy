import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { execFile } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

const run = promisify(execFile);
const HELPER = fileURLToPath(new URL('../canopy-web-mcp-headers.js', import.meta.url));

// This helper used to be a .sh, and `headersHelper` is ONE string for every platform —
// so on Windows, Claude Code handed it to cmd.exe, which cannot execute a .sh and
// returns EMPTY with EXIT 0. No auth header was sent, canopy-web correctly 401'd,
// Claude Code fell back to OAuth dynamic client registration, and the user was shown a
// Connect Labs 404 page that reads like a routing bug or an expired login. Six MCP logs
// on one Windows machine, the same failure in all six; the server had never connected
// there. These tests exercise the contract the MCP client actually depends on.

let dir: string;
let tokenPath: string;

beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), 'canopy-mcp-headers-test-'));
  tokenPath = join(dir, 'workbench-token');
});

afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
});

async function headers(env: NodeJS.ProcessEnv): Promise<{ stdout: string }> {
  return run(process.execPath, [HELPER], { env: { ...process.env, ...env } });
}

describe('canopy-web-mcp-headers', () => {
  it('emits a bearer header from the token file', async () => {
    await writeFile(tokenPath, 'sample-pat-value\n', 'utf8');
    const { stdout } = await headers({ CANOPY_WORKBENCH_TOKEN: tokenPath });
    expect(JSON.parse(stdout)).toEqual({ Authorization: 'Bearer sample-pat-value' });
  });

  it('strips surrounding whitespace, not just the trailing newline', async () => {
    await writeFile(tokenPath, '  sample-pat-value \n\n', 'utf8');
    const { stdout } = await headers({ CANOPY_WORKBENCH_TOKEN: tokenPath });
    expect(JSON.parse(stdout)).toEqual({ Authorization: 'Bearer sample-pat-value' });
  });

  it('emits {} when the token file is missing, and does NOT crash', async () => {
    // A crash here produces empty stdout — exactly the silent, success-shaped failure
    // the .sh version had on Windows. {} is the honest answer: canopy-web then 401s and
    // /mcp surfaces the server as needing auth, which is the cue to mint a PAT.
    const { stdout } = await headers({ CANOPY_WORKBENCH_TOKEN: join(dir, 'nope') });
    expect(JSON.parse(stdout)).toEqual({});
  });

  it('emits {} when the token file is empty', async () => {
    await writeFile(tokenPath, '\n', 'utf8');
    const { stdout } = await headers({ CANOPY_WORKBENCH_TOKEN: tokenPath });
    expect(JSON.parse(stdout)).toEqual({});
  });

  it('always emits parseable JSON on stdout and nothing else', async () => {
    await writeFile(tokenPath, 'sample-pat-value\n', 'utf8');
    const { stdout } = await headers({ CANOPY_WORKBENCH_TOKEN: tokenPath });
    expect(stdout.trim()).toBe(stdout);          // no stray newline/banner to confuse the client
    expect(() => JSON.parse(stdout)).not.toThrow();
  });
});
