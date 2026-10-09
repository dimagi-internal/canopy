#!/usr/bin/env bash
# canopy-auth-preflight.sh
#
# Fast auth health check before any long-running deploy/workflow.
# Probes gh, 1Password (op), and (when labs work is likely) AWS SSO labs profile.
# Prints one pass/fail line per dependency plus a final summary.
#
# Exit code:
#   0 — all checks that ran passed
#   1 — at least one check failed
#
# Stdlib bash only (no Python). Best-effort: missing tools are reported,
# not fatal. Designed to complete in well under 3 seconds.

set -u

FAILED=0
RAN_ANY_FAIL=0

# ---------- gh ----------
if command -v gh >/dev/null 2>&1; then
  if gh auth status >/dev/null 2>&1; then
    echo "gh: OK"
  else
    echo "gh: FAIL — run \`gh auth login\`"
    FAILED=1
  fi
else
  echo "gh: NOT INSTALLED"
fi

# ---------- op (1Password CLI) ----------
if command -v op >/dev/null 2>&1; then
  if op whoami >/dev/null 2>&1; then
    echo "op: OK"
  else
    echo "op: FAIL — run \`op signin --account dimagi.1password.com\`"
    FAILED=1
  fi
else
  echo "op: NOT INSTALLED"
fi

# ---------- AWS labs (only if labs work is likely) ----------
labs_likely() {
  local cwd
  cwd="$(pwd 2>/dev/null || echo)"
  case "$cwd" in
    *ace-web*|*connect-labs*|*connect-search*) return 0 ;;
  esac
  # Check git remote for labs repos.
  if command -v git >/dev/null 2>&1; then
    local remote
    remote="$(git config --get remote.origin.url 2>/dev/null || echo)"
    case "$remote" in
      *ace-web*|*connect-labs*|*connect-search*) return 0 ;;
    esac
  fi
  return 1
}

if labs_likely; then
  if command -v aws >/dev/null 2>&1; then
    if aws sts get-caller-identity --profile labs >/dev/null 2>&1; then
      echo "aws/labs: OK"
    else
      echo "aws/labs: FAIL — run \`canopy aws login --profile labs --reason \"<what you need AWS for>\"\` (pushes the approval to the runner owner's phone)"
      FAILED=1
    fi
  else
    echo "aws/labs: NOT INSTALLED"
  fi
fi

# ---------- summary ----------
if [ "$FAILED" -eq 0 ]; then
  echo "auth-preflight: PASS"
  exit 0
else
  echo "auth-preflight: FAIL"
  exit 1
fi
