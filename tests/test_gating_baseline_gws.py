"""The fleet gws deny rails — deliverable filing (agent-core/gating-baseline.json).

WHY THIS FILE EXISTS (2026-08-13). `agent-core/deliverables.md` rule 1 is "never My Drive
root", and it was enforced by pattern-matching a hand-listed set of creation verbs. The list
was short and, worse, mostly lived in ONE agent's local config: the baseline shipped a single
rail (`gog drive upload --convert`), Eva had added two more locally, and hal/ada/echo had
none and did not even mount the `gws` channel.

`gog sheets create` was in nobody's list. On 2026-08-12 Eva built a 45-row target roster with
it, straight into her own My Drive root, unshared — a dead link to the @dimagi.com human who
asked for it. Nothing errored.

So these rails moved to the baseline (one fix reaches every agent via /canopy:update) and
were broadened from a verb list to the SHAPE of "creates a Drive object with no destination".
A denylist of verbs always trails the tool surface; that is the failure being pinned here.

Run: uv run pytest tests/test_gating_baseline_gws.py
"""
import json
import re
from pathlib import Path

import pytest

BASELINE = Path(__file__).parent.parent / "plugins" / "canopy" / "agent-core" / "gating-baseline.json"
RAILS = json.loads(BASELINE.read_text())["channels"]["gws"]
BASH_RAILS = [r for r in RAILS if r.get("tool") == "Bash"]
MCP_RAILS = [r for r in RAILS if r.get("tool_pattern")]


def bash_blocks(cmd: str) -> bool:
    return any(re.search(r["pattern"], cmd) for r in BASH_RAILS)


def mcp_blocks(tool: str, tool_input: dict) -> bool:
    """Mirrors hooks/gating_guard.py: tool_pattern over the NAME, pattern over the JSON of
    the input (which is what `_subject` now serializes for a non-built-in tool)."""
    subject = json.dumps(tool_input, sort_keys=True, default=str)
    for r in MCP_RAILS:
        if re.search(r["tool_pattern"], tool) and re.search(r["pattern"], subject):
            return True
    return False


# --------------------------------------------------------------------------------------
# raw gog — every creation verb, not just the ones somebody remembered
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "gog sheets create 'SF Trip Targets'",          # the 2026-08-12 miss, verbatim shape
    "gog docs create 'Concept note'",
    "gog slides create 'Deck'",
    "gog forms create 'Intake'",
    "gog drive mkdir 'Bay Area Trip'",
    "gog drive upload roster.csv --convert",
    "gog sheets new 'Tracker'",                     # `new` is an alias for `create`
    "cd /tmp && gog sheets create 'X'",             # not anchored at start of line
    "foo || gog docs create 'X'",
])
def test_creating_without_a_destination_is_blocked(cmd):
    assert bash_blocks(cmd), f"unrailed Drive create: {cmd!r}"


@pytest.mark.parametrize("cmd", [
    "gog sheets create 'X' --parent ABC123",
    "gog docs create 'X' --parent ABC123",
    "gog drive mkdir 'X' --parent ABC123",
    "gog drive upload f.pdf --parent ABC123",
])
def test_a_destination_satisfies_the_rail(cmd):
    assert not bash_blocks(cmd)


@pytest.mark.parametrize("cmd", [
    "gog sheets read SID 'A1:B2'",
    "gog drive ls --parent ABC",
    "gog drive permissions FILEID",
    "gog calendar events -a eva@dimagi-ai.com",
    "gog gmail search -a eva@dimagi-ai.com --json 'subject:create a doc'",
])
def test_reads_stay_free(cmd):
    """Reads-free is the operating model's first rule — a filing rail must not touch them."""
    assert not bash_blocks(cmd)


@pytest.mark.parametrize("cmd", [
    "gog sheets create --help",
    "gog docs create -h",
    "canopy gdoc publish --help",
])
def test_help_is_never_railed(cmd):
    """Blocking `--help` blocks reading the usage of the command you are being told to use.
    The pre-2026-08-13 canopy-gdoc rail did exactly this."""
    assert not bash_blocks(cmd)


@pytest.mark.parametrize("cmd", [
    "git commit -F /tmp/msg.txt",
    "rg 'docs create' skills/",
    "echo 'note: always pass a destination' >> NOTES.md",
])
def test_prose_and_unrelated_commands_are_not_railed(cmd):
    """Hit live while committing this very change. The PRE-fix rail was lookahead-based and
    UNANCHORED, so a commit message that merely DESCRIBED the railed commands tripped it —
    the agent could not write up its own fix. These rails are anchored to a command position
    (start of line, or after ; & | newline), so text about a command is just text."""
    assert not bash_blocks(cmd)


# --------------------------------------------------------------------------------------
# the canopy publishing engines
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,blocked", [
    ("canopy gdoc publish --md f.md --name N", True),            # falls back to the ROOT
    ("canopy gsheet publish --tab r.tsv --name N", True),
    ("canopy gdoc publish --md f.md --project 'Bay Area Trip'", False),
    ("canopy gsheet publish --tab r.tsv --project 'Bay Area Trip'", False),
    ("canopy gdoc publish --md f.md --area 'Process State'", False),
    ("canopy gdoc publish --md f.md --replace DOCID", False),     # keeps its existing home
    ("canopy gdoc publish --md f.md --parent FOLDER", False),
])
def test_engine_requires_a_destination(cmd, blocked):
    assert bash_blocks(cmd) is blocked


# --------------------------------------------------------------------------------------
# the MCP half — every rail used to be tool:"Bash", so these were wide open
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("tool", [
    "mcp__plugin_chrome-sales_gdrive__drive_create_file",
    "mcp__plugin_chrome-sales_gdrive__drive_create_folder",
    "mcp__plugin_ace_ace-gdrive__drive_create_doc_from_markdown",
    "mcp__plugin_ace_ace-gdrive__drive_create_folder",
    "mcp__gdrive__drive_create_file",          # a different mount of the same server
])
def test_mcp_create_without_a_parent_is_blocked(tool):
    assert mcp_blocks(tool, {"name": "roster"})


@pytest.mark.parametrize("key", ["parent", "parent_id", "parentId", "folder_id", "folderId"])
def test_mcp_parent_argument_satisfies_the_rail(key):
    """Each gdrive server spells it differently; all of them count."""
    tool = "mcp__plugin_chrome-sales_gdrive__drive_create_file"
    assert not mcp_blocks(tool, {"name": "roster", key: "FOLDER123"})


@pytest.mark.parametrize("value", ["", None])
def test_mcp_empty_parent_is_not_a_destination(value):
    """An empty/null parent still lands the file in My Drive root — presence of the KEY is
    not compliance."""
    tool = "mcp__plugin_chrome-sales_gdrive__drive_create_file"
    assert mcp_blocks(tool, {"name": "roster", "parent_id": value})


@pytest.mark.parametrize("tool,inp", [
    ("mcp__plugin_chrome-sales_gdrive__sheets_read", {"spreadsheet_id": "S"}),
    ("mcp__plugin_chrome-sales_gdrive__drive_list_folder", {"folder_id": "F"}),
    ("mcp__plugin_chrome-sales_salesforce__sf_create_record", {"object": "Account"}),
    ("Bash", {"command": "ls"}),
])
def test_mcp_rail_leaves_everything_else_alone(tool, inp):
    """CRM writes run free by design (Eva's config: rails, not gates), reads are free, and
    the MCP rail must not shadow the Bash rails."""
    assert not mcp_blocks(tool, inp)


def test_every_rail_names_the_right_path_not_just_the_wrong_one():
    """Rails, not gates: a block must tell the agent what to do instead, or it just stalls
    the turn. (agent-operating-model §1a; and Eva's 2026-07-30 learning that a rail can be
    right about the risk and wrong about the remedy.)"""
    for r in RAILS:
        msg = r["message"]
        assert "BLOCKED" in msg
        assert "--parent" in msg or "--project" in msg, msg
        assert "deliverables.md" in msg, msg
