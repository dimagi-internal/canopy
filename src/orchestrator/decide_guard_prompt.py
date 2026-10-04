"""The fleet's "decide, don't offer" Stop rail — rendered as a NATIVE prompt hook.

An agent must not end a session by handing back a call it was authorized to make
("Want me to ship the PR?", "It's a small canopy PR if you want it.") instead of making
it. This used to be a regex engine (`agent-core/decide_guard.py`), and it was
whack-a-mole: on 2026-10-04 one ada session got past it three times with shapes nobody
had written a pattern for. Jonathan's call: judge it with an LLM fed labelled examples,
and let it use its judgment on phrasings the examples don't show.

The prompt can be wired at one of TWO scopes, and `PLUGIN_WIDE` picks which:

* **plugin-wide** (`PLUGIN_WIDE = True`): a `{"type": "prompt"}` Stop entry in the canopy
  plugin's `hooks/hooks.json` — fires in EVERY session on a machine with canopy installed.
* **agent-only** (`PLUGIN_WIDE = False`, the current setting): the same entry is STAMPED into
  each agent repo's `.claude/settings.json` Stop hooks by `canopy decide-guard stamp`,
  replacing that repo's `hooks/decide_guard.py` command hook (the regex loader). The plugin's
  hooks.json then carries no entry. Cost: a new example reaches an agent only when its
  settings are re-stamped (one PR per agent repo).

Plugin-wide is NOT approved yet (2026-10-04: the approval turned out to be misrouted), so
the switch is off. Flipping it is: set `PLUGIN_WIDE = True`, `canopy decide-guard render`,
then unstamp/remove the agent wiring so no session is judged twice.

THIS module is the only thing that writes the prompt:

* `agent-core/decide_guard_examples.jsonl` is the source of truth — one
  `{"closing", "handback", "why"}` per line. Add a case there (or with
  `canopy decide-guard add-example`) and re-render; never hand-edit the wired JSON.
* `render_prompt()` turns the examples into the prompt; `sync_hooks_json()` makes the
  plugin's hooks.json agree with `PLUGIN_WIDE`; `stamp_settings()` writes an agent repo's
  settings. `tests/hooks/test_agent_core_decide_guard.py` fails if hooks.json drifts.

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

# The scope switch — see the module docstring. False = agent-only (stamped per repo).
PLUGIN_WIDE = False

# The regex loader command hook that an agent-only stamp replaces.
LOADER_MARKER = "hooks/decide_guard.py"

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


def rendered_hooks(hooks: dict, examples: list[dict], wire: bool = True,
                   drop_loader: bool = False) -> dict:
    """`hooks` (a parsed hooks.json / settings.json) with our Stop entry re-rendered.

    `wire=False` removes our entry instead. `drop_loader=True` also removes the regex
    loader command hook (`hooks/decide_guard.py`) — the agent-only stamp replaces it.
    """
    out = json.loads(json.dumps(hooks))
    all_hooks = out.setdefault("hooks", {})
    stop = []
    for entry in all_hooks.get("Stop", []):
        if _is_ours(entry):
            continue
        if drop_loader:
            kept = [h for h in entry.get("hooks", []) if LOADER_MARKER not in h.get("command", "")]
            if not kept:
                continue
            entry = {**entry, "hooks": kept}
        stop.append(entry)
    if wire:
        stop.append(hook_entry(render_prompt(examples)))
    if stop:
        all_hooks["Stop"] = stop
    else:
        all_hooks.pop("Stop", None)
    return out


def dumps(hooks: dict) -> str:
    return json.dumps(hooks, indent=2, ensure_ascii=False) + "\n"


def _fresh_plugin(hooks_path: Path, examples_path: Path) -> tuple[str, str]:
    current = Path(hooks_path).read_text(encoding="utf-8")
    fresh = dumps(rendered_hooks(json.loads(current), load_examples(examples_path),
                                 wire=PLUGIN_WIDE))
    return current, fresh


def sync_hooks_json(hooks_path: Path = HOOKS_JSON_PATH,
                    examples_path: Path = EXAMPLES_PATH) -> bool:
    """Make the plugin's hooks.json agree with PLUGIN_WIDE. True if the file changed."""
    current, fresh = _fresh_plugin(hooks_path, examples_path)
    if fresh == current:
        return False
    Path(hooks_path).write_text(fresh, encoding="utf-8")
    return True


def in_sync(hooks_path: Path = HOOKS_JSON_PATH, examples_path: Path = EXAMPLES_PATH) -> bool:
    current, fresh = _fresh_plugin(hooks_path, examples_path)
    return current == fresh


def _fresh_settings(settings_path: Path, examples_path: Path) -> tuple[str, str]:
    current = Path(settings_path).read_text(encoding="utf-8")
    fresh = dumps(rendered_hooks(json.loads(current), load_examples(examples_path),
                                 wire=True, drop_loader=True))
    return current, fresh


def stamp_settings(settings_path: Path, examples_path: Path = EXAMPLES_PATH) -> bool:
    """Agent-only scope: write the prompt hook into an agent repo's .claude/settings.json,
    replacing its regex loader command hook. True if the file changed."""
    current, fresh = _fresh_settings(settings_path, examples_path)
    if fresh == current:
        return False
    Path(settings_path).write_text(fresh, encoding="utf-8")
    return True


def settings_in_sync(settings_path: Path, examples_path: Path = EXAMPLES_PATH) -> bool:
    current, fresh = _fresh_settings(settings_path, examples_path)
    return current == fresh


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
