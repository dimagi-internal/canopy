"""Pins the fleet gdoc rail (agent-core/gdoc_gate.py): a Doc an agent WROTE cannot be
handed over unreviewed. Ported from eva's tests/test_gdoc_gate.py with the rail itself.

WHY THE RAIL EXISTS. `skills/gdoc-review` shipped a deterministic checker and a SKILL.md
telling the agent to fire it "right after publishing ... and BEFORE sharing the link", and nothing
enforced it. On 2026-08-28 (session 483a4291) the agent published three docs, handed over three
links, and ran the gate zero times — then wrote the diagnosis herself: *"The right move is a
hook that blocks handing over a docs.google.com link that hasn't passed check_gdoc.py."*
Every input was already in context; prose lost anyway. CLAUDE.md's rule for that outcome is
"invariants are hooks, not memory".

WHY IT IS NOT A `config/gating.json` DENY RAIL. The handoff that actually happened was a link
typed into the closing report, not a tool call. A PreToolUse rail sees only tool calls and
would have caught none of the three. So the rail lives on the Stop event.

WHAT IT MUST NOT DO. Fire on a link the agent merely READ — Beth's tracker, a doc a counterpart
sent. False positives on other people's links are the fastest way to teach an agent to ignore
a rail, so the `published/` marker (written only by a doc-WRITING tool call) is what arms it.
And it must never wedge the session: one block per doc, `stop_hook_active` honoured, and every
unexpected condition exits 0.
"""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "plugins", "canopy", "agent-core", "gdoc_gate.py")

DOC_A = "1ENWfnF-B6WwaArM_8zgNLI2cB60rUd_qbVz2xTs13IM"   # a doc from the 8/28 session
DOC_B = "1lFRPuRN25HviWmALrZD58NU8hKsSniu420AMANNFQm0"
URL_A = f"https://docs.google.com/document/d/{DOC_A}/edit?usp=drivesdk"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """A fresh copy of the hook with its marker state pointed at a tmp dir."""
    monkeypatch.setenv("CANOPY_AGENT_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("gdoc_gate_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def transcript(tmp_path, text):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }) + "\n")
    return str(p)


def stop_payload(tmp_path, text, **kw):
    return dict({"hook_event_name": "Stop",
                 "transcript_path": transcript(tmp_path, text)}, **kw)


# --------------------------------------------------------------- arming the rail

WRITES = [
    ("Bash", {"command": f"gog docs format {DOC_A} --match x --link https://e.org"}),
    ("Bash", {"command": "gog api call docs v1 documents.batchUpdate --params "
                         f"'{{\"documentId\":\"{DOC_A}\"}}' --body '{{}}' --allow-write"}),
    ("Bash", {"command": f"gog docs insert {DOC_A} 'x' --index 1 -a eva@dimagi-ai.com"}),
    ("Bash", {"command": f"gog docs find-replace {DOC_A} old new -a eva@dimagi-ai.com"}),
    ("mcp__plugin_chrome-sales_gdrive__docs_batch_update", {"document_id": DOC_A}),
    ("mcp__plugin_chrome-sales_gdrive__docs_insert_email_block", {"document_id": DOC_A}),
    ("mcp__plugin_ace_ace-gdrive__drive_create_doc_from_markdown", {"x": 1}),
]


@pytest.mark.parametrize("tool,inp", WRITES)
def test_a_doc_write_arms_the_rail(gate, tool, inp):
    """Every shape that WRITES a Doc must leave a `published/` marker, whether the id
    arrives in the command, the arguments, or only in the tool's output."""
    assert gate.record({"tool_name": tool, "tool_input": inp,
                        "tool_response": {"url": URL_A}}) == 0
    assert gate.needs_review([DOC_A]) == [DOC_A]


ENGINE_WRITES = [
    f"canopy gdoc publish --md x.md --name X  # -> {URL_A}",
    f'uv run --project "$CANOPY_ROOT" canopy gdoc email-blocks {DOC_A} --blocks b.json',
]


@pytest.mark.parametrize("cmd", ENGINE_WRITES)
def test_engine_writes_are_left_to_the_engine(gate, cmd):
    """`canopy gdoc publish|email-blocks` stamp `published` and run the review themselves.
    Recording them here, AFTER the command returns, would stamp `published` later than the
    receipt the engine just wrote and re-arm the rail on a doc that passed."""
    gate.record({"tool_name": "Bash", "tool_input": {"command": cmd},
                 "tool_response": {"stdout": json.dumps({"url": URL_A})}})
    assert gate.needs_review([DOC_A]) == []


READS = [
    ("Bash", {"command": f"gog docs cat {DOC_A} -a eva@dimagi-ai.com > /tmp/before.txt"}),
    ("Bash", {"command": f"gog docs structure {DOC_A} --json"}),
    ("Bash", {"command": f"gog drive ls  # saw {URL_A}"}),
    ("mcp__plugin_chrome-sales_gdrive__docs_get", {"document_id": DOC_A}),
    ("mcp__plugin_chrome-sales_gdrive__sheets_read", {"spreadsheet_id": DOC_A}),
]


@pytest.mark.parametrize("tool,inp", READS)
def test_reading_a_doc_never_arms_the_rail(gate, tool, inp):
    """Reads run free — the whole guardrail. A doc the agent only looked at is not its to gate."""
    gate.record({"tool_name": tool, "tool_input": inp, "tool_response": {"url": URL_A}})
    assert gate.needs_review([DOC_A]) == []


# ------------------------------------------------------------------- the verdict

def test_blocks_a_link_to_a_doc_written_and_never_gated(gate, tmp_path, capsys):
    """The 2026-08-28 failure, exactly: publish, then hand the link over."""
    gate.record({"tool_name": "Bash", "tool_input": {"command": f"gog docs insert {DOC_A} x --index 1"},
                 "tool_response": {"url": URL_A}})
    assert gate.check(stop_payload(tmp_path, f"Done — the brief is at {URL_A}")) == 2
    err = capsys.readouterr().err
    assert "canopy gdoc check " + DOC_A in err, "the block must name the exact command"


def test_a_pass_clears_it(gate, tmp_path):
    gate.record({"tool_name": "Bash", "tool_input": {"command": f"gog docs insert {DOC_A} x --index 1"},
                 "tool_response": {"url": URL_A}})
    mark_passed(tmp_path, DOC_A)                  # what `canopy gdoc check` does on a pass
    assert gate.check(stop_payload(tmp_path, f"Done — {URL_A}")) == 0


def test_a_republish_after_the_pass_re_arms_it(gate, tmp_path):
    """`canopy gdoc --replace` reuses the doc id AND the old run styling — the italic-bleed
    defect gdoc-review exists to catch. A pass from before the rewrite must not clear it."""
    mark_passed(tmp_path, DOC_A)
    os.utime(os.path.join(str(tmp_path), "gdoc-gate", "passed", DOC_A), (1_000, 1_000))
    gate.record({"tool_name": "Bash",
                 "tool_input": {"command": f"gog docs find-replace {DOC_A} a b"},
                 "tool_response": {"url": URL_A}})
    assert gate.check(stop_payload(tmp_path, f"Republished: {URL_A}")) == 2


def test_a_link_agent_never_wrote_is_not_hers_to_gate(gate, tmp_path):
    """Beth's tracker, a counterpart's doc, a link quoted back out of an email."""
    assert gate.check(stop_payload(
        tmp_path, f"Beth's tracker: https://docs.google.com/document/d/{DOC_B}/edit")) == 0


def test_it_gates_every_unreviewed_doc_in_the_message(gate, tmp_path, capsys):
    """Three docs went out that day, not one."""
    for doc in (DOC_A, DOC_B):
        gate.record({"tool_name": "Bash",
                     "tool_input": {"command": f"gog docs insert {DOC_A} x --index 1"},
                     "tool_response": {"url": f"https://docs.google.com/document/d/{doc}/edit"}})
    mark_passed(tmp_path, DOC_B)
    assert gate.check(stop_payload(
        tmp_path,
        f"v1 {URL_A} and v2 https://docs.google.com/document/d/{DOC_B}/edit")) == 2
    err = capsys.readouterr().err
    assert DOC_A in err and DOC_B not in err, "only the UNREVIEWED doc gets named"


# ----------------------------------------------------------- it cannot wedge the agent

def test_it_blocks_a_given_doc_at_most_once(gate, tmp_path):
    """canopy's own standard: a guard that can wedge the agent is worse than the gap it
    closes. Same at-most-once shape as hooks/decide_guard.py."""
    gate.record({"tool_name": "Bash", "tool_input": {"command": f"gog docs insert {DOC_A} x --index 1"},
                 "tool_response": {"url": URL_A}})
    assert gate.check(stop_payload(tmp_path, URL_A)) == 2
    assert gate.check(stop_payload(tmp_path, URL_A)) == 0


def test_a_later_republish_earns_a_fresh_block(gate, tmp_path):
    """At-most-once is scoped to the doc's last WRITE, not to all time — otherwise the rail
    fires once in a doc's lifetime and every republish after that sails through."""
    gate.record({"tool_name": "Bash", "tool_input": {"command": f"gog docs insert {DOC_A} x --index 1"},
                 "tool_response": {"url": URL_A}})
    assert gate.check(stop_payload(tmp_path, URL_A)) == 2
    assert gate.check(stop_payload(tmp_path, URL_A)) == 0      # same turn: suppressed
    os.utime(os.path.join(str(tmp_path), "gdoc-gate", "blocked", DOC_A), (1_000, 1_000))
    gate.record({"tool_name": "Bash",
                 "tool_input": {"command": f"gog docs find-replace {DOC_A} a b"},
                 "tool_response": {"url": URL_A}})
    assert gate.check(stop_payload(tmp_path, URL_A)) == 2


def test_stop_hook_active_never_loops(gate, tmp_path):
    gate.record({"tool_name": "Bash", "tool_input": {"command": f"gog docs insert {DOC_A} x --index 1"},
                 "tool_response": {"url": URL_A}})
    assert gate.check(stop_payload(tmp_path, URL_A, stop_hook_active=True)) == 0


@pytest.mark.parametrize("payload", [
    {}, {"transcript_path": "/nonexistent/t.jsonl"}, {"transcript_path": None},
])
def test_a_broken_payload_never_blocks(gate, payload):
    assert gate.check(payload) == 0


def test_garbage_on_stdin_never_blocks():
    """End to end, as Claude Code actually invokes it."""
    for mode in ("check", "record"):
        r = subprocess.run([sys.executable, HOOK, mode], input="not json",
                           capture_output=True, text=True)
        assert r.returncode == 0, (mode, r.stderr)


# ------------------------------------------------- the engine and the hook agree

def mark_passed(home, doc_id):
    """Write the receipt exactly as the engine does, so a drift in either side fails here."""
    from orchestrator import gdoc_review
    gdoc_review.mark(home, "passed", doc_id)


def test_engine_receipt_location_matches_the_hook(gate, tmp_path, monkeypatch):
    from orchestrator import gdoc_review
    monkeypatch.setenv("CANOPY_AGENT_HOME", str(tmp_path))
    home = gdoc_review.agent_home("anyagent")
    gdoc_review.mark(home, "published", DOC_A)
    assert gate.needs_review([DOC_A]) == [DOC_A]
    gdoc_review.mark(home, "passed", DOC_A)
    assert gate.needs_review([DOC_A]) == []
