---
name: patch-gstack-browse
description: Use when the gstack `browse` headless browser fails to render WebGL/Mapbox/three.js/deck.gl ("Failed to initialize WebGL", a blank map, NO_WEBGL), or right after a gstack update/upgrade overwrites the browse source — re-applies the SwiftShader WebGL patch and rebuilds the binary.
---

# Patch gstack browse for headless WebGL

gstack `browse` drives headless Chromium, which has no GPU. WebGL context
creation fails there, so any page built on WebGL — **Mapbox GL, three.js,
deck.gl, regl, PixiJS-WebGL** — throws `Failed to initialize WebGL` on init and
the page's script aborts. That makes map/canvas QA impossible via `browse`.

This skill patches `browse/src/browser-manager.ts` to launch Chromium with
`--enable-unsafe-swiftshader` (plus `--use-angle=swiftshader` and
`--ignore-gpu-blocklist`). **SwiftShader** is Chromium's CPU implementation of
the GL APIs — it renders WebGL on the CPU with no GPU present, visually
identical (just slower), which is exactly right for headless screenshot QA.
Chrome deprecated the *automatic* SwiftShader fallback (it JITs in the GPU
process, a risk only for untrusted content), so the explicit flag is required.

**Why a skill:** the patch lives in a vendored tool. A `gstack` self-update
(`/gstack-upgrade`) overwrites `browser-manager.ts` and rebuilds, silently
dropping the patch. Re-run this skill after any gstack update to restore
headless WebGL. It is **idempotent** — safe to run anytime; a no-op if already
patched.

## First: is headless the right browser?

Since gstack v1.7x, gstack drives **[Aside](https://aside.com)** first (macOS
15+) and falls back to its own headless Chromium only when Aside is absent.
Aside is a real browser with a real GPU — WebGL just works, no patch — and it
carries the person's own signed-in sessions, so there are no cookies to import.
For a **person** looking at a map page on their own Mac, installing Aside is
the better answer (gstack never installs it for you).

This skill is for the **headless** path: agent turns, walkthrough renders and
any machine without Aside, where `browse` runs gstack's bundled
`chrome-headless-shell`.

## Two patches, since gstack #2709

1. **Add SwiftShader** to the headless launch args (the original patch).
2. **Stop gstack switching it back off.** gstack #2709 (v1.7x) added
   `headlessGpuArgs()`: on macOS headless it appends `--disable-gpu`,
   `--disable-software-rasterizer`, `--disable-gpu-compositing` and
   `--disable-gpu-watchdog`, to tame a runaway GPU process. Those flags win over
   SwiftShader, so with only patch 1 the browser launches with BOTH sets and
   still reports `NO_WEBGL`. Patch 2 applies `headlessGpuArgs()` only when the
   WebGL patch is opted out of (`BROWSE_DISABLE_WEBGL=1`).

   gstack's own escape, `GSTACK_DISABLE_GPU=off`, does the same thing at runtime
   but has to be in the environment of whichever `browse` call happens to start
   the daemon — every agent, every shell. Patching the source makes it hold.

   **Trade-off:** this gives up #2709's GPU-spin taming while WebGL is on. If a
   headless session pins a CPU core with a GPU process at idle, set
   `BROWSE_DISABLE_WEBGL=1` for runs that do not need WebGL.

## Run

Idempotent: applies each patch only if missing, rebuilds only if something
changed, then **stops** the daemon (so the next call starts the new build) and
verifies both that WebGL works and that the running browser carries the right
flags. Local opt-out at runtime: `BROWSE_DISABLE_WEBGL=1`.

```bash
set -e
GSTACK="${GSTACK_DIR:-$HOME/.claude/skills/gstack}"
SRC="$GSTACK/browse/src/browser-manager.ts"
BIN="$GSTACK/browse/dist/browse"
[ -f "$SRC" ] || { echo "browse source not found at $SRC — is gstack installed?"; exit 1; }

# The daemon is `bun run browse/src/server.ts`, spawned by whichever `browse`
# call starts it. Bun's installer only adds ~/.bun/bin to ~/.zshrc, so a
# non-interactive shell (every agent Bash call) cannot find it, and browse dies
# with 'Executable not found in $PATH: "bun"'.
[ -x "$HOME/.bun/bin/bun" ] && export PATH="$HOME/.bun/bin:$PATH"
command -v bun >/dev/null || { echo "bun not found; install it (curl -fsSL https://bun.sh/install | bash) then re-run"; exit 1; }

CHANGED=0
python3 - "$SRC" <<'PY' && CHANGED=1 || true
import sys, io
path = sys.argv[1]
src = io.open(path, encoding="utf-8").read()
before = src

# Patch 1: SwiftShader flags right after `let useHeadless = true;` in the
# headless launch path (launchArgs is already declared above it).
if "--enable-unsafe-swiftshader" not in src:
    anchor = "let useHeadless = true;"
    assert anchor in src, "patch 1 anchor not found — browse internals changed; patch by hand"
    src = src.replace(anchor, """let useHeadless = true;

    // [canopy:patch-gstack-browse] Headless WebGL via SwiftShader.
    // Headless Chromium has no GPU, so WebGL context creation fails and pages
    // built on it (Mapbox GL, three.js, deck.gl) crash on init. SwiftShader is
    // Chromium's CPU GL implementation; Chrome deprecated the automatic
    // fallback, hence the explicit flag. Opt out at runtime: BROWSE_DISABLE_WEBGL=1.
    // NOTE: a gstack update overwrites this file — re-run /canopy:patch-gstack-browse.
    if (!process.env.BROWSE_DISABLE_WEBGL) {
      launchArgs.push(
        '--enable-unsafe-swiftshader',
        '--use-angle=swiftshader',
        '--ignore-gpu-blocklist',
      );
    }""", 1)
    print("✓ patch 1 (SwiftShader) applied")
else:
    print("✓ patch 1 (SwiftShader) already present")

# Patch 2: gstack #2709's macOS headless GPU-off flags override SwiftShader.
# Only apply them when WebGL is opted out of. Absent on older gstack: skip.
gpu_call = "launchArgs.push(...headlessGpuArgs(process.platform, process.env));"
marker = "[canopy:patch-gstack-browse] headlessGpuArgs"
if marker in src:
    print("✓ patch 2 (keep GPU-off flags away from SwiftShader) already present")
elif gpu_call in src:
    anchor = "    if (useHeadless) {\n      " + gpu_call + "\n    }"
    assert anchor in src, "patch 2 anchor not found — browse internals changed; patch by hand"
    src = src.replace(anchor, """    // """ + marker + """() passes --disable-gpu and
    // --disable-software-rasterizer on macOS (gstack #2709), which switch
    // SwiftShader back off. Only apply them when WebGL is opted out of.
    if (useHeadless && process.env.BROWSE_DISABLE_WEBGL) {
      """ + gpu_call + """
    }""", 1)
    print("✓ patch 2 (keep GPU-off flags away from SwiftShader) applied")
else:
    print("· patch 2 not needed (this gstack has no headlessGpuArgs)")

if src != before:
    io.open(path, "w", encoding="utf-8").write(src)
    sys.exit(0)
sys.exit(1)  # nothing changed
PY

# Rebuild the compiled binary when the source changed (or the binary is stale).
if [ "$CHANGED" = 1 ] || [ "$SRC" -nt "$BIN" ]; then
  echo "Building browse binary…"
  BUILD_LOG=$(mktemp)
  ( cd "$GSTACK" && bun run build ) >"$BUILD_LOG" 2>&1 || { tail -30 "$BUILD_LOG"; echo "build failed (full log: $BUILD_LOG)"; exit 1; }
  # macOS arm64: bun --compile can produce a signature macOS SIGKILLs; re-sign.
  if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
    codesign --remove-signature "$BIN" 2>/dev/null || true
    codesign -s - -f "$BIN" 2>/dev/null || true
  fi
fi

# `stop`, not `restart`: stop kills the Chromium child, so the next command
# starts the daemon fresh from the patched source. A browser left running from
# before the patch keeps its old flags and reads as "the patch didn't work".
"$BIN" stop >/dev/null 2>&1 || true

# Verify WebGL now initializes headlessly (offline; uses about:blank).
"$BIN" goto about:blank >/dev/null 2>&1 || true
RESULT=$("$BIN" js "(()=>{const c=document.createElement('canvas');const gl=c.getContext('webgl2')||c.getContext('webgl');return gl?('WEBGL_OK '+gl.getParameter(gl.VERSION)):'NO_WEBGL';})()" 2>/dev/null | tail -1)
echo "$RESULT"

# And show what the running browser was actually launched with.
PID=$(pgrep -U "$(id -u)" -f 'chrome-headless-shell' | head -1 || true)
if [ -n "$PID" ]; then
  ARGS=$(ps -p "$PID" -o command= | tr ' ' '\n')
  echo "$ARGS" | grep -q -- '--enable-unsafe-swiftshader' && echo "  running with --enable-unsafe-swiftshader"
  echo "$ARGS" | grep -q -- '--disable-software-rasterizer' && echo "  ⚠ running with --disable-software-rasterizer (overrides SwiftShader)"
fi

case "$RESULT" in
  *WEBGL_OK*) echo "✓ headless WebGL working";;
  *) echo "⚠ WebGL still failing — see the flags above; GSTACK_DISABLE_GPU=off in the daemon's env is the runtime escape"; exit 1;;
esac
```

## Notes

- The patch only affects the **headless** launch path; headed mode already has a
  real GPU.
- Stopping the daemon **drops imported auth cookies** — if you were using an
  authenticated session (e.g. labs prod), re-import them after running this.
  `browse cookie-import <file>` only accepts cookies for the page currently
  open, so `goto` the site first, import, then `goto` the page again.
- Every later `browse` call from a non-interactive shell also needs bun on
  `PATH` whenever it has to start the daemon: `export PATH="$HOME/.bun/bin:$PATH"`.
- Target path override: set `GSTACK_DIR` for a non-default install (e.g. a
  repo-vendored `.claude/skills/gstack`).
