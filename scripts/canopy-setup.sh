#!/usr/bin/env bash
# canopy-setup.sh
#
# One-shot, idempotent setup for the canopy plugin on a new machine.
# Each step prints its own pass/skip/fail line. Safe to re-run.
#
# Steps:
#   1. ~/.claude/canopy/ state dir (session-log + repo-map will land here)
#   2. ~/emdash-projects/canopy/ main checkout (stable path used by emdash worktrees + CLI)
#   3. PostToolUse capture hook — now PLUGIN-MANAGED (plugins/canopy/hooks/hooks.json
#      registers it via ${CLAUDE_PLUGIN_ROOT}); this step only REMOVES a legacy
#      settings.json registration pointing at a checkout, so the two never double-fire
#   4. workbench-token (per-human PAT; mint via /canopy:canopy-web-pat-mint)
#   5. canopy CLI installed non-editable from the marketplace clone (same source
#      /canopy:update deploys from — never an editable dev-checkout install)
#   6. video-engine node deps (npm ci) for local Remotion render (best-effort)
#   7. canopy-gws MCP node deps (npm install in the installed plugin dir; best-effort)
#
# Exit code: 0 if all required steps pass, 1 otherwise.
# Stdlib bash + python3 only; no plugin dependencies.

set -u

CANOPY_STATE_DIR="$HOME/.claude/canopy"
MAIN_CHECKOUT="$HOME/emdash-projects/canopy"
SETTINGS_FILE="$HOME/.claude/settings.json"
TOKEN_FILE="$CANOPY_STATE_DIR/workbench-token"
REPO_URL="https://github.com/dimagi-internal/canopy.git"

FAILED=0
NEXT_STEPS=()  # Lines appended here are printed as a remediation block at the end.

echo "=== Canopy setup ==="
echo

# ---------- 1. State dir ----------
if [ -d "$CANOPY_STATE_DIR" ]; then
  echo "[1/7] state dir       : OK ($CANOPY_STATE_DIR)"
elif mkdir -p "$CANOPY_STATE_DIR"; then
  echo "[1/7] state dir       : CREATED ($CANOPY_STATE_DIR)"
else
  echo "[1/7] state dir       : FAIL — could not create $CANOPY_STATE_DIR"
  FAILED=1
fi

# ---------- 2. Main checkout ----------
if [ -d "$MAIN_CHECKOUT/.git" ]; then
  echo "[2/7] main checkout   : OK ($MAIN_CHECKOUT)"
else
  if ! command -v git >/dev/null 2>&1; then
    echo "[2/7] main checkout   : FAIL — git not installed"
    FAILED=1
  else
    mkdir -p "$(dirname "$MAIN_CHECKOUT")"
    if git clone --quiet "$REPO_URL" "$MAIN_CHECKOUT" 2>/dev/null; then
      echo "[2/7] main checkout   : CLONED ($MAIN_CHECKOUT)"
    else
      echo "[2/7] main checkout   : FAIL — \`git clone $REPO_URL $MAIN_CHECKOUT\` failed"
      FAILED=1
    fi
  fi
fi

# ---------- 3. Hook registration ----------
hook_already_registered() {
  python3 - <<'PY' 2>/dev/null
import json, sys
from pathlib import Path
s = Path.home() / ".claude" / "settings.json"
if not s.exists():
    sys.exit(1)
d = json.load(open(s))
hooks = d.get("hooks", {}).get("PostToolUse", [])
found = any(
    "post_tool_use.py" in h.get("command", "")
    for entry in hooks
    for h in entry.get("hooks", [])
)
sys.exit(0 if found else 1)
PY
}

# The capture hook is plugin-managed now (plugins/canopy/hooks/hooks.json). If a
# legacy settings.json registration from an older setup exists, remove it so the
# plugin-managed hook takes over (the plugin copy defers while a working legacy
# entry is present, so removal here is what flips it).
if hook_already_registered; then
  if python3 - <<'PY' >/dev/null 2>&1
import json
from pathlib import Path
s = Path.home() / ".claude" / "settings.json"
d = json.loads(s.read_text())
matchers = d.get("hooks", {}).get("PostToolUse", [])
changed = False
for entry in matchers:
    kept = [h for h in entry.get("hooks", []) if "post_tool_use.py" not in h.get("command", "")]
    if len(kept) != len(entry.get("hooks", [])):
        entry["hooks"] = kept
        changed = True
d["hooks"]["PostToolUse"] = [e for e in matchers if e.get("hooks")]
if not d["hooks"]["PostToolUse"]:
    del d["hooks"]["PostToolUse"]
if changed:
    s.write_text(json.dumps(d, indent=2) + "\n")
PY
  then
    echo "[3/7] post-tool hook  : MIGRATED — legacy settings.json entry removed; plugin-managed hook active"
  else
    echo "[3/7] post-tool hook  : FAIL — could not remove legacy settings.json entry"
    FAILED=1
  fi
else
  echo "[3/7] post-tool hook  : OK (plugin-managed via hooks.json — nothing to register)"
fi

# ---------- 4. Workbench token ----------
if [ -s "$TOKEN_FILE" ]; then
  PERMS=$(stat -f "%Lp" "$TOKEN_FILE" 2>/dev/null || stat -c "%a" "$TOKEN_FILE" 2>/dev/null)
  if [ "$PERMS" != "600" ]; then
    chmod 600 "$TOKEN_FILE"
  fi
  echo "[4/7] workbench token : OK ($TOKEN_FILE)"
else
  echo "[4/7] workbench token : MISSING — mint a PAT to enable workbench writes + walkthrough sharing"
  NEXT_STEPS+=(
    "Mint a per-human canopy-web Personal Access Token:"
    "  /canopy:canopy-web-pat-mint"
    "(opens your browser, one click, writes to $TOKEN_FILE chmod 600.)"
  )
  FAILED=1
fi

# ---------- 5. canopy CLI ----------
# Editable install needs a modern PEP 660 build backend. macOS system pip3
# (from Xcode CLT) is too old, so we use uv (preferred) or pipx. If neither
# is installed, we install uv automatically via the official installer.
ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v curl >/dev/null 2>&1; then
    return 1
  fi
  # Progress goes to stderr — install_cli is called inside $(...) so stdout is captured.
  echo "      uv not found — installing via https://astral.sh/uv/install.sh ..." >&2
  if curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1; then
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1
  else
    return 1
  fi
}

# Non-editable, from the marketplace clone — the SAME source /canopy:update
# deploys from, so the CLI always matches main. An editable checkout install
# silently drifts with whatever branch is checked out; a "skip if present"
# check is how the CLI once went 67 versions stale. Always reinstall (cheap).
CLI_SOURCE="$HOME/.claude/plugins/marketplaces/canopy"
[ -f "$CLI_SOURCE/pyproject.toml" ] || CLI_SOURCE="$MAIN_CHECKOUT"

install_cli() {
  local target="$1"
  if ensure_uv; then
    if uv tool install --reinstall --force "$target" >/dev/null 2>&1; then
      echo "uv tool"
      return 0
    fi
  fi
  if command -v pipx >/dev/null 2>&1; then
    if pipx install --force "$target" >/dev/null 2>&1; then
      echo "pipx"
      return 0
    fi
  fi
  return 1
}

if [ -f "$CLI_SOURCE/pyproject.toml" ]; then
  METHOD=$(install_cli "$CLI_SOURCE") || true
  if [ -n "${METHOD:-}" ] && command -v canopy >/dev/null 2>&1; then
    echo "[5/7] canopy CLI      : INSTALLED via $METHOD ($(command -v canopy))"
  elif [ -n "${METHOD:-}" ]; then
    echo "[5/7] canopy CLI      : INSTALLED via $METHOD — but \`canopy\` not on current PATH"
    NEXT_STEPS+=(
      "Add ~/.local/bin to your shell PATH so the \`canopy\` CLI is reachable:"
      "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
      "  source ~/.zshrc   # or open a new terminal"
    )
    FAILED=1
  else
    echo "[5/7] canopy CLI      : FAIL — uv install failed and no pipx fallback"
    NEXT_STEPS+=(
      "Install uv (or pipx) manually and re-run /canopy:setup:"
      "  brew install uv     # recommended"
      "  # or: brew install pipx"
    )
    FAILED=1
  fi
else
  echo "[5/7] canopy CLI      : SKIP — no marketplace clone or main checkout with pyproject.toml found"
  FAILED=1
fi

# ---------- 6. Video engine node deps (best-effort) ----------
# The general Remotion video engine (local DDD / connect-ddd-walkthrough
# render) lives at video-engine/. Its node_modules is uncommitted, so install
# it once with npm ci. Best-effort: a machine without npm (or that never
# renders video) shouldn't fail setup — this step never sets FAILED.
ENGINE_DIR="$MAIN_CHECKOUT/video-engine"
if [ ! -f "$ENGINE_DIR/package.json" ]; then
  echo "[6/7] video engine    : SKIP — $ENGINE_DIR/package.json missing"
elif [ -d "$ENGINE_DIR/node_modules" ]; then
  echo "[6/7] video engine    : OK (node_modules present)"
elif ! command -v npm >/dev/null 2>&1; then
  echo "[6/7] video engine    : SKIP — npm not installed (needed only for local video render)"
  NEXT_STEPS+=(
    "Install Node/npm, then build the video engine for local render:"
    "  (cd $ENGINE_DIR && npm ci)"
  )
elif (cd "$ENGINE_DIR" && npm ci >/dev/null 2>&1); then
  echo "[6/7] video engine    : INSTALLED (npm ci in $ENGINE_DIR)"
else
  echo "[6/7] video engine    : SKIP — \`npm ci\` failed in $ENGINE_DIR (run it by hand to see why)"
fi

# ---------- 7. canopy-gws MCP node deps (best-effort) ----------
# The canopy-gws MCP server (plugins/canopy/mcp/gws-server.ts) runs via
# `npx tsx` from the INSTALLED plugin dir; its imports resolve against a
# node_modules that ships uncommitted. Install it once per plugin version.
# Best-effort: a machine without npm shouldn't fail setup — this step never
# sets FAILED. Note the server also needs per-agent GWS_* identity env
# (GWS_IDENTITY_MODE / GWS_SA_KEY_PATH) — it fails loud at startup without it.
GWS_PLUGIN_DIR="$(python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null || true)"
if [ -z "$GWS_PLUGIN_DIR" ] || [ ! -f "$GWS_PLUGIN_DIR/package.json" ]; then
  echo "[7/7] gws MCP deps    : SKIP — installed plugin dir (or its package.json) not found"
elif [ -d "$GWS_PLUGIN_DIR/node_modules" ]; then
  echo "[7/7] gws MCP deps    : OK (node_modules present in $GWS_PLUGIN_DIR)"
elif ! command -v npm >/dev/null 2>&1; then
  echo "[7/7] gws MCP deps    : SKIP — npm not installed (needed for the canopy-gws MCP server)"
  NEXT_STEPS+=(
    "Install Node/npm, then install the canopy-gws MCP deps:"
    "  (cd $GWS_PLUGIN_DIR && npm install --no-audit --no-fund)"
  )
elif (cd "$GWS_PLUGIN_DIR" && npm install --no-audit --no-fund >/dev/null 2>&1); then
  echo "[7/7] gws MCP deps    : INSTALLED (npm install in $GWS_PLUGIN_DIR)"
else
  echo "[7/7] gws MCP deps    : SKIP — \`npm install\` failed in $GWS_PLUGIN_DIR (run it by hand to see why)"
fi

# Note: /canopy:walkthrough-share now uses the same ~/.claude/canopy/workbench-token
# PAT covered by step 4 (mint via /canopy:canopy-web-pat-mint). No separate
# step here — one token, multiple consumers (hook + walkthrough-share +
# canopy-doctor).

echo
if [ "$FAILED" -eq 0 ]; then
  echo "All checks passed. Restart Claude Code so the new PostToolUse hook fires,"
  echo "then run /canopy:canopy-doctor to verify the API connection."
else
  echo "=== Next steps ==="
  for line in "${NEXT_STEPS[@]}"; do
    echo "$line"
  done
  echo
  echo "After fixing the items above, re-run \`/canopy:setup\` — it's idempotent and skips completed steps."
fi

exit "$FAILED"
