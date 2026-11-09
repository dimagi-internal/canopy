# Session-analysis surfaces

`analyze` / `improve` / `agent-review` / `session-review` / `select-session` are overlapping peers with no
single entry point. Reach for the row that matches your **grain** (flags: `canopy <cmd> --help`):

| Grain | Use | What it is |
|-------|-----|------------|
| One specific transcript | `canopy analyze <transcript.jsonl>` | Analyze a single session file from any cwd; no registry or checkout required. |
| Broad batch across sessions | `canopy improve` | The full analyze, propose, implement cycle over discovered transcripts. |
| Menu-driven pick + act | `/canopy:select-session` / `/canopy:session-review` | Interactive: choose a project/session, analyze, propose, implement behind gates. |
| One AGENT's own turns | `canopy agent-review <slug>` / `/canopy:agent-review` | Reviews an agent's recent TURN transcripts for operating-model friction. Assembles its prompt inline (below). |

## Prompt loading: one rule per tier

- **PRODUCT, user-editable templates** (`analyze` / `propose` / `review` / `briefing` / `verify-findings`)
  live in external `src/orchestrator/prompts/*.md`, loaded install-safely via `importlib.resources`
  (`src/orchestrator/prompts/__init__.py::load_prompt`), robust when the wheel is relocated or zipped.
- **FRAMEWORK logic-prompts** (`src/orchestrator/agent_review.py::build_review_prompt`,
  `src/orchestrator/fleet_align.py::build_judgment_prompt`) are **inline**: static, co-located with the
  logic they serve, and a Python string literal always ships. Loading the PRODUCT `prompts/` package from a
  FRAMEWORK module would break the framework-to-product boundary (`tests/test_plugin_boundary.py`).

The split is deliberate; don't collapse it. A new framework judge-prompt is inlined.
