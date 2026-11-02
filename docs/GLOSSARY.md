# Canopy glossary

One meaning per word. Use these terms in docs, skills and answers; each has an _Avoid_ line for the
wrong word people reach for. Where a term is defined by code, the code is named: if this page and
the code disagree, the code is right and this page is the bug.

**Plugin**: an agent's code — persona, skills, rails — installed into Claude Code from the agent's GitHub repo (`/plugin install <name>@canopy-agents`). Code: `apps/plugins/` in canopy-web.
_Avoid_: "the agent" when you mean the installed code.

**Instance**: canopy-web's `Agent` record for an agent running in one workspace: identity, workspace, vault, runner settings. A local install is bound to exactly one instance.
_Avoid_: "plugin", "the agent's account".

**Catalog**: the marketplace `canopy-agents`, one per signed-in person, listing the plugins of every workspace they belong to. Served by canopy-web; the plugin code itself comes from GitHub with the installer's own git credentials.
_Avoid_: "the marketplace repo" (there is no per-agent marketplace repo to add).

**Local mode / "use it yourself"**: a person runs the agent in their own Claude Code (`canopy agent use <slug>`, then `/<slug>:turn`). Each session is a *local turn*. Secrets are opened on that computer through a vault grant.
_Avoid_: "advanced option", "the CLI route".

**Runner mode / "let it run on its own"**: canopy-web starts turns (schedule, email, dispatch) on a *runner*: a fleet cloud box, or a person's own machine set Private.
_Avoid_: "the remote way", "the service".

**Runner**: a machine that claims queued turns and runs them.

**Turn**: one unit of agent work, scheduled, dispatched or started by a person; recorded on canopy-web with its transcript.

**Turn mode (`manual` | `auto`)**: whether outbound actions wait for the person (`manual`) or run within the agent's rails and are reviewed afterwards (`auto`). Decided by canopy-web at claim; `canopy agent mode` prints it.

**Vault**: an agent's secrets store on canopy-web. Opened per computer by a *device grant* that the vault's keeper approves.

**Licence**: permission for a person to use an agent locally; requested at their first session and approved by the agent's owner or an admin.

**Workspace**: canopy-web's tenancy unit; people and agents belong to workspaces with roles (see `roles.md`).

**Agent core**: the shared procedures and rails that ship with the canopy plugin (`plugins/canopy/agent-core/`). Agents bind to them; they do not copy them.
