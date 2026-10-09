/**
 * canopy-gws session identity (canopy#850): act as the session's agent only
 * when `canopy cred check` allows it, with GWS_* from the env file
 * `canopy cred env` resolves — never a fixed or default account.
 */
import { describe, it, expect } from 'vitest';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  DRIVE_REFUSAL_HINT,
  createSessionGate,
  credFailure,
  defaultCredRunner,
  installSessionGate,
  parseGwsEnv,
  resolveSessionIdentity,
  sessionAgent,
  type CredResult,
  type CredRunner,
} from '../../mcp/gws/lib/session-identity.js';

const REFUSED =
  "This is agent 'ada's runner turn (turn t-1), so it may act only as 'ada' — not as 'hal'.";
const ENV_PATH = '/home/x/.hal/.env';
const ENV_FILE = [
  '# hal',
  'GMAIL_ACCOUNT=hal@dimagi-ai.com',
  'GWS_IDENTITY_MODE=sa',
  'export GWS_SA_KEY_PATH="/home/x/.hal/sa.json"',
  "GWS_ALLOWED_DRIVE_IDS='0AAA,0BBB'",
].join('\n');

function runner(map: Record<string, CredResult>): CredRunner & { calls: string[][] } {
  const calls: string[][] = [];
  const run = (async (args: string[]) => {
    calls.push(args);
    return map[args[1]] ?? { status: 1, stdout: '', stderr: 'unexpected' };
  }) as CredRunner & { calls: string[][] };
  run.calls = calls;
  return run;
}
const OK = { status: 0, stdout: '', stderr: '' };
const ENV_OK = { status: 0, stdout: `${ENV_PATH}\n`, stderr: '' };
const deps = (run: CredRunner, env: Record<string, string | undefined> = { CANOPY_AGENT_SLUG: 'hal' }) => ({
  run,
  env,
  readFile: (p: string) => {
    if (p !== ENV_PATH) throw new Error(`ENOENT ${p}`);
    return ENV_FILE;
  },
  exists: () => true,
});

describe('sessionAgent', () => {
  it('prefers CANOPY_AGENT_SLUG, then CANOPY_AGENT', () => {
    expect(sessionAgent({ CANOPY_AGENT_SLUG: 'hal', CANOPY_AGENT: 'ada' })).toBe('hal');
    expect(sessionAgent({ CANOPY_AGENT: 'ada' })).toBe('ada');
    expect(sessionAgent({})).toBe('');
  });
});

describe('parseGwsEnv', () => {
  it('keeps only GWS_* keys and strips quotes / export', () => {
    expect(parseGwsEnv(ENV_FILE)).toEqual({
      GWS_IDENTITY_MODE: 'sa',
      GWS_SA_KEY_PATH: '/home/x/.hal/sa.json',
      GWS_ALLOWED_DRIVE_IDS: '0AAA,0BBB',
    });
  });
});

describe('credFailure', () => {
  it('passes exit 3 stderr through verbatim', () => {
    expect(credFailure('hal', 'check', { status: 3, stdout: '', stderr: REFUSED })).toBe(REFUSED);
  });
  it.each([1, 2, 4])('refuses exit %i', (status) => {
    expect(credFailure('hal', 'check', { status, stdout: '', stderr: '' })).toMatch(/Refusing to act as agent 'hal'/);
  });
  it('refuses a missing CLI', () => {
    const error = Object.assign(new Error('spawn canopy ENOENT'), { code: 'ENOENT' });
    expect(credFailure('hal', 'check', { status: null, stdout: '', stderr: '', error })).toMatch(
      /`canopy` CLI is not installed/,
    );
  });
});

describe('resolveSessionIdentity', () => {
  it("resolves the session agent's GWS identity through check then env", async () => {
    const run = runner({ check: OK, env: ENV_OK });
    const r = await resolveSessionIdentity(deps(run));
    expect(run.calls).toEqual([
      ['cred', 'check', '--agent', 'hal'],
      ['cred', 'env', '--agent', 'hal'],
    ]);
    expect(r).toMatchObject({ ok: true, agent: 'hal', identity: { mode: 'sa', saKeyPath: '/home/x/.hal/sa.json' } });
  });

  it("the agent's env file overrides inherited GWS_* (no fixed account wins)", async () => {
    const r = await resolveSessionIdentity(
      deps(runner({ check: OK, env: ENV_OK }), {
        CANOPY_AGENT_SLUG: 'hal',
        GWS_IDENTITY_MODE: 'sa',
        GWS_SA_KEY_PATH: '/shared/fixed-account.json',
        GWS_ROOT_FOLDER_ID: 'root-1',
      }),
    );
    expect(r.ok && r.identity.saKeyPath).toBe('/home/x/.hal/sa.json');
    expect(r.ok && r.env.GWS_ROOT_FOLDER_ID).toBe('root-1');
  });

  it('refuses a human session without calling canopy', async () => {
    const run = runner({});
    const r = await resolveSessionIdentity(deps(run, {}));
    expect(r.ok).toBe(false);
    expect(run.calls).toEqual([]);
  });

  it('refuses when the broker refuses, and never resolves env', async () => {
    const run = runner({ check: { status: 3, stdout: '', stderr: REFUSED }, env: ENV_OK });
    const r = await resolveSessionIdentity(deps(run));
    expect(r).toEqual({ ok: false, message: REFUSED });
    expect(run.calls).toHaveLength(1);
  });

  it('refuses when the env file carries no GWS identity', async () => {
    const run = runner({ check: OK, env: ENV_OK });
    const r = await resolveSessionIdentity({ ...deps(run), readFile: () => 'GMAIL_ACCOUNT=x\n' });
    expect(r.ok).toBe(false);
    expect(!r.ok && r.message).toMatch(/has no usable Google identity: GWS_IDENTITY_MODE is not set/);
  });

  it('refuses when `canopy cred env` fails', async () => {
    const r = await resolveSessionIdentity(deps(runner({ check: OK, env: { status: 1, stdout: '', stderr: 'op inject failed' } })));
    expect(!r.ok && r.message).toMatch(/cred env --agent hal` error \(exit 1\): op inject failed/);
  });
});

type Handler = (...a: unknown[]) => Promise<unknown>;
function fakeServer() {
  const registered: Record<string, Handler> = {};
  return {
    registered,
    tool(...args: unknown[]) {
      registered[args[0] as string] = args[args.length - 1] as Handler;
    },
  };
}

describe('installSessionGate', () => {
  it('a refused session never reaches the handler; the text ends with the Drive hint', async () => {
    const server = fakeServer();
    const applied: string[] = [];
    installSessionGate(
      server,
      createSessionGate((r) => applied.push(r.agent), deps(runner({ check: { status: 3, stdout: '', stderr: REFUSED } }))),
    );
    let reached = false;
    server.tool('drive_read_file', {}, async () => {
      reached = true;
      return { content: [] };
    });
    const out = (await server.registered.drive_read_file({})) as { isError: boolean; content: Array<{ text: string }> };
    expect(reached).toBe(false);
    expect(applied).toEqual([]);
    expect(out.isError).toBe(true);
    expect(out.content[0].text).toBe(`${REFUSED}\n\n${DRIVE_REFUSAL_HINT}`);
  });

  it('applies the identity once, caches the allow, and re-checks after a failed call', async () => {
    const run = runner({ check: OK, env: ENV_OK });
    const applied: string[] = [];
    const server = fakeServer();
    installSessionGate(server, createSessionGate((r) => applied.push(r.agent), deps(run)));
    server.tool('a', async () => ({ content: [] }));
    server.tool('b', async () => ({ isError: true, content: [] }));
    await Promise.all([server.registered.a(), server.registered.a()]);
    await server.registered.a();
    expect(applied).toEqual(['hal']);
    expect(run.calls).toHaveLength(2);
    await server.registered.b();
    await server.registered.a();
    expect(applied).toEqual(['hal', 'hal']);
    expect(run.calls).toHaveLength(4);
  });
});

describe('defaultCredRunner (canopy mocked via PATH)', () => {
  it('reports the exit code and stderr of a fake canopy', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fake-canopy-'));
    fs.writeFileSync(path.join(dir, 'canopy'), '#!/bin/sh\necho "no: $*" >&2\nexit 3\n', { mode: 0o755 });
    const prev = process.env.PATH;
    process.env.PATH = `${dir}:${prev}`;
    try {
      const r = await defaultCredRunner(['cred', 'check', '--agent', 'hal']);
      expect(r.status).toBe(3);
      expect(credFailure('hal', 'check', r)).toBe('no: cred check --agent hal');
    } finally {
      process.env.PATH = prev;
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it('reports a missing CLI as ENOENT', async () => {
    const prev = process.env.PATH;
    process.env.PATH = fs.mkdtempSync(path.join(os.tmpdir(), 'empty-path-'));
    try {
      const r = await defaultCredRunner(['cred', 'check']);
      expect(r.error?.code).toBe('ENOENT');
    } finally {
      process.env.PATH = prev;
    }
  });
});
