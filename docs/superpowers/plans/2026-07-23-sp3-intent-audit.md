# SP3 — harvest intent-audit Implementation Plan

> **Status: shipped (PR #374) — `canopy harvest intent-audit` live. Historical record, not current-state.**

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox syntax.

**Goal:** Add the judgment layer harvest deliberately omits — an intent-fidelity audit that reconstructs what Jonathan asked/decided from HIS OWN words and flags intent-misses (approved-X/shipped-Y, question-read-as-approval, unapproved-judgment, eroded-discipline), emitting findings that pass SP1's evidence-record validator.

**Architecture:** New `intent_audit` + `build_intent_prompt` in `src/orchestrator/harvest.py`, a `harvest intent-audit` CLI subcommand, reusing `strip_session`/`human_messages` (material) and `parse_findings`/`qualify_findings` from `agent_review` (the shared finding path). Mirrors agent-review's LLM-pass shape. Ephemeral — no persistence.

**Tech Stack:** Python 3.14, pytest, click, PyYAML. Repo: `/Users/jjackson/emdash-projects/worktrees/sp3-intent-lens` (branch feat/harvest-intent-audit, off origin/main = SP1+C1+C2).

## Global Constraints
- Test runner: `cd /Users/jjackson/emdash-projects/worktrees/sp3-intent-lens && uv run pytest <file> -q`.
- FRAMEWORK tier: no import from the PRODUCT `prompts/` package; the intent prompt stays an inline string literal (mirror `build_review_prompt`).
- Reuse, don't reimplement: `strip_session`, `human_messages` (harvest.py); `parse_findings`, `qualify_findings` (agent_review.py). Do NOT duplicate the evidence-record schema — it lives in `_valid_evidence`.
- The LLM call mirrors `agent_review._call_verify_llm` (subprocess `claude -p`, fail-loud with a named error, never silent-None). Tests mock it — never hit the network / spawn claude.
- Version bump per PR via `canopy version bump` (atomic; never hand-edit version files). No auto-merge.
- Branch: feat/harvest-intent-audit.

---

### Task 1: build_intent_prompt (pure, testable)

**Files:** Modify `src/orchestrator/harvest.py`; Test `tests/test_harvest.py` (or the harvest test file — grep tests/ for an existing harvest test to match location/style).

**Interfaces:**
- Produces: `build_intent_prompt(stripped: str, human_msgs: list[str]) -> str`.

- [ ] **Step 1: Write the failing test**
```python
from orchestrator.harvest import build_intent_prompt
def test_intent_prompt_has_rubric_material_and_schema():
    p = build_intent_prompt("USER: do X\n\nASSISTANT: I did Y", ["always run the tests first"])
    # embeds the human's own words (the close-read evidence)
    assert "do X" in p and "always run the tests first" in p
    # names the intent-miss classes it must flag
    for term in ["approved", "shipped", "approval", "eroded"]:
        assert term.lower() in p.lower()
    # REQUIRES the SP1 evidence record with a verbatim source_ref quote
    assert "source_ref" in p and "already_fixed_check" in p and "confidence_basis" in p
    assert "verbatim" in p.lower()
```
- [ ] **Step 2: Run → fails** (`ImportError`). `uv run pytest tests/test_harvest.py -k intent_prompt -v`
- [ ] **Step 3: Implement** — an inline-string prompt (mirror `build_review_prompt`'s style) that: states the reviewer role (reconstruct Jonathan's intent from his OWN words, weighted as ground truth); embeds `stripped` (prompt↔response pairs) and the `human_msgs` list; instructs a YAML list of findings, each with `friction_type: intent_miss`, `fix_kind`, `target`, `recommendation`, and an `evidence` RECORD whose `source_ref` is Jonathan's VERBATIM quote + the diverging response, plus `was_read: true`, `already_fixed_check`, `confidence`, `confidence_basis`; and the four intent-miss classes to look for (approved-X/shipped-Y, question-read-as-approval, unapproved-judgment-folded-in, eroded-discipline). End: "Output ONLY the YAML list."
- [ ] **Step 4: Run → passes.**
- [ ] **Step 5: Commit** `feat(harvest): build_intent_prompt for the intent-fidelity audit`

---

### Task 2: intent_audit (orchestration + schema validation)

**Files:** Modify `src/orchestrator/harvest.py`; Test same file.

**Interfaces:**
- Consumes: `build_intent_prompt` (T1); `strip_session`, `human_messages` (harvest); `parse_findings`, `qualify_findings` (agent_review).
- Produces: `intent_audit(path: str, *, use_llm: bool = True, model: str = "sonnet", max_budget_usd: float = 2.0) -> dict` returning `{"session": <stem>, "qualified": [...], "dropped": [...], "error": <str|None>}`. Add `_run_intent_llm(prompt, model, max_budget_usd) -> tuple[list|None, str|None]` mirroring `agent_review._call_verify_llm`.

- [ ] **Step 1: Write the failing tests**
```python
from orchestrator import harvest
def test_intent_audit_no_llm_returns_material_no_findings(tmp_path, monkeypatch):
    # a minimal real jsonl with one human msg + one assistant reply
    j = tmp_path / "s.jsonl"
    j.write_text(
        '{"type":"user","message":{"content":"approve the broad option"}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"shipped the narrow one"}]}}\n')
    out = harvest.intent_audit(str(j), use_llm=False)
    assert out["error"] is None and out["qualified"] == [] and out["dropped"] == []

def test_intent_audit_validates_emitted_findings(tmp_path, monkeypatch):
    j = tmp_path / "s.jsonl"; j.write_text('{"type":"user","message":{"content":"hi"}}\n')
    good = {"title":"approved broad, shipped narrow","friction_type":"intent_miss","fix_kind":"skill_edit",
            "target":"skills/x","recommendation":"honor the approved scope",
            "evidence":{"source_ref":"you: 'approve the broad option'","was_read":True,
                        "already_fixed_check":{"ran":True,"result":"live on main"},
                        "confidence":"high","confidence_basis":"verbatim quote diverges from the shipped narrow filter"}}
    bad = {"title":"vibes","friction_type":"intent_miss","evidence":"I feel you wanted more"}
    monkeypatch.setattr(harvest, "_run_intent_llm", lambda *a, **k: ([good, bad], None))
    out = harvest.intent_audit(str(j), use_llm=True)
    assert [f["title"] for f in out["qualified"]] == ["approved broad, shipped narrow"]
    assert len(out["dropped"]) == 1  # the vibes finding has no evidence record
```
- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** — `intent_audit`: `stripped = strip_session(path,"final")`, `hm = human_messages(path)`, `prompt = build_intent_prompt(stripped, hm)`; if not use_llm → `findings=[]`; else `findings, err = _run_intent_llm(prompt, model, max_budget_usd)` (on err return `{...,"error":err,"qualified":[],"dropped":[]}`); `qualified, dropped = qualify_findings(findings or [])`; return the dict. `_run_intent_llm` mirrors `_call_verify_llm` (subprocess `claude -p ... --model ... --max-budget-usd ...`, returns `(parse_findings(stdout), None)` or `(None, "<named reason>")`). Import `parse_findings, qualify_findings` from `orchestrator.agent_review`.
- [ ] **Step 4: Run → passes.**
- [ ] **Step 5: Commit** `feat(harvest): intent_audit — reconstruct intent, validate against the evidence schema`

---

### Task 3: `harvest intent-audit` CLI

**Files:** Modify `src/orchestrator/cli.py` (the `harvest` group is at ~line 1573, `strip` subcommand ~1616 to mirror); Test the repo's CLI test file (grep for CliRunner + harvest).

**Interfaces:** `harvest intent-audit <session_path> [--no-llm] [--model] [--max-budget-usd] [--json-output]` → calls `harvest.intent_audit`, prints `Qualified (N)` / `Dropped (M)` (mirror the --qualify-file output style in agent_review_cmd) or JSON.

- [ ] **Step 1: Write the failing test** (CliRunner; monkeypatch `harvest._run_intent_llm` to return one good + one bad finding; assert exit 0, "Qualified (1)", "Dropped (1)"). Also a `--json-output` test asserting the JSON has `qualified`/`dropped`.
- [ ] **Step 2: Run → fails.**
- [ ] **Step 3: Implement** — `@harvest.command("intent-audit")` with `@click.argument("session_path", type=click.Path(exists=True))`, `--no-llm` flag, `--model` (default sonnet), `--max-budget-usd` (default 2.0), `--json-output`. Call `harvest.intent_audit(session_path, use_llm=not no_llm, model=model, max_budget_usd=max_budget_usd)`. If `--json-output`: dump the dict. Else: if `error` → echo the warning; print `Qualified (N):` + `  ✓ <title>` and `Dropped (M):` + `  ✗ <title> — <_drop_reason>`.
- [ ] **Step 4: Run → passes.** Then `canopy version bump`.
- [ ] **Step 5: Commit** `feat(harvest): intent-audit CLI subcommand; version bump`

---

## Self-Review
- Spec coverage: intent reconstruction from Jonathan's words (T1 prompt embeds strip_session + human_messages); intent-miss classes (T1 rubric); schema-validated output (T2 qualify_findings drops the vibes finding); CLI (T3); ephemeral (no persistence anywhere). ✅
- Placeholder scan: none — real code/tests in every step.
- Type consistency: `build_intent_prompt(str, list[str])->str`, `intent_audit(path,*,use_llm,model,max_budget_usd)->dict{session,qualified,dropped,error}`, `_run_intent_llm(prompt,model,budget)->(list|None,str|None)` consistent across tasks.

## Downstream (ada, after this merges)
Update `skills/self-review/SKILL.md` §1: the intent axis uses `canopy harvest intent-audit <session>`; REMOVE the "no schema backing yet / cap intent low" caveat (it now flows through the same validator). Ships as an ada PR.
