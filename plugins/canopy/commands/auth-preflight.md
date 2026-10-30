---
description: Fast auth health check (gh, AWS labs) before long deploy/workflow runs
allowed-tools: [Bash, Read]
---

# Auth Preflight

Read the auth-preflight SKILL.md from disk and follow it exactly:

```bash
python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); print(d['plugins']['canopy@canopy'][0]['installPath'] + '/skills/auth-preflight/SKILL.md')"
```

Read that file with the Read tool and follow it. The SKILL.md defines the
exact probe sequence for `gh` and AWS labs SSO and what to do when a
check fails. **Do NOT improvise from memory.** The SKILL.md is the
authoritative source.
