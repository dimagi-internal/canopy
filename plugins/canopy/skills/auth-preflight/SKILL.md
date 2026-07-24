---
name: auth-preflight
description: Fast auth health check before long deploy/workflow runs — covers gh, 1Password, and AWS SSO (labs profile).
---

# Auth Preflight

Run this before any long-running deploy, workflow, or infrastructure task that
will touch GitHub, 1Password, or AWS labs. The purpose is to surface stale
credentials up-front (AWS SSO token expiry, 1Password sign-out, missing `gh`
auth) instead of mid-deploy when recovery is awkward and out-of-band.

The check probes `gh auth status`, `op whoami`, and — only when labs work is
likely (cwd or git remote contains `ace-web`, `connect-labs`, or
`connect-search`) — `aws sts get-caller-identity --profile labs`. Each line
prints `OK`, `FAIL` with a recovery command, or `NOT INSTALLED`. The script
exits 0 if all checks pass, 1 if any fail.

When to invoke: at the start of a deploy/workflow conversation, or whenever
the user says "deploy", "ship", "push to prod", or similar long-running flows
on labs repos. Fast: completes in well under 3 seconds.

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
bash "$CANOPY_ROOT/scripts/canopy-auth-preflight.sh"
```

If a check fails, run the printed recovery command and re-run the preflight.
The `canopy:canopy-doctor` skill also includes these checks in its standard run.
