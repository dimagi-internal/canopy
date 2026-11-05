# Session-analysis surfaces

`analyze` / `improve` / `agent-review` / `session-review` / `select-session` are overlapping peers
with no single entry point. Reach for the row that matches your **grain**:

| Grain | Use | What it is |
|-------|-----|------------|
| One specific transcript | `canopy analyze <transcript.jsonl> [--propose]` | Analyze a single session file. Runs from any cwd; no registry or checkout required. |
| Broad batch across sessions | `canopy improve` (or `--dry-run` / `--observe-only`) | The full analyze → propose → implement cycle over discovered transcripts. |
| Menu-driven pick + act | `/canopy:select-session` / `/canopy:session-review` | Interactive: choose a project/session, analyze, propose, implement behind gates. |
| One AGENT's own turns | `canopy agent-review <slug>` / `/canopy:agent-review` | Reviews an agent's recent TURN transcripts for operating-model friction. Assembles its prompt inline (below). |

## Prompt loading: one rule per tier

- **PRODUCT, user-editable templates** (`analyze` / `propose` / `review` / `briefing` /
  `verify-findings`) → external `prompts/*.md`, loaded install-safely via `importlib.resources`
  (`src/orchestrator/prompts/__init__.py::load_prompt`), robust when the wheel is relocated or
  zipped.
- **FRAMEWORK logic-prompts** (`agent_review.build_review_prompt`,
  `fleet_align.build_judgment_prompt`) → **inline**: static, co-located with the logic they serve,
  and a Python string literal always ships. Loading the PRODUCT `prompts/` package from a FRAMEWORK
  module would break the framework→product boundary (`tests/test_plugin_boundary.py`).

The split is deliberate; don't collapse it. A new framework judge-prompt is inlined.
