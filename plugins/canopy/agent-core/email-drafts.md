# Email drafts in a Google Doc — fleet procedure

> Any agent that hands a person emails to send (outreach sets, trip or conference macros,
> reply drafts for review) delivers them as **email blocks** in a Doc. Each agent keeps a
> thin skill that points here. The engine is `canopy gdoc email-blocks`
> (`src/orchestrator/gdoc_email_blocks.py`).

## What an email block is

An email block is the To / Cc / Bcc / Subject / Body table that Docs shows with a **Gmail
icon in the right margin**. A person hovers the block, clicks the icon, and gets a Gmail draft
with every field already filled in. The agent drafts and the person sends.

A bold `Subject:` line followed by a quoted body is **not** a draft email. It looks like one,
but clicking it does nothing. Always use the block.

The Docs API has no request that inserts a block by name. The engine builds the same table
with the native block's styling constants (originally chrome-sales' `docs_insert_email_block`),
and Docs gives it the Gmail icon.

## Procedure

1. **Publish the doc with an anchor wherever a block goes.** Use `canopy gdoc publish` as
   usual ([[deliverables]]). Under each email's heading, write the anchor `@@EMAIL_<key>@@`
   alone on its own line, with a blank line either side. The key may use letters, digits,
   `_` and `-`, and each key appears once. Do not write the email text in the markdown; the
   block carries it.

   ```markdown
   ## Acme Health — intro

   @@EMAIL_acme@@
   ```

2. **Insert every block in ONE call**, as the same agent that owns the doc:

   ```bash
   _CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
   CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
   uv run --project "$CANOPY_ROOT" canopy gdoc email-blocks <docId> --blocks blocks.json --repo <agent-repo>
   ```

   Run it through the plugin's **runtime bundle**, as above, and never through the global
   `canopy` on PATH. The session-start hook keeps the runtime at the installed plugin's
   version automatically. It does not touch the global CLI, so a skill that relies on the
   global CLI gets a stale copy, or none, on any machine where nobody has reinstalled it.

   `blocks.json` is a list of `{"anchor", "to", "cc", "bcc", "subject", "body"}`. Only
   `anchor` is required, and `\n` in `body` separates paragraphs. When no address is known,
   put a visible placeholder like `[Name]` in `to`. Never guess an address.

   The engine checks every anchor before it writes anything, and reports every problem at
   once: a missing or duplicated anchor, an anchor on a heading line (the block would take
   the heading's 18pt font), or a misspelled field. It then inserts the blocks last-first,
   so you do no index math, and deletes the anchor tokens it used. A leftover `@@EMAIL_*@@`
   that no block asked for is printed as a warning and stays in the doc, where a reader will
   see it.

3. **Blocks go in LAST.** `canopy gdoc publish --replace` rewrites the whole body and wipes
   the tables. To change an email afterwards, republish the markdown and run step 2 again.

4. **Check it, then share the link** as [[deliverables]] says. Sending the link is still the
   outbound gate.

## Identity

The engine runs as the agent's own Google account, via gog (`gog api call docs v1 …`), and
resolves identity the same way `canopy email` does. The agent that published the doc can
edit it, so no sharing step is needed. That is the reason this moved off chrome-sales, whose
service account had to be granted writer on every doc first.

One exception: **ACE's run docs are owned by its Drive service account**, and ace@ gets a 403
on them. ACE also runs some turns with no shell. ACE therefore keeps a TypeScript copy of this
engine as an MCP tool (`docs_insert_email_blocks`, `ace/lib/docs-email-block.ts`). Change the
two together.

## Related

- `deliverables.md` — how a deliverable is published, filed and shared.
- `turn.md` — reply quality: deliverables are gdocs, not inline drafts.
