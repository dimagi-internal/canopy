#!/usr/bin/env bash
# Run the docs-related tests hermetically: a throwaway HOME and an environment holding only
# what the tests need, so a developer's real credentials (~/.canopy tokens, device keys,
# CANOPY_* / GH_* / *_TOKEN variables, git identity) are never read and cannot change a result.
#
#   scripts/test-docs.sh            # the docs tests, then the docs-commands gate
#   scripts/test-docs.sh -k budget  # extra pytest arguments pass through
#   CANOPY_WEB_PATH=~/src/canopy-web scripts/test-docs.sh   # also check canopy-web labels (the one variable passed through)
#
# Needs `uv` on PATH. Takes about 15 s once the uv cache is warm.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

# Read these from the real environment BEFORE scrubbing it; neither holds a secret.
uv_bin="$(command -v uv)" || { echo "test-docs.sh: uv is not on PATH" >&2; exit 2; }
uv_cache="${UV_CACHE_DIR:-$("$uv_bin" cache dir)}"

home="$(mktemp -d "${TMPDIR:-/tmp}/canopy-test-home.XXXXXX")"
trap 'rm -rf "$home"' EXIT

TESTS=(
  tests/test_claude_md_budget.py          # CLAUDE.md size and history
  tests/test_cli_text_is_current.py       # --help text and CLI module docstrings: no dates, PRs, names
  tests/test_docs_sync_check.py           # the docs-sync PR gate
  tests/test_docs_commands_check.py       # commands shown in end-user pages exist
  tests/test_docs_ui_labels.py            # canopy-web labels the pages name
  tests/test_docs_ignore.py               # .docs-ignore, and the regen scripts on the repo's docs
  tests/test_version_bump.py              # version bump rules
  tests/test_version_bump_check.py        # the plugin-bump CI guard
  tests/test_cli_version.py
  tests/skills/test_doc_regeneration_scripts.py
  tests/test_old_address_in_docs.py
  tests/test_hermetic_env.py              # proves this script's own promise
)

run() {
  env -i \
    HOME="$home" \
    PATH="$(dirname "$uv_bin"):/usr/local/bin:/usr/bin:/bin" \
    UV_CACHE_DIR="$uv_cache" \
    UV_LINK_MODE=copy \
    XDG_CONFIG_HOME="$home/.config" XDG_CACHE_HOME="$home/.cache" XDG_DATA_HOME="$home/.local/share" \
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
    GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com \
    GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com \
    LANG=C.UTF-8 TMPDIR="$home" TEST_DOCS_HERMETIC=1 \
    ${CANOPY_WEB_PATH:+CANOPY_WEB_PATH="$CANOPY_WEB_PATH"} \
    "$@"
}

run uv run --extra browser pytest -q -p no:cacheprovider "$@" "${TESTS[@]}"
run uv run --extra browser python .github/scripts/docs_commands_check.py
