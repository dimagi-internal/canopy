# orchestrator tiers — the framework/product boundary

> Wave 0 of the framework harvest. The canopy plugin is "Canopy the framework"
> seen from the plugin side; this doc makes its framework/product split **legible**
> and `tests/test_plugin_boundary.py` makes it **enforced**.

## The invariant

> **FRAMEWORK code never imports PRODUCT code. PRODUCT (and the orchestration
> HUBS) import FRAMEWORK freely.**

- **FRAMEWORK** = the generic, *agent-agnostic* agent-runtime substrate: the
  canopy-web client, agent scaffolding, session/transcript capture + discovery,
  scheduling/safety infra, provisioning, version/structure tooling.
  Any agent could reuse these.
- **PRODUCT** = canopy's *own* features: the self-improvement brain (analyze →
  propose → review), DDD/narrative, walkthrough/portfolio/PM. Bespoke to canopy.

Unlike canopy-web (separate Django apps), the plugin is a single importable
package, so the tiers are **per-module**. The boundary is a *direction, not a
wall* — we do not split `orchestrator/` into `framework/` and `product/`
subpackages (the design doc forbids that decomposition); we hold the arrow and
enforce it.

## Tiers

**FRAMEWORK** (agent-runtime substrate — must not import product):
`agent_cli` · `agent_client` · `agent_coverage` · `agent_doctor` · `agent_email` · `agent_gdoc` · `review_receipt` · `agent_factory` · `agent_web` · `canopy_web` ·
`inbox_filters` · `capture` · `transcripts` · `scanner` · `circuit_breaker` · `rate_limiter` ·
`scheduler` · `paths` · `repo_map` · `repo_paths` · `repo_evidence` ·
`skill_budget` · `skill_catalog` · `skill_runner` · `provision` · `run_log` ·
`version_bump` · `doctor` · `agent_review` · `structure_drift` · `eval_cli` ·
`eval_rubric` · `turn_synthesis` · `session_upload` · `fleet_align` · `session_sources` ·
`work_cursor`

**HUBS** (orchestration / composition roots — wire product into the CLI, the
improvement pipeline, and the web server; allowed to import product, like
canopy-web's `api` app):
`cli` · `pipeline` · `server`

**PRODUCT** (canopy's own features — may import framework):
`analyzer` · `proposer` · `reviewer` · `briefing` · `observations` · `proposals` ·
`campaigns` · `tracker` · `labels` · `patterns` · `router` · `digest` · `harvest` ·
`shareout` · `portfolio_discover` · `openclaw_harvest` ·
`issue_origin` · `verify_findings` · `corpus` · `test_audit` · `prompts`

> `turn_synthesis` was re-tiered PRODUCT → FRAMEWORK: it's a generic,
> dependency-free transcript reducer (stdlib only, no product imports) — the
> agent-agnostic substrate shared by share-session, harvest, and now
> `session_upload` (the packageable transcript uploader behind `canopy agent turn`).

> `session_sources` is FRAMEWORK: the typed, N-source seam for enumerating
> readable session-transcript corpora (local `~/.claude/projects` today; a
> future `kind` per additional runtime). `agent_coverage` (framework) depends on
> it directly; `harvest` (product) now delegates its `user_session_roots` to it
> too, so the `/Users/*/.claude/projects` glob lives in exactly one place.

> `work_cursor` is FRAMEWORK: the generic "what have I already processed?"
> watermark for any skill that runs on a cadence. It replaces four ad-hoc
> implementations (`ada/bin/ada-run-cursor`, `eva/bin/cos-state`,
> `shareout.resolve_default_range`, and `ada/skills/self-review`'s deliberate
> no-state stance). Pure logic is Drive-free and injectable; `DriveCursorStore`
> reuses `agent_gdoc`'s per-agent identity so it works for any agent without
> per-agent code.

Top-level `scripts/` (ddd, narrative, walkthrough), `video-engine/`, and
`plugins/canopy/{skills,commands,agents}/` are all **product** — correct for a
product plugin. The framework substrate is the 30 modules above.

## Enforcement

`tests/test_plugin_boundary.py` (stdlib `ast`, in the normal `uv run pytest` job)
fails if any framework module imports a product module, and if a **new**
`orchestrator` module is left untiered. Keep the tier sets there in sync with this
doc.

> Today every framework module is import-clean — the boundary already holds; this
> just documents + locks it. The only product→framework coupling lives in the 3
> hubs, by design.
