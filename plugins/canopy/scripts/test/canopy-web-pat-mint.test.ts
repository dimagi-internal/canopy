import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { browserOpenCommand, isDirectInvocation, writeTokenFile } from '../canopy-web-pat-mint.js';

// Ported from ace's test/scripts/ace-web-pat-mint.test.ts and adapted to
// canopy's token sink: canopy writes the RAW token (+ trailing newline) to a
// standalone file at ~/.claude/canopy/workbench-token with mode 600, rather
// than ace's `.env` marker-block. The tests therefore cover the file-write
// contract (parent-dir creation, full overwrite/rotate, trailing newline,
// mode 600) instead of ace's marker-block surgery.

let dir: string;
let tokenPath: string;

beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), 'canopy-web-pat-mint-test-'));
  tokenPath = join(dir, 'workbench-token');
});

afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
});

describe('writeTokenFile', () => {
  it('creates the token file when it does not exist', async () => {
    await writeTokenFile(tokenPath, 'token-1');
    const content = await readFile(tokenPath, 'utf8');
    expect(content).toBe('token-1\n');
  });

  it('creates missing parent directories', async () => {
    const nested = join(dir, 'a', 'b', 'c', 'workbench-token');
    await writeTokenFile(nested, 'token-nested');
    const content = await readFile(nested, 'utf8');
    expect(content).toBe('token-nested\n');
  });

  it('overwrites a prior token on rotation (no append, no stale value)', async () => {
    await writeFile(tokenPath, 'old-token\n');
    await writeTokenFile(tokenPath, 'new-token');
    const content = await readFile(tokenPath, 'utf8');

    expect(content).toBe('new-token\n');
    expect(content).not.toContain('old-token');
  });

  it('writes exactly one trailing newline', async () => {
    await writeTokenFile(tokenPath, 'token-nl');
    const content = await readFile(tokenPath, 'utf8');
    expect(content.endsWith('\n')).toBe(true);
    expect(content.match(/\n/g)?.length).toBe(1);
  });

  it('writes file with mode 600', async () => {
    await writeTokenFile(tokenPath, 'token-mode');
    const s = await stat(tokenPath);
    // Mask off file-type bits, just check the permission bits.
    const mode = s.mode & 0o777;
    expect(mode).toBe(0o600);
  });

  it('preserves the raw token verbatim (no trimming or transformation)', async () => {
    const raw = 'AbC123._-base64url~token';
    await writeTokenFile(tokenPath, raw);
    const content = await readFile(tokenPath, 'utf8');
    expect(content).toBe(`${raw}\n`);
  });
});

// ---------------------------------------------------------------------------
// Windows bring-up regressions (@smazumdar, 2026-09-01)
//
// There is no Windows machine in the fleet, so both of these are written
// against PURE, platform-parameterised functions rather than the real
// platform() — otherwise the win32 branch is untestable and stays broken.
// ---------------------------------------------------------------------------

describe('browserOpenCommand', () => {
  const url = 'https://labs.connect.dimagi.com/canopy/auth/cli/authorize/'
    + '?cb=http%3A%2F%2F127.0.0.1%3A53123%2Fcb&state=nonce-abc&label=box-2026-09-01';

  it('uses rundll32 on win32, not the cmd builtin `start`', () => {
    // spawn('start', ...) fails ENOENT — `start` is a cmd.exe builtin, not an
    // executable — and the ENOENT arrives as an async 'error' event that kills
    // the pending loopback listener.
    const [cmd, args] = browserOpenCommand(url, 'win32');
    expect(cmd).toBe('rundll32.exe');
    expect(args[0]).toBe('url.dll,FileProtocolHandler');
    expect(cmd).not.toBe('start');
  });

  it('passes the whole URL as ONE argv entry on win32, ampersands intact', () => {
    // The `cmd /c start` "fix" truncates at the first &, so canopy-web sees no
    // state and renders "missing state" — which reads like a server bug.
    const [, args] = browserOpenCommand(url, 'win32');
    const passed = args[args.length - 1];
    expect(passed).toBe(url);
    expect(passed).toContain('&state=nonce-abc');
    expect(passed).toContain('&label=');
    expect(args).toHaveLength(2);
  });

  it('still uses open on darwin and xdg-open elsewhere', () => {
    expect(browserOpenCommand(url, 'darwin')).toEqual(['open', [url]]);
    expect(browserOpenCommand(url, 'linux')).toEqual(['xdg-open', [url]]);
  });
});

describe('isDirectInvocation', () => {
  it('matches a path that needs percent-encoding (the portable form of the win32 bug)', () => {
    // COVERAGE NOTE. The Windows shape cannot be simulated from macOS:
    // url.pathToFileURL resolves against the HOST platform, so
    // pathToFileURL('C:\\...') yields a relative POSIX URL here rather than
    // 'file:///C:/...'. What IS portable is the defect CLASS — building a file
    // URL by string concatenation instead of pathToFileURL — and a path that
    // needs percent-encoding exhibits it on every platform.
    //
    // On Windows the same divergence is total rather than partial:
    // import.meta.url is 'file:///C:/Users/...' while `file://${argv1}` is
    // 'file://C:\\Users\\...', so the two can NEVER match. main() never ran,
    // and the script exited 0 having printed nothing at all — which looks
    // exactly like success.
    const argv1 = '/Users/shayoni/my canopy/scripts/canopy-web-pat-mint.ts';
    const metaUrl = pathToFileURL(argv1).href;
    expect(metaUrl).toContain('my%20canopy');
    expect(metaUrl).not.toBe(`file://${argv1}`);   // the old comparison — fails
    expect(isDirectInvocation(metaUrl, argv1)).toBe(true);
  });

  it('matches a POSIX argv path', () => {
    const argv1 = '/home/op/canopy/scripts/canopy-web-pat-mint.ts';
    expect(isDirectInvocation(pathToFileURL(argv1).href, argv1)).toBe(true);
  });

  it('is false when imported by a test runner (different file)', () => {
    const argv1 = '/usr/local/bin/vitest';
    const metaUrl = pathToFileURL('/home/op/canopy/scripts/canopy-web-pat-mint.ts').href;
    expect(isDirectInvocation(metaUrl, argv1)).toBe(false);
  });

  it('is false when argv[1] is absent rather than throwing', () => {
    expect(isDirectInvocation('file:///x.ts', undefined)).toBe(false);
  });
});
