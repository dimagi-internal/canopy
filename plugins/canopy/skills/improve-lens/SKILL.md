---
name: improve-lens
description: Run a single lens of canopy's self-improvement loop against a target project. Reads the project's .canopy/lenses/<lens>.yaml descriptor and dispatches the lens-specific analyzer + verifier + PR shipper. Runs in parallel safely with other lenses.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
_CANOPY_DIR="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue.

# Improve-lens

Run **one lens** of canopy's self-improvement loop against a target project. Each lens looks at a different slice of evidence (operational / production / judge) and uses a lens-specific verification protocol (observational / sandbox-regen / re-grade). Lenses are designed to run in parallel — they share no state and can't deadlock each other.

## Arguments

- `--lens <name>` (required) — which lens to run (e.g. `judge`, `production`, `operational`)
- `--project <repo>` (required) — short repo name (e.g. `ace`); resolved via `orchestrator.repo_paths.resolve_repo_path`
- `--run <run-id>` (optional) — for `per_run` lenses, the specific run to analyze. Defaults to latest completed run if omitted.
- `--mode observe|dry-run|implement` (optional, default `dry-run`) — observe writes findings only; dry-run also drafts proposals; implement runs verification + PRs.
- `--max-proposals N` (optional, default 3) — cap proposals per lens-run to bound cost.

Examples:

```text
/canopy:improve-lens --lens judge --project ace --run 20260507-1134
/canopy:improve-lens --lens production --project ace --mode dry-run
/canopy:improve-lens --lens operational --project ace
```

To run all three lenses in parallel from one human session, fan out three messages — they're independent.

## Phase 1 — Resolve project and load descriptors

1. Resolve project path:
   ```bash
   _PROJECT_DIR="$(python3 -c "from orchestrator.repo_paths import resolve_repo_path as r; p=r('<project>'); print(p) if p else None" 2>/dev/null)"
   ```
   If unset, error: "project '<project>' not found on this machine; check orchestrator.repo_paths."

2. Verify `.canopy/` exists at project root:
   ```bash
   test -d "$_PROJECT_DIR/.canopy" || { echo "no .canopy/ in $_PROJECT_DIR"; exit 1; }
   ```
   If absent, error and stop — the project hasn't been canopy-onboarded.

3. Load lens descriptor: `Read $_PROJECT_DIR/.canopy/lenses/<lens>.yaml`.
   If file doesn't exist, error: "lens '<lens>' not declared in project; available lenses: $(ls $_PROJECT_DIR/.canopy/lenses/)".

4. Load run-artifacts descriptor: `Read $_PROJECT_DIR/.canopy/run-artifacts.yaml`.
   This declares what the project produces when it runs — per-run + opp-level artifacts, plus the backend (gdrive vs local-fs vs other) and the read/list tools to use.

## Phase 2 — Bind run_id and resolve evidence paths

For `scope: per_run` lenses (the v1 default), resolve `--run`:
- If provided, validate format and use as-is.
- If omitted, find latest completed run by listing `runs/` in Drive and picking the most recently modified that has `run_state.yaml.phases.*.status == "completed"` for at least one phase. Skip any that match the active /ace:run heuristic (run_state mtime within last 30 min).

Substitute `{run_id}`, `{opp_name}` (if known), and `{project}` placeholders in evidence-source globs.

For `scope: cross_run` lenses (deferred): not implemented in v1; error if requested.

## Phase 3a — Dispatcher-side probes

Some lens-internal probes require dispatching parallel Agents. **Agents cannot dispatch Agents** — hard harness constraint — so multi-agent probes must run here at the dispatcher level, not inside the lens runner.

The lens descriptor's `signals[].probe` field declares which probes a lens needs. Walk all signals, dedupe probes, run each once, stash output keyed by verdict path. Pass to the runner in Phase 3b.

### Probe: `cross_model` (rubric internal-consistency)

For each verdict with score ≥ 6 (and not previously probed in this run), dispatch three parallel Agents in a single message — sonnet, opus, haiku — each grading the verdict's captured artifact with the eval skill's current rubric.

```
Agent(model: "sonnet", subagent_type: "general-purpose", description: "judge probe sonnet", prompt: <grade prompt>)
Agent(model: "opus", subagent_type: "general-purpose", description: "judge probe opus", prompt: <grade prompt>)
Agent(model: "haiku", subagent_type: "general-purpose", description: "judge probe haiku", prompt: <grade prompt>)
```

Grade prompt template:

> Grade the artifact below using ONLY the rubric provided. Do not improvise dimensions.
>
> ## Artifact
> <full text of capture_path artifact>
>
> ## Rubric
> <LLM-as-Judge Rubric section from skills/<eval-skill>/SKILL.md>
>
> Return a YAML verdict matching `lib/verdict-schema.ts` shape — at minimum `overall_score`, per-dimension scores, and `auto_surfaced`. No prose outside the YAML.

Collect three verdicts. Per-dimension mean + variance (max - min). Stash in `cross_model_evidence` keyed by verdict path.

**What this probe answers:** is the rubric's language precise enough that different models read it the same way? High variance = ambiguous anchors.

**What it does NOT answer:** whether the rubric is asking the right questions, or harsh enough. Three models reading the same lenient rubric will all agree on a lenient score.

### Probe: `holistic_adversarial` (rubric scope check)

For each verdict, dispatch one Agent (Opus by default — most thorough) with an adversarial PM-style prompt that does NOT see the rubric. Its job is to find what's actually wrong with the artifact a real-world skeptical reader would catch. Compare what surfaces against what the rubric flagged. The gap = rubric blind spots.

```
Agent(model: "opus", subagent_type: "general-purpose", description: "holistic adversarial read", prompt: <adversarial prompt>)
```

Adversarial prompt template (project-specific specialization comes from the lens descriptor's `holistic_prompt:` field if present; otherwise use generic):

> You are a tough, experienced PM doing an adversarial read of <artifact-type>. Assume this will actually be implemented — real org, real budget. Find what goes wrong, what's hand-waved, what's missing, what's likely to fail. Don't grade with a checklist; surface concerns.
>
> ## Artifact
> <full text of capture_path artifact>
>
> ## Your task
> 1. Top 3 highest-likelihood failure modes. For each: PDD mitigation? real or hand-waved?
> 2. Strongest argument this artifact does NOT deliver useful value.
> 3. Resource-realism check (budget vs labor implied).
> 4. Demand reality (is there a named downstream consumer / pre-committed action?).
> 5. Technical feasibility (claims that need empirical backing — what evidence exists?).
> 6. The $10K bet — odds you'd take on the headline metrics being met.
>
> Return YAML per the lens descriptor's `holistic_output_schema:`. Include `rubric_blind_spots:` — concrete dimensions an experienced reader would flag that the artifact's eval rubric (named in the prompt) has no place to capture. No prose outside the YAML.

Stash output in `holistic_evidence` keyed by verdict path.

**What this probe answers:** does the rubric grade the right things? If the holistic probe surfaces critical concerns the rubric scored 8+ on, the rubric is missing dimensions or compressing its score range too much.

**Cost:** one Opus dispatch per verdict (~30K tokens input). Bound by `--max-proposals`.

### Probe selection

The lens descriptor's `signals` declare which probes are needed. v1 supports `cross_model` and `holistic_adversarial`. Skip Phase 3a entirely if no signal references a dispatcher-side probe.

## Phase 3b — Dispatch lens runner

Each lens has a runner prompt — the lens-specific analyzer that, given evidence (including any dispatcher-side probe results), identifies findings and drafts proposals.

**Runner resolution order** (first hit wins):

1. **Project-local:** `$_PROJECT_DIR/.canopy/lenses/<lens>.runner.md`. Use this for lenses that are domain-specific to a single project (e.g. ACE's `qa-eval-system` lens that audits per-skill QA + Eval registries — meaningful only inside ACE). Project-local runners ship in the project's repo alongside the lens descriptor.
2. **Canopy-bundled:** `skills/improve-lens/lens-types/<lens>.md` (relative to canopy plugin). Use this for lens types meant for any canopy-onboarded project (`judge`, `production`, `operational`).

If neither exists, error: `lens '<lens>' has no runner. Expected one of: $_PROJECT_DIR/.canopy/lenses/<lens>.runner.md OR canopy plugin's skills/improve-lens/lens-types/<lens>.md.`

The bundled runners are project-agnostic and parameterized by the descriptor's evidence/probes/verify blocks. Project-local runners can hard-code project-specific paths and signal logic, since they only ever run against one project.

Dispatch via Agent tool:

```
Agent(
  subagent_type: "general-purpose",
  description: "<lens> lens runner",
  prompt: <runner prompt content + descriptor + evidence pointers + cross_model_evidence + holistic_evidence>
)
```

The runner returns a structured report:

```yaml
findings:
  - id: <hex>
    signal: <signal-id from descriptor>
    target_skill: <skill-name>
    severity: low|medium|high
    description: "..."
    evidence_refs: [...]
proposals:
  - id: <hex>
    finding_id: <id>
    target_file: skills/<skill>/SKILL.md
    target_section: "## ..."
    proposed_edit: |
      <unified diff or replacement text>
    rationale: "..."
```

If `--mode observe`, write findings only and stop.

## Phase 4 — Verification (lens-specific)

For each proposal, run the verification protocol declared in the lens descriptor's `verify:` block.

**`type: re_grade`** (judge lens):
1. Apply proposed edit to a copy of the eval skill's SKILL.md in memory.
2. Dispatch a `general-purpose` Agent that grades the original artifact using the EDITED rubric prompt. Return verdict YAML.
3. Compare new verdict vs original verdict on the dimensions declared in `verify.steps.compare_verdicts`.
4. Pass if all `pass_criteria` are met.

**`type: sandbox_regen`** (production lens):
1. Apply proposed edit to producer skill's SKILL.md in memory.
2. Dispatch `general-purpose` Agent that runs the EDITED producer prompt against the inputs from `inputs-manifest.yaml`. Write artifact to `runs/<run-id>/8-closeout/eval-improve/sandbox/<skill>/<artifact>`.
3. Re-dispatch the matching `-eval` skill against the sandbox artifact.
4. Compare new verdict vs original.
5. Pass if all `pass_criteria` are met.

**`type: observational`** (operational lens):
1. Apply edit locally on the worktree.
2. Run `npm test` (or the project's declared test command).
3. Run `bin/ace-doctor` (or project's health check).
4. Diff inspection: confirm change is confined to declared file patterns.
5. Pass if no test regressions and diff matches declared targets.

Mark each proposal as `verified: pass | fail | error` with comparison evidence attached.

If `--mode dry-run`, write proposals + verification results and stop. Do not open PRs.

## Phase 5 — Open PRs

For each proposal where `verified == pass`:

1. Create a worktree at `~/emdash/worktrees/<project>/emdash/<lens>-<short-id>` off `origin/main`.
2. Apply the proposed edit to the worktree.
3. Bump version per the project's convention (ACE: `bash scripts/version-bump.sh`).
4. Commit with message: `<lens>: <short description> (auto-generated by canopy:improve-lens)`. Include rationale + verification evidence in the commit body.
5. Push + open PR via `gh pr create`. Title: `[<lens>] <short description>`. Body includes:
   - Finding description
   - Verification evidence (before/after verdict diff for re_grade; before/after artifact diff for sandbox_regen)
   - Auto-merge eligibility per descriptor
6. Auto-merge if descriptor's `auto_merge.enabled: true` AND all `auto_merge.conditions` met:
   - Wait for CI green.
   - `gh pr merge --merge`.
   - Update proposal YAML: `status: implemented_and_merged`, `pr_url: ...`.

For proposals where auto-merge isn't eligible, leave the PR open with `[needs-review]` label.

## Phase 6 — Run log

Write to `~/.claude/canopy/runs/improve-lens-<lens>-<project>-<ISO>.yaml`:

```yaml
lens: <lens>
project: <project>
run_id: <run-id or null>
mode: <mode>
started: <ISO>
completed: <ISO>
findings_count: N
proposals_drafted: N
proposals_verified_pass: N
proposals_merged: N
proposals_open_for_review: N
proposals_failed_verification: N
errors: []
```

Show a summary table:

| Proposal | Target | Verified | PR | Status |
|----------|--------|----------|----|----|

## State and idempotency

Findings, proposals, and run logs share the canopy state directory shape used by `canopy:improve`:

- `~/.claude/canopy/observations/<id>.yaml` — findings (with added `lens:` field)
- `~/.claude/canopy/proposals/<id>.yaml` — proposals (with added `lens:` field, `verification_evidence:` block)
- `~/.claude/canopy/runs/improve-lens-*.yaml` — run logs

Before drafting a new proposal, check existing proposals for the same `(lens, target_file, signal)` triple in status `pending` or `failed_verification`. If found and a previous edit was tried, log `[INFO]` and skip — the loop won't re-propose the same fix without new evidence.

## Notes

- **Lenses are independent.** Don't share state across lens runs in the same session. If the user wants all three lenses, they fan out three `improve-lens` calls.
- **Cross-run aggregation is deferred.** `scope: cross_run` lenses error in v1; will be added when ACE state stabilizes enough for cross-run patterns to mean something.
- **Auto-merge is opt-in per lens.** Only the judge lens declares `auto_merge.enabled: true` in ACE today. Production and operational always require human review (irreversible-on-future-runs blast radius).
- **Cost bounding.** `--max-proposals` caps per-run proposals (default 3). Verification-step LLM costs (cross-model probe = 3x, sandbox-regen = 1x producer + 1x eval) dominate; pick the cap thoughtfully.
