"""The fleet's "decide, don't offer" Stop rail — rendered as a NATIVE prompt hook.

An agent must not end a session by handing back a call it was authorized to make
("Want me to ship the PR?", "It's a small canopy PR if you want it.") instead of making
it. This used to be a regex engine (`agent-core/decide_guard.py`), and it was
whack-a-mole: on 2026-10-04 one ada session got past it three times with shapes nobody
had written a pattern for. Jonathan's call: judge it with an LLM fed labelled examples,
and let it use its judgment on phrasings the examples don't show.

So the rail is now a `{"type": "prompt"}` Stop hook in the canopy plugin's
`hooks/hooks.json`, and THIS module is the only thing that writes its prompt:

* `agent-core/decide_guard_examples.jsonl` is the source of truth — one
  `{"closing", "handback", "why"}` per line. Add a case there (or with
  `canopy decide-guard add-example`) and re-render; never hand-edit hooks.json.
* `render_prompt()` turns the examples into the prompt; `sync_hooks_json()` writes it
  into the Stop entry; `tests/test_decide_guard_prompt.py` fails if the two drift.

Facts the prompt depends on (probed live 2026-10-04):
* the hook's `$ARGUMENTS` carries `last_assistant_message` and `stop_hook_active`;
* WITHOUT the stop_hook_active rule a blocking prompt hook re-blocks forever (the probe
  blocked 10x in a row) — so that rule is the prompt's FIRST instruction;
* `model` must be a full id (`"haiku"` is rejected as unrecognized).
"""
from __future__ import annotations

import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "canopy"
EXAMPLES_PATH = PLUGIN_ROOT / "agent-core" / "decide_guard_examples.jsonl"
HOOKS_JSON_PATH = PLUGIN_ROOT / "hooks" / "hooks.json"

# Chosen by the 2026-10-04 eval (see the PR that introduced this module): held-out
# accuracy, the three live misses, and the flag rate on real closing messages.
MODEL = "claude-sonnet-5-5"
TIMEOUT_S = 30

# Marks our entry in hooks.json so a re-render replaces it instead of appending.
STATUS_MESSAGE = "decide-guard: checking whether the close hands back a call"

_HEADER = """\
You are the fleet's "decide, don't offer" check. An AI agent is about to end its turn. \
The hook input JSON is at the bottom.

RULE ZERO, before anything else: if "stop_hook_active" is true, respond {"ok": true} and \
nothing more. The agent has already been nudged once; never block twice.

Otherwise judge "last_assistant_message" — the agent's final message to its human. \
Decide whether it ENDS by handing back a call the agent was authorized to make itself, \
instead of making it.

Whose call is it:
- DEV ACTIONS NEED NO APPROVAL: writing or fixing code, opening / merging / closing PRs \
and issues, branches, deploys, repo and config ops, dispatching another fleet agent, \
fixing the agent's own tooling. A close that defers one of these is a HANDBACK — whether \
it offers it ("want me to…", "say the word", "shall I"), parks it ("…if you want it", \
"would be a small follow-up", "left as a follow-up", "next step would be…", "unless \
you'd rather"), or floats it as an option without doing it.
- OUTBOUND ALWAYS WAITS for a human: sending or replying to email or messages, \
publishing, posting, sharing a document, notifying people. Asking before those is \
correct — NOT a handback.
- Also NOT a handback: a finished report; a stated default ("default is next turn", \
"otherwise I'll…", "unless you object"); a follow-up already routed ("filed as #…", \
"queued"); a pointer to information ("the trace is in the PR if you want the detail"); \
a genuine fork on the human's taste, priorities, budget or risk — ideally with the \
agent's recommendation and reason; a question for information the agent cannot get.
- Only the CLOSE matters: an offer mentioned mid-message and then acted on is not a \
handback.

Labelled examples (HANDBACK = should block; FINE = should not):
"""

_FOOTER = """
Use your AI judgment on phrasings not shown above — the examples illustrate the rule, \
they do not bound it. The test: does the agent know the right end state, could it reach \
it without approval, and did it stop short and leave the call with the human? When \
genuinely unsure, lean toward blocking: a wrong block costs one beat (the agent explains \
in one line and stops), a miss costs the human a full round trip.

Respond with JSON only.
- Not a handback: {"ok": true}
- A handback: {"ok": false, "reason": "<one sentence naming exactly what you handed \
back>. Whose call is this actually? If it is a dev action (code, PR, merge, deploy, repo \
ops) it needs no approval: do it now, in this session, and report what you did and what \
you scoped out. Outbound (send, reply, publish, post, share) always waits for a human — \
if that is this, or it truly turns on the human's taste, priorities or risk, say so in \
one line and stop. This check will not fire again this session."}

Hook input:
$ARGUMENTS"""


def _escape(text: str) -> str:
    """Escape `$` so example text can never be read as a hook placeholder."""
    return text.replace("$", "\\$")


def load_examples(path: Path = EXAMPLES_PATH) -> list[dict]:
    rows = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row.get("closing"), str) or not row["closing"].strip():
            raise ValueError(f"{path}:{n}: 'closing' must be a non-empty string")
        if not isinstance(row.get("handback"), bool):
            raise ValueError(f"{path}:{n}: 'handback' must be true or false")
        if not isinstance(row.get("why"), str) or not row["why"].strip():
            raise ValueError(f"{path}:{n}: 'why' must be a non-empty string")
        rows.append(row)
    return rows


def _example_line(row: dict) -> str:
    label = "HANDBACK" if row["handback"] else "FINE"
    closing = json.dumps(row["closing"], ensure_ascii=False)
    return f"{label}: {_escape(closing)} — {_escape(row['why'])}"


def render_prompt(examples: list[dict]) -> str:
    # Handbacks first, then the fine ones: the contrast reads better grouped.
    ordered = [r for r in examples if r["handback"]] + [r for r in examples if not r["handback"]]
    body = "\n".join(_example_line(r) for r in ordered)
    return _HEADER + body + "\n" + _FOOTER


def hook_entry(prompt: str, model: str = MODEL, timeout: int = TIMEOUT_S) -> dict:
    return {
        "hooks": [{
            "type": "prompt",
            "model": model,
            "prompt": prompt,
            "timeout": timeout,
            "statusMessage": STATUS_MESSAGE,
        }]
    }


def _is_ours(entry: dict) -> bool:
    return any(h.get("statusMessage") == STATUS_MESSAGE for h in entry.get("hooks", []))


def rendered_hooks(hooks: dict, examples: list[dict]) -> dict:
    """`hooks` (a parsed hooks.json) with our Stop entry replaced by a fresh render."""
    out = json.loads(json.dumps(hooks))
    stop = [e for e in out.setdefault("hooks", {}).get("Stop", []) if not _is_ours(e)]
    stop.append(hook_entry(render_prompt(examples)))
    out["hooks"]["Stop"] = stop
    return out


def dumps(hooks: dict) -> str:
    return json.dumps(hooks, indent=2, ensure_ascii=False) + "\n"


def sync_hooks_json(hooks_path: Path = HOOKS_JSON_PATH,
                    examples_path: Path = EXAMPLES_PATH) -> bool:
    """Re-render the Stop prompt into hooks.json. Returns True if the file changed."""
    current = Path(hooks_path).read_text(encoding="utf-8")
    fresh = dumps(rendered_hooks(json.loads(current), load_examples(examples_path)))
    if fresh == current:
        return False
    Path(hooks_path).write_text(fresh, encoding="utf-8")
    return True


def in_sync(hooks_path: Path = HOOKS_JSON_PATH, examples_path: Path = EXAMPLES_PATH) -> bool:
    current = Path(hooks_path).read_text(encoding="utf-8")
    return dumps(rendered_hooks(json.loads(current), load_examples(examples_path))) == current


def add_example(closing: str, handback: bool, why: str,
                examples_path: Path = EXAMPLES_PATH) -> dict:
    """Append one labelled example (refusing an exact duplicate closing)."""
    row = {"closing": closing.strip(), "handback": bool(handback), "why": why.strip()}
    if not row["closing"] or not row["why"]:
        raise ValueError("closing and why must be non-empty")
    existing = load_examples(examples_path)
    if any(r["closing"] == row["closing"] for r in existing):
        raise ValueError("an example with this exact closing already exists")
    with open(examples_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row
