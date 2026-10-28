# Running an agent — the two ways

There are two ways to run an agent. You can use both for the same agent.

1. **Use it yourself in Claude Code.** No runner needed. Run this once on your computer:

   ```bash
   canopy agent use <slug>            # in the project you want to use it from (--dir to name one)
   ```

   Then start a new Claude Code session there and run `/<slug>:turn`.

2. **Let it run on its own.** A runner starts its turns for you: on a schedule, when email
   arrives, or when someone dispatches one. The runner is either your own Mac's runner app (set
   it **Private** and it does only your work) or a fleet cloud box.

|  | Use it yourself | Let it run on its own |
|---|---|---|
| **Who starts turns** | You, in Claude Code | canopy-web: schedules, email, dispatches |
| **What you install** | Claude Code, the canopy plugin (`/canopy:setup`), the agent's plugin. `canopy agent use` installs the plugin. | The runner app on your Mac, or nothing (a fleet cloud box) |
| **Where secrets come from** | The agent's canopy vault, opened on this computer | The agent's canopy vault, opened on the runner |
| **How it's recorded** | Each session is a *local turn* on canopy-web: yours, with its transcript | Each turn on canopy-web, with its transcript |

Schedules fire from canopy-web, so a schedule does not depend on any one machine being awake.

## What `canopy agent use` does

It checks each step, fixes the ones canopy can, and reports **ok**, **done** or **NEEDS YOU**
with the exact URL or person. It is safe to re-run until everything is ok:

- you are signed in to canopy-web (`/canopy:setup` signs you in),
- this computer is registered for local use,
- this computer's device key is enrolled and approved,
- this device holds a grant of the agent's vault,
- you have a licence to use the agent (requested automatically at your first session; an admin
  approves it unless the agent's caller list already gives you the whole agent),
- the agent's plugin is installed (`/plugin install <slug>@canopy-agents`),
- `$CANOPY_AGENT` is set for the project, and the agent's env is synced,
- the agent's Google login is in gog (its OAuth client file, its token, the account mapped),
  so its Gmail and Drive work. The agent acts on Google as itself. An admin's computer gets it
  at once; with a licence it is imported when your first session starts (canopy-web hands a
  licence holder the agent's login only inside their own turn), by
  `canopy agent google-login --if-needed`. It needs `gog` installed — the step names the
  install for your OS. An agent whose mail canopy-web hosts skips this step.

It ends with one line: start a session and run `/<slug>:turn`, or the list of what still needs
you.

## Where your agent's secrets live

In its **canopy vault** on canopy-web. For a zero-knowledge agent canopy-web stores only
ciphertext; values are opened on a device that holds a grant of the agent's vault key.

- **The recovery code** is printed once, when zero-knowledge is turned on. The admin who enabled
  it keeps it offline. It opens the vault if every device and passkey is lost.
- **A device needs a grant.** A new computer or runner appears under **Grant pending** on the
  agent's page (`/agents/<slug>`). An admin who holds a grant presses it (or runs
  `canopy vault zk grant --agent <slug>` from a machine that has access).
- **Changing a value**: the agent's Settings → Credentials, or `canopy vault zk set --agent <slug>
  NAME`. The next session picks it up.

Muse still keeps its secrets in 1Password. That path still works but is **legacy**: an admin with
1Password access moves an agent with `canopy vault import --agent <slug> --zk --switch`.

A computer with no runner app holds its device key too: registering it for local use gives it
its own private "computer" runner on canopy-web, which never runs turns. Until that canopy-web
change is live, `canopy agent use` says "this computer can't hold a vault key yet".
