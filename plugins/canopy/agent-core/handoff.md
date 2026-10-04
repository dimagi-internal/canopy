# Handoff — moving a project from one agent to another

> **Fleet-canonical process (canopy agent-core).** Run by the **receiving** agent when its principal
> says "take this over from <agent>". To change this process for the whole fleet, PR canopy.

A project's artifacts travel easily; its **reasons** do not. Why the framing changed, which draft
the principal rejected and in what words, which edit was his and which was the agent's — all of
that lives only in the source agent's session transcripts. A receiver that starts from the doc
alone will re-propose what was already turned down. So a handoff is four moves, in this order:
**find and read the sessions → re-home the files → open the project on your board → take over the
thread.**

## 1. Run the command (finds the sessions, opens the project)

```bash
canopy agent handoff --from <source> --to <you> --name "<Project>" \
  --ref <gmail-thread-id> --ref <doc-or-folder-id> [--ref <url> …] \
  [--from-project "<P<N> or name on the source board>"] \
  --drive-folder-url <your Projects/<Project> folder> \
  --outcome "…" --owner "<principal>" --note "<why it moved>" \
  --exclude-session <your session id>          # --dry-run first; it writes nothing
```

It scans the local session transcripts for the refs and lists every session that touched them —
the source agent's first (matched by worktree dir OR slash command, so a resumed session still
counts), the session matching **more** refs leading. It opens the project on your board with
notes naming those sessions, and closes it on the source board when you pass `--from-project`
(a board your PAT cannot see is reported with the exact command for a human, not fatal).

**Refs are what the work is known by.** The thread id the principal wrote on, the doc ids. Pass
several: a session that merely *mentions* a thread id (a sibling's duplicate check) matches one;
the session that did the work matches all of them.

**No source session found ≠ there was none.** Transcripts are per-machine. If the list is empty,
the work may have run on another runner — ask; don't conclude.

## 2. Read the source sessions — before you touch anything

Dispatch a read-only subagent per heavy transcript (they run to megabytes) and have it return a
brief with: the origin request quoted verbatim; every principal instruction **in order** and what
the agent did about it; every artifact id (docs, folders, board tasks, PRs, the last email sent
and to whom); what the principal edited himself; open items and promises not kept. Read the brief,
then open the artifacts it names. The principal's own instruction to the source agent outranks
anything you would have chosen.

## 3. Re-home the files — identity decides who can do it

Files belong under **your** `Projects/<Project>/` (see `deliverables.md`). Moving keeps the URL,
so links already sent still work. But **who may move a file is a property of who owns it**, and
the source agent's files are routinely owned by its *service account*, on *its* shared drive,
invisible to your own account.

- Moving between two shared drives needs **Manager** on the source drive. A folder-level writer
  grant on the destination is not enough — Drive answers `insufficient permissions for this file`.
- **Do not widen grants to force it.** If the owning principal cannot move it:
  1. have it share each file with **your** agent account (writer, notification OFF), and
  2. create a **shortcut** to each in your project folder, named so a reader knows where the
     original lives;
  3. name the literal move as a one-drag task for a human Manager in your closeout.
- A temporary grant you made to bridge identities (e.g. writer on your folder for the source
  service account, so it can create the shortcuts) is **revoked in the same turn** and the
  revocation verified with a permissions read.
- Verify the result as YOU: `gog docs cat <id> -a <your account> --client "$(canopy email client --repo .)"` returns the text.

## 4. Take over the thread

The principal's handoff message is usually on a thread to you; the history is on a thread to the
source agent. **Reply on yours**; carry the source thread's id in the project's refs. The source
agent will still be triggered by a new message on its own thread — so if the counterpart replies
there, it is the source agent's turn that sees it. Say so in your closeout, and when the next
outbound goes to the counterpart, send it from the thread you now own so replies come to you.

**Stay in your lane on adjacent work.** A handoff moves ONE project. A sibling thread of the source
agent's that only *cites* the project (it read the doc for context) stays with the source agent —
name it, don't absorb it.

## Close checklist (state each in the closeout)
1. Sessions found and read (name them; say if none were found and why that might be).
2. Where the files now live — moved, or shortcut + shared — and any human move left to do.
3. Every temporary grant revoked and verified.
4. The project on your board (`P<N>`), and on the source board closed or the manual step named.
5. Which thread you now own, and what still routes to the source agent.

*(Origin: 2026-10-02, the fleet's first agent-to-agent transfer — ACE → Eva, a funder concept
note. The principal asked for it as "a first class canopy operation". Finding the source session
took a hand-rolled grep across ~300 project dirs; the first cross-drive move was refused and
fell back to share + shortcut.)*
