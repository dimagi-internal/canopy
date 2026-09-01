---
description: Mint a per-human Personal Access Token (PAT) for canopy-web via a gh-style loopback browser flow, write it to ~/.claude/canopy/workbench-token, and verify. Replaces the WORKBENCH_WRITE_TOKEN / CANOPY_E2E_AUTH_TOKEN shared secrets. One-time per machine; re-run to rotate.
allowed-tools: [Bash]
---

# /canopy:canopy-web-pat-mint

Mints a canopy-web PersonalToken for the human operator (whoever is
signed into canopy-web in their default browser, e.g.
`jjackson@dimagi.com`) via a `gh auth login` style loopback flow. The
token belongs to *you* — not to `ace@dimagi-ai.com` — so any actions
canopy-web attributes (e.g. action records, walkthrough uploads) trace
back to the actual human.

## When to run

- **First-time setup** on a new machine where `/canopy:canopy-doctor`
  shows the workbench-token check failing (`FAIL: workbench-token not
  found`)
- **Rotation** when an existing token is expired, leaked, or you've
  moved laptops (revoke the old one from canopy-web Settings or the
  admin)
- **Per-environment** if you want a separate token labeled for sandbox
  vs. prod use (pass a custom label as the first arg)

## Prerequisites

- A reachable canopy-web at `$CANOPY_WEB_API_URL` (default
  `https://labs.connect.dimagi.com/canopy`; `CANOPY_WEB_BASE` is a legacy alias)
- A signed-in browser session at that URL — or willingness to sign in
  when the browser tab opens

## How to run it

Resolve the script path inside the installed plugin cache and exec
it via `tsx` (Node TypeScript runner). The script lives at
`<installPath>/scripts/canopy-web-pat-mint.ts` (rsync'd from the
plugin source on `/canopy:update`).

```bash
SCRIPT="$(sed -n '/"canopy@canopy"/,/\]/{ s/.*"installPath": *"\([^"]*\)".*/\1/p; }' "$HOME/.claude/plugins/installed_plugins.json" | head -1)/scripts/canopy-web-pat-mint.ts"
npx --yes tsx "$SCRIPT"
```

Custom label:

```bash
npx --yes tsx "$SCRIPT" "jjackson-laptop-prod"
```

Pointed at a different canopy-web (e.g. local dev):

```bash
CANOPY_WEB_API_URL=http://localhost:8000 npx --yes tsx "$SCRIPT"
```

## What it does

1. **Binds a free loopback port** (`socket(0)` on `127.0.0.1`).
2. **Generates a state nonce** (32 bytes urlsafe, binds the listener
   to this specific mint invocation).
3. **Opens your browser** to `${CANOPY_WEB_API_URL}/auth/cli/authorize/?cb=
   http://127.0.0.1:NNNN/cb&state=<nonce>&label=<label>`.
4. **canopy-web** (after `@login_required` bounce through OAuth if
   you're not already signed in) shows a one-click "Authorize CLI
   access" page identifying the label, callback host, and your
   account. Click **Authorize**.
5. **canopy-web mints a PersonalToken** bound to your user, then
   `302`-redirects to `<cb>?token=<raw>&state=<nonce>`.
6. **Local listener** verifies the state nonce, extracts the token,
   writes it to `~/.claude/canopy/workbench-token` (chmod 600),
   responds with a "Token captured" page, and shuts down.

## Output

```
[mint] label=jjackson-laptop-2026-05-27 canopy_web_base=https://canopy-web-...
[1/3] listening on http://127.0.0.1:54321/cb
[2/3] open this URL in your browser to authorize:
  https://canopy-web-…/auth/cli/authorize/?cb=http%3A%2F%2F127.0.0.1%3A54321%2Fcb&state=...&label=jjackson-laptop-2026-05-27
[3/3] waiting up to 5 minutes for callback...
[done] minted "jjackson-laptop-2026-05-27" (43 chars), wrote token to /Users/.../.claude/canopy/workbench-token
       /reload-plugins to pick up the new token, then /canopy:canopy-doctor to verify.
```

## Trust model

The loopback flow is designed so the token never traverses the public
internet beyond the redirect to your own laptop:

- canopy-web's `_validate_callback` rejects any `cb` URL that isn't
  `http://127.0.0.1:*` or `http://localhost:*` (no `https://evil.com`
  attacks)
- The `state` nonce binds the callback to the specific local listener
  that minted it (prevents cross-process race conditions)
- The token goes from canopy-web → 302 redirect → your laptop. No
  upstream proxies, no referer leak, no third-party hops.
- The listener is one-shot: it shuts down after one callback, so even
  if the URL leaks, the port is closed.
- The PersonalToken is immediately revocable from
  `DELETE /api/tokens/{id}/` or the Django admin if you suspect
  compromise.

## Troubleshooting

- **`timeout — no callback received in 5 minutes`** — operator never
  approved. Re-run; complete the flow within 5 minutes.
- **`state mismatch`** — another mint invocation raced for the same
  port (very rare on a single user's laptop). Re-run.
- **Browser didn't open** — copy the URL printed in step `[2/3]` and
  paste it into your browser manually. The listener is waiting on the
  port shown in step `[1/3]`.
- **HTTP 401 from `hooks/post_tool_use.py` afterward** — token not
  picked up by the running hook. Run `/reload-plugins` to refresh, then
  retry. If still 401, run `/canopy:canopy-doctor` and check the
  workbench-token block.
- **Wrong canopy-web** — set `CANOPY_WEB_API_URL` to the right host before
  invoking (e.g. for a labs/staging deploy).

## Related

- `scripts/canopy-web-pat-mint.ts` — the listener + browser-open
- `apps/tokens/cli_authorize_views.py` (canopy-web side) — the
  one-click authorize page + PersonalToken mint
- `/canopy:canopy-doctor` — verifies the workbench-token file is
  present + chmod 600
- `hooks/post_tool_use.py` + `scripts/walkthrough-share/upload.py` —
  primary consumers of the workbench-token
