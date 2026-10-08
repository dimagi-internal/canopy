"""The fleet's "decide, don't offer" Stop rail — rendered as a NATIVE prompt hook.

An agent must not end a session by handing back a call it was authorized to make
("Want me to ship the PR?", "It's a small canopy PR if you want it.") instead of making
it. This used to be a regex engine (`agent-core/decide_guard.py`), and it was
whack-a-mole: on 2026-10-04 one ada session got past it three times with shapes nobody
had written a pattern for. Jonathan's call: judge it with an LLM fed labelled examples,
and let it use its judgment on phrasings the examples don't show.

The prompt can be wired at two scopes, and `PLUGIN_WIDE` picks between them:

* **plugin-wide** (`PLUGIN_WIDE = True`): a `{"type": "prompt"}` Stop entry in the canopy
  plugin's `hooks/hooks.json`. It fires in EVERY session on every machine with canopy
  installed, which makes one person's working preference everyone's.
* **opt-in** (`PLUGIN_WIDE = False`, the current setting): the plugin carries no entry. The
  same entry is STAMPED by `canopy decide-guard stamp` into each place that wants it:
  - an agent repo's `.claude/settings.json` (`--agent-repo DIR`): the agent's own rail,
    shipped as part of that agent;
  - a person's `~/.claude/settings.json` (`--user`): a personal preference for every
    session on that macOS account.
  Cost: a new example reaches a stamped place only when it is re-stamped (`stamp --check`
  finds the stale ones).

History: plugin-wide from 2026-10-04 (Jonathan's choice at the time); moved to opt-in on
2026-10-08, when Jonathan asked that it stop being imposed on every canopy user. It is now
his own preference plus a rail in the agents that want it.

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

# The scope switch — see the module docstring. False = opt-in (stamped per agent repo / user).
PLUGIN_WIDE = False

# The regex loader command hook that an agent-only stamp replaces.
LOADER_MARKER = "hooks/decide_guard.py"

# Marks our entry in hooks.json so a re-render replaces it instead of appending.
STATUS_MESSAGE = "decide-guard: checking whether the close hands back a call"

# The prompt is sent on EVERY Stop of every canopy session, and shown to the agent in full
# when it blocks — so it must stay SHORT (Jonathan, 2026-10-04: the 12K-char first version
# was "way way too much text"). Only examples marked `"in_prompt": true` are rendered; the
# rest of decide_guard_examples.jsonl is the held-out eval set. PROMPT_BUDGET is enforced.
PROMPT_BUDGET = 3000

_HEADER = """\
Judge an AI agent's final message (last_assistant_message in the input below).
If stop_hook_active is true, respond {"ok": true} and stop.

Block only if the message ENDS by leaving undone something the agent could do itself \
without approval — code, PRs, merges, repo ops, its own tooling, dispatching an agent — \
whether it asks ("want me to…"), parks it ("…if you want it", "unless you'd rather", \
"would be a small follow-up") or floats it as an option.
Fine: outbound waits for a human (send, reply, publish, post, share, notify, production \
deploys); a finished report; a stated default it will act on ("otherwise I'll…", "unless you \
object"); work already routed; a pointer to info; a fork it says is the human's to weigh \
(taste, priorities, budget); work it says THIS turn may not do without approval (manual turn \
mode, a caller who is not the owner — the session's rules outrank this hook); an answer to a \
request that asked only for ideas, an audit or a question, not the build. Use judgment beyond \
the examples; when unsure, allow — a wrong block costs a judge call, a wasted turn and a human's cancel.

"""

_FOOTER = """
Respond with JSON only: {"ok": true}, or {"ok": false, "reason": "<one sentence: what \
you handed back>. If it needs no approval, do it now; if it is outbound or truly the \
human's call, say so in one line and stop."}

Input: $ARGUMENTS"""


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
    label = "BLOCK" if row["handback"] else "FINE"
    return f"{label}: {_escape(json.dumps(row['closing'], ensure_ascii=False))}"


def prompt_examples(examples: list[dict]) -> list[dict]:
    """The few examples rendered into the prompt; every other row is held out for eval."""
    return [r for r in examples if r.get("in_prompt")]


def render_prompt(examples: list[dict]) -> str:
    shown = prompt_examples(examples)
    ordered = [r for r in shown if r["handback"]] + [r for r in shown if not r["handback"]]
    prompt = _HEADER + "\n".join(_example_line(r) for r in ordered) + "\n" + _FOOTER
    if len(prompt) > PROMPT_BUDGET:
        raise ValueError(f"decide-guard prompt is {len(prompt)} chars, over the "
                         f"{PROMPT_BUDGET}-char budget — show fewer or shorter examples")
    return prompt


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


def user_settings_path() -> Path:
    """The person's own Claude Code settings — the `--user` stamp target."""
    return Path.home() / ".claude" / "settings.json"


def stamp_settings(settings_path: Path, examples_path: Path = EXAMPLES_PATH) -> bool:
    """Opt-in scope: write the prompt hook into a settings.json (an agent repo's
    .claude/settings.json, or a person's ~/.claude/settings.json), replacing any regex
    loader command hook. A missing file is created. True if the file changed."""
    settings_path = Path(settings_path)
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text("{}\n", encoding="utf-8")
    current, fresh = _fresh_settings(settings_path, examples_path)
    if fresh == current:
        return False
    Path(settings_path).write_text(fresh, encoding="utf-8")
    return True


def settings_in_sync(settings_path: Path, examples_path: Path = EXAMPLES_PATH) -> bool:
    current, fresh = _fresh_settings(settings_path, examples_path)
    return current == fresh


def add_example(closing: str, handback: bool, why: str,
                examples_path: Path = EXAMPLES_PATH, in_prompt: bool = False) -> dict:
    """Append one labelled example (refusing an exact duplicate closing).

    Held out (eval only) by default; `in_prompt=True` also shows it to the judge — spend that
    sparingly, the prompt has a hard PROMPT_BUDGET."""
    row = {"closing": closing.strip(), "handback": bool(handback), "why": why.strip()}
    if in_prompt:
        row["in_prompt"] = True
    if not row["closing"] or not row["why"]:
        raise ValueError("closing and why must be non-empty")
    existing = load_examples(examples_path)
    if any(r["closing"] == row["closing"] for r in existing):
        raise ValueError("an example with this exact closing already exists")
    with open(examples_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row
