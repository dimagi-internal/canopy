# DDD ↔ Walkthrough Render Engine + Manifest — Implementation Plan

> **Status: shipped — one render engine + `walkthrough-run-data.json` manifest live. Historical record, not current-state.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the two-renderer split into one render engine that emits a single manifest (`walkthrough-run-data.json`), consumed identically by the standalone walkthrough skill and by ddd — fixing empty decks, `${…}` links, the missing audit URL, the `narrative` NameError, cwd coupling, and SKILL↔code drift.

**Architecture:** `record_video.py` becomes the one capture pass; it tracks per-scene visited URLs + mp4 offsets and writes a superset `walkthrough-run-data.json`. `generate_presentation.py` and `upload.py` read that manifest (deleting the buggy rebuild + spec-link derivation). Independent clarity fixes (shared `auth.py`, `_review_id_from_url` move, `_resolve_ddd_dir(repo_root=)`, SKILL calling `compute_auto_iterate`, `Gate`/`Finding` models) ride along.

**Tech Stack:** Python 3 (stdlib + pydantic), Playwright (recorder), pytest (`uv run pytest` from canopy repo root), YAML specs.

**Branch:** `ddd-render-engine-manifest` (already created; spec committed at `6216820`).

**Spec:** `docs/superpowers/specs/2026-06-14-ddd-walkthrough-render-engine-and-manifest-design.md`

---

## Conventions (read once)

- Run tests from canopy repo root: `uv run pytest <path> -v`.
- Tests live in `tests/ddd/` and `tests/walkthrough/`. Fakes are hand-rolled (`FakePage`, `FakeResponse`); HTTP/gate/upload are injected via `_post`/`_upload`/`_gate` kwargs; no network in tests.
- Commit after each task with the venv on PATH already (canopy `.venv`). Use `git -c user.name="Jonathan Jackson" -c user.email="jjackson@dimagi.com" commit`.
- Manifest filename stays `walkthrough-run-data.json` (superset; back-compat with `generate_presentation`, `walkthrough-eval`, `defect-creator`).

---

## File Structure

**Create:**
- `scripts/walkthrough/manifest.py` — builds the manifest dict from resolved scenes + report + snapshot dir + spec; one pure function `build_manifest(...)`.
- `scripts/ddd/auth.py` — `DEFAULT_API`, `TOKEN_FILE`, `resolve_base_url`, `resolve_token` (the de-duped helpers).
- `tests/walkthrough/test_manifest.py`, `tests/ddd/test_auth.py`.

**Modify:**
- `scripts/walkthrough/_lib/results.py` — `RunReport`/scene-timing struct: add per-scene `urls_visited`.
- `scripts/walkthrough/_lib/orchestrator.py` — collect `page.url` per action into the scene's visited list; thread resolved scene URL.
- `scripts/walkthrough/record_video.py` — `--manifest` arg; attach nav listener; write the manifest.
- `scripts/walkthrough/generate_presentation.py` — tolerate manifest superset (no behavior change if keys present).
- `scripts/ddd/upload.py` — read manifest for deck + links; delete `_build_deck_run_data` + `_external_links_from_spec`; import auth + `_review_id_from_url` from shared spots; assert-not-skip.
- `scripts/ddd/review.py` — import from `auth.py`; host `_review_id_from_url` + `_REVIEW_ID_RE`.
- `scripts/ddd/narrative.py` — import `_review_id_from_url` from `review` (fixes NameError).
- `scripts/ddd/runstate.py` — `_resolve_ddd_dir(repo_root=None)`, `load(run_id, ddd_dir=None)`, `save(state, ddd_dir=None)`.
- `scripts/ddd/escalation.py` — pass-through `ddd_dir`.
- `scripts/ddd/run_pipeline.py` — `assemble_run_state(..., manifest=None)` fills `scenes_run`/`scene_filter`.
- `scripts/narrative/models.py` — `Gate` enum + `Finding` model (where ReviewRequest/Finding already live).
- `scripts/ddd/findings_review.py` — use `Gate.PRODUCT_FINDINGS`; stop re-deriving severity (carry judge severity).
- `plugins/canopy/skills/ddd-run/SKILL.md`, `skills/walkthrough/SKILL.md`, `plugins/canopy/agents/ddd.md` — call `compute_auto_iterate`; manifest flow; glossary.

---

## Phase 1 — Render engine emits the manifest

### Task 1: Per-scene visited-URL collection in the report

**Files:**
- Modify: `scripts/walkthrough/_lib/results.py`
- Modify: `scripts/walkthrough/_lib/orchestrator.py:511-519` (the `record_scene_timing` call site)
- Test: `tests/walkthrough/test_scene_urls.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/walkthrough/test_scene_urls.py
from scripts.walkthrough._lib.results import RunReport

def test_report_records_scene_urls():
    r = RunReport()
    r.record_scene_timing(scene_index=2, title="t", start_seconds=1.0, duration_seconds=3.0)
    r.record_scene_urls(scene_index=2, urls=["https://x/a", "https://x/a", "https://x/b"])
    timing = r.scene_timing_for(2)
    assert timing["start_seconds"] == 1.0
    # deduped, order-preserved
    assert timing["urls_visited"] == ["https://x/a", "https://x/b"]
```

- [ ] **Step 2: Run it — expect FAIL** (`record_scene_urls`/`scene_timing_for` missing)

Run: `uv run pytest tests/walkthrough/test_scene_urls.py -v`

- [ ] **Step 3: Implement** — in `results.py`, find the scene-timing storage (a list/dict keyed by `scene_index` built by `record_scene_timing`). Add:

```python
def record_scene_urls(self, *, scene_index: int, urls: list[str]) -> None:
    """Attach the ordered, deduped URLs a scene's page visited."""
    deduped: list[str] = []
    for u in urls:
        if u and u not in deduped:
            deduped.append(u)
    entry = self._scene_timing_index.setdefault(scene_index, {})
    entry["urls_visited"] = deduped

def scene_timing_for(self, scene_index: int) -> dict:
    return self._scene_timing_index.get(scene_index, {})
```

(If scene timings are stored as a list, add a `self._scene_timing_index: dict[int, dict]` mirror updated in `record_scene_timing`; ensure `to_json()` includes `urls_visited`.)

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `git add scripts/walkthrough/_lib/results.py tests/walkthrough/test_scene_urls.py && git commit -m "feat(walkthrough): RunReport records per-scene visited URLs"`

### Task 2: Orchestrator collects page.url per action

**Files:**
- Modify: `scripts/walkthrough/_lib/orchestrator.py` (`run_scene`, ~396-522)
- Test: `tests/walkthrough/test_scene_urls.py` (extend)

- [ ] **Step 1: Write the failing test** — drive a fake page whose `url` changes across actions, assert the report captured both.

```python
def test_run_scene_collects_visited_urls(monkeypatch):
    from scripts.walkthrough._lib import orchestrator as orch
    # FakePage whose .url flips after the click action
    class FakePage:
        def __init__(self): self.url = "https://x/start"; self._clicked = False
        def wait_for_timeout(self, ms): pass
        def wait_for_load_state(self, *a, **k): pass
        def evaluate(self, *a, **k): return None
        def screenshot(self, **k): Path(k["path"]).write_bytes(b"\x89PNG")
    page = FakePage()
    rec = orch.Recorder(base_url="https://x", config=orch.RecorderConfig())
    # monkeypatch execute_action to flip the url (simulate a redirect)
    def fake_exec(p, action, **k):
        if action.get("kind") == "click": p.url = "https://x/after"
        from scripts.walkthrough._lib.results import ActionResult
        return ActionResult(ok=True, kind=action.get("kind"), target=action.get("target"))
    monkeypatch.setattr(orch, "execute_action", fake_exec)
    rec.run_scene(page, {"url": "https://x/start", "actions": [{"kind": "click", "target": "x"}], "title": "t"}, scene_index=1)
    assert rec.report.scene_timing_for(1)["urls_visited"] == ["https://x/start", "https://x/after"]
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — in `run_scene`, before the action loop init a list seeded with the current URL; after each `execute_action`, append `page.url`; after the loop (next to the existing `record_scene_timing` at ~513) call `record_scene_urls`:

```python
# near the top of run_scene, after navigation:
visited: list[str] = []
try:
    visited.append(page.url)
except Exception:
    pass
# ... inside the per-action loop, AFTER execute_action(...):
try:
    visited.append(page.url)
except Exception:
    pass
# ... where record_scene_timing is already called (idx is not None):
self.report.record_scene_urls(scene_index=int(idx), urls=visited)
```

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(walkthrough): orchestrator records the URLs each scene navigated to`

### Task 3: A robust nav listener (catch client-side redirects/SPA)

**Files:**
- Modify: `scripts/walkthrough/record_video.py` (page creation ~809; pass a nav sink into the recorder)
- Modify: `scripts/walkthrough/_lib/orchestrator.py` (accept an optional `nav_urls: list[str]` sink the recorder appends to per scene)
- Test: `tests/walkthrough/test_scene_urls.py` (extend with a `framenavigated`-style callback)

> Rationale (spec Risk): action-boundary `page.url` snapshots miss redirects that happen *between* actions (e.g. the audit→workflow redirect that fires after the completion click while the recorder is waiting). Subscribe to Playwright `page.on("framenavigated", …)` for the main frame and feed those URLs into the same per-scene `visited` list.

- [ ] **Step 1: Write the failing test** — register a fake `page.on` that fires the callback with an extra URL mid-scene; assert it lands in `urls_visited`.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — in `record_video.py` after `page = context.new_page()` (line ~809):

```python
_nav_sink: list[str] = []
def _on_nav(frame):
    try:
        if frame == page.main_frame:
            _nav_sink.append(frame.url)
    except Exception:
        pass
page.on("framenavigated", _on_nav)
```

Pass `_nav_sink` to `recorder.run(page, scenes, nav_sink=_nav_sink)`; in `run_scene`, drain+clear `_nav_sink` into `visited` alongside the action snapshots (the recorder owns the per-scene boundary, so clear the sink at scene start and fold its contents in at scene end).

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(walkthrough): capture client-side redirects via framenavigated into visited URLs`

### Task 4: `build_manifest()` + `record_video` writes it

**Files:**
- Create: `scripts/walkthrough/manifest.py`
- Create: `tests/walkthrough/test_manifest.py`
- Modify: `scripts/walkthrough/record_video.py` (add `--manifest`; call builder after `recorder.run`)

- [ ] **Step 1: Write the failing test**

```python
# tests/walkthrough/test_manifest.py
import base64, json
from pathlib import Path
from scripts.walkthrough.manifest import build_manifest
from scripts.walkthrough._lib.results import RunReport

def test_build_manifest_superset(tmp_path):
    snap = tmp_path / "snapshots"; snap.mkdir()
    (snap / "scene_1.png").write_bytes(b"\x89PNG-1")
    (snap / "scene_1_page_text.json").write_text("{}")
    report = RunReport()
    report.record_scene_timing(scene_index=1, title="Opens", start_seconds=0.0, duration_seconds=5.0)
    report.record_scene_urls(scene_index=1, urls=["https://labs/x", "https://labs/audit/4317/bulk"])
    spec = {"name": "PAR", "narrative": "n", "base_url": "https://labs",
            "personas": {"amani": {"name": "Amani", "color": "#111"}},
            "scenes": [{"title": "Opens", "persona": "amani", "narrative": "Amani opens",
                        "url": "https://labs/x"}]}
    m = build_manifest(spec=spec, report=report, snapshots_dir=snap,
                       scenes_run=[1], scene_filter=None,
                       substitution_vars={"wk4_url": "/labs/x"}, generated_at="2026-06-14")
    assert m["name"] == "PAR"
    assert m["substitution_vars"] == {"wk4_url": "/labs/x"}
    s = m["slides"][0]
    assert s["type"] == "scene"
    assert s["scene_index"] == 1 and s["scene_total"] == 1
    assert s["url"] == "https://labs/x"                       # resolved; generate_presentation reads slide["url"]
    assert "https://labs/audit/4317/bulk" in s["urls_visited"]  # the on-camera audit URL
    assert s["screenshot_path"] == "snapshots/scene_1.png"
    assert s["mp4_start_offset"] == 0.0
    assert s["ai_evaluation"] is None
    assert base64.b64decode(s["screenshot_b64"]) == b"\x89PNG-1"
```

- [ ] **Step 2: Run — expect FAIL** (module missing).
- [ ] **Step 3: Implement `scripts/walkthrough/manifest.py`**

```python
"""Build the canonical render manifest (walkthrough-run-data.json, superset).

ONE artifact the render engine emits and every consumer (deck, ddd upload/links,
run-state) reads. Superset of the legacy walkthrough-run-data.json shape so
generate_presentation + the eval fixtures keep working. Capture facts only —
ai_evaluation is an overlay merged later by a scoring pass.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any


def build_manifest(
    *,
    spec: dict,
    report: Any,
    snapshots_dir: Path,
    scenes_run: list[int],
    scene_filter: str | None,
    substitution_vars: dict[str, str],
    generated_at: str,
) -> dict:
    scenes = spec.get("scenes", []) or []
    personas = spec.get("personas", {}) or {}
    slides: list[dict] = []
    for idx, scene in enumerate(scenes, start=1):
        if idx not in scenes_run:
            continue
        png = snapshots_dir / f"scene_{idx}.png"
        timing = report.scene_timing_for(idx) if hasattr(report, "scene_timing_for") else {}
        b64 = base64.b64encode(png.read_bytes()).decode() if png.exists() else None
        page_text = snapshots_dir / f"scene_{idx}_page_text.json"
        slides.append({
            "type": "scene",
            "scene_index": idx,
            "scene_total": len(scenes),
            "title": scene.get("title", f"Scene {idx}"),
            "narration": scene.get("narrative") or scene.get("show") or "",
            "persona_key": scene.get("persona") or (next(iter(personas), "") if personas else ""),
            "url": scene.get("url") or "",                       # already ${…}-substituted upstream
            "urls_visited": timing.get("urls_visited", []),
            "screenshot_path": f"snapshots/{png.name}" if png.exists() else None,
            "page_text_path": f"snapshots/{page_text.name}" if page_text.exists() else None,
            "screenshot_b64": b64,
            "mp4_start_offset": timing.get("start_seconds"),
            "ok": timing.get("ok", True),
            "ai_evaluation": None,
        })
    return {
        "name": spec.get("name", ""),
        "narrative": spec.get("narrative", ""),
        "generated_at": generated_at,
        "base_url": (spec.get("base_url") or "").rstrip("/"),
        "scenes_run": list(scenes_run),
        "scene_filter": scene_filter,
        "substitution_vars": dict(substitution_vars or {}),
        "personas": {
            k: {"name": v.get("name", k), "role": v.get("role", ""),
                "color": v.get("color", "#4F46E5"), "intro": v.get("intro", "")}
            for k, v in personas.items()
        },
        "slides": slides,
    }
```

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Wire into `record_video.py`** — add `ap.add_argument("--manifest", help="path to write the render manifest (walkthrough-run-data.json)")`. After `total_seconds = recorder.run(...)` and the report write, add:

```python
if args.manifest:
    from scripts.walkthrough.manifest import build_manifest
    import datetime as _dt
    scenes_run = [s.get("scene_index", i) for i, s in enumerate(scenes, 1)]
    manifest = build_manifest(
        spec=spec, report=recorder.report,
        snapshots_dir=Path(args.snapshots) if args.snapshots else Path(args.manifest).parent,
        scenes_run=scenes_run,
        scene_filter=(spec.get("scene_filter") or None),
        substitution_vars=(setup_provenance or {}).get("variables", {}),
        generated_at=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
    Path(args.manifest).write_text(json.dumps(manifest, indent=2))
    print(f"Wrote manifest: {args.manifest}")
```

- [ ] **Step 6: Commit** — `feat(walkthrough): record_video emits the canonical render manifest`

---

## Phase 2 — Deck reads the manifest

### Task 5: `generate_presentation` tolerates the superset; delete the rebuild's reason to exist

**Files:**
- Modify: `scripts/walkthrough/generate_presentation.py` (only if a superset key trips it — verify `slide["url"]` + `ai_evaluation` optional access already use `.get`)
- Test: `tests/walkthrough/test_generate_presentation_manifest.py`

- [ ] **Step 1: Write the failing test** — feed the Task-4 manifest to `build_presentation_html`; assert one scene slide rendered with the screenshot and the resolved URL shown, no `${`.

```python
from scripts.walkthrough.generate_presentation import build_presentation_html
from scripts.walkthrough.manifest import build_manifest
# (reuse the manifest fixture) → html = build_presentation_html(m)
# assert "scene_1" frame present; assert "${" not in html; assert "https://labs/x" in html
```

- [ ] **Step 2: Run — expect PASS or FAIL.** If FAIL (a key accessed without `.get`), fix that access to `.get`. If PASS, the superset is already compatible — record that and move on.
- [ ] **Step 3: Commit** (only if changed) — `fix(walkthrough): generate_presentation tolerates manifest superset`

---

## Phase 3 — ddd consumers read the manifest

### Task 6: `upload.py` deck step reads the manifest; delete `_build_deck_run_data`; assert-not-skip

**Files:**
- Modify: `scripts/ddd/upload.py` (deck step ~1139-1164; delete `_build_deck_run_data` ~860-920)
- Test: `tests/ddd/test_upload_deck.py`

- [ ] **Step 1: Write the failing test** — run dir containing `walkthrough-run-data.json` (manifest) + `snapshots/scene_1.png`; capture `_upload` calls; assert a `role="deck"` upload happened with one slide; assert a manifest with `slides=[]` RAISES (not silent).

```python
def test_deck_uploads_from_manifest(tmp_path, monkeypatch):
    # write run_dir/walkthrough-run-data.json with one scene slide (b64)
    # stub _resolve_ddd_dir to tmp; run upload_run with _upload/_gate/_narrative_check injected
    # assert any(call.kwargs["role"] == "deck" for call in uploads)

def test_deck_empty_manifest_raises(tmp_path):
    # manifest with slides: [] → upload_run raises / records a red line, does not silently skip
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — replace the deck try/except block (1139-1164). Read the manifest; if present use it; else (no manifest) it's an ERROR for a release upload, not a silent skip:

```python
sidecar_path = run_dir / "walkthrough-run-data.json"
if not sidecar_path.exists():
    raise DeckMissingError(
        f"no render manifest at {sidecar_path}; render must emit walkthrough-run-data.json "
        f"(record_video --manifest). Re-render before upload."
    )
deck_run_data = json.loads(sidecar_path.read_text())
slides = [s for s in deck_run_data.get("slides", []) if s.get("type") == "scene"]
if not slides:
    raise DeckMissingError(f"manifest {sidecar_path} has zero scene slides")
upload_fn(build_presentation_html(deck_run_data), kind="html",
          title=f"{spec.name} — walkthrough slides", base_url=base_url, token=token,
          run_id=run_id, narrative_slug=run_state.narrative_slug, role="deck",
          narrative_review_id=narrative_review_id)
```

Add `class DeckMissingError(RuntimeError): ...` near `NarrativeMissingError`. Delete `_build_deck_run_data` and its import of `base64`/`datetime` if now unused.

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(ddd): upload deck reads the render manifest; missing/empty deck is loud`

### Task 7: External-systems links from the manifest (fixes `${…}` + audit URL)

**Files:**
- Modify: `scripts/ddd/upload.py` (`_external_links_from_spec` → `_external_links_from_manifest`; call site ~1086-1088)
- Test: `tests/ddd/test_upload_links.py`

- [ ] **Step 1: Write the failing test**

```python
def test_links_from_manifest_resolved_and_include_visited():
    manifest = {"base_url": "https://labs", "slides": [
        {"type": "scene", "title": "Amani opens", "url": "https://labs/labs/workflow/3149/run/?run_id=4318",
         "urls_visited": ["https://labs/labs/workflow/3149/run/?run_id=4318",
                          "https://labs/audit/4317/bulk/?opportunity_id=10000"]}]}
    links = _external_links_from_manifest(manifest)
    urls = [l["url"] for l in links]
    assert all("${" not in u for u in urls)                       # no template leakage
    assert "https://labs/audit/4317/bulk/?opportunity_id=10000" in urls  # the created audit
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — new function; dedupe in scene order over each slide's `url` + `urls_visited`; label by scene title. Replace the `external_links = _external_links_from_spec(spec)` call with `external_links = _external_links_from_manifest(deck_run_data)` (the manifest already read in Task 6 — hoist its read above the links step). Delete `_external_links_from_spec`.

```python
def _external_links_from_manifest(manifest: dict) -> list[dict]:
    base = (manifest.get("base_url") or "").rstrip("/")
    out: list[dict] = []
    seen: set[str] = set()
    if base and base not in seen:
        out.append({"label": "App", "url": base, "kind": "reference"}); seen.add(base)
    for s in manifest.get("slides", []):
        if s.get("type") != "scene":
            continue
        label = s.get("title") or "Scene"
        for u in [s.get("url"), *s.get("urls_visited", [])]:
            if u and "${" not in u and u not in seen:
                out.append({"label": label, "url": u, "kind": "reference"}); seen.add(u)
    return out
```

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `fix(ddd): external-systems links from manifest (resolved URLs + created audit URL)`

### Task 8: `assemble_run_state` fills `scenes_run`/`scene_filter` from the manifest

**Files:**
- Modify: `scripts/ddd/run_pipeline.py:42-85`
- Test: `tests/ddd/test_run_pipeline.py` (extend)

- [ ] **Step 1: Write the failing test** — pass `manifest={"scenes_run":[1,2,3],"scene_filter":None}`; assert `state.scenes_run == [1,2,3]`.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — add `manifest: dict | None = None` param; after setting verdicts/findings/phase:

```python
if manifest is not None:
    state.scenes_run = manifest.get("scenes_run")
    state.scene_filter = manifest.get("scene_filter")
```

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(ddd): assemble_run_state derives scenes_run/scene_filter from the manifest`

---

## Phase 4 — Walkthrough skill migration (markdown)

### Task 9: Rewrite `skills/walkthrough/SKILL.md` render→score→deck flow

**Files:**
- Modify: `skills/walkthrough/SKILL.md`

- [ ] **Step 1:** Replace the "build `/tmp/walkthrough-run-data.json` by hand → generate deck → (later) record video" flow with: (a) one `record_video.py … --manifest /tmp/walkthrough-run-data.json` capture pass; (b) per-scene `visual-judge` scoring that **merges `ai_evaluation` into the manifest's matching slide** (read manifest, set `slides[i]["ai_evaluation"]`, write back); (c) `generate_presentation.py --input /tmp/walkthrough-run-data.json`. Remove the after-scoring separate video pass.
- [ ] **Step 2:** Add a one-line note that `walkthrough-eval`/`defect-creator` consume the same manifest (superset, unchanged keys).
- [ ] **Step 3: Commit** — `docs(walkthrough): skill renders once → scores into the manifest → decks from it`

---

## Phase 5 — Shared auth + NameError

### Task 10: Extract `scripts/ddd/auth.py`

**Files:**
- Create: `scripts/ddd/auth.py`; Create: `tests/ddd/test_auth.py`
- Modify: `scripts/ddd/review.py` (import from auth), `scripts/ddd/upload.py` (import from auth)

- [ ] **Step 1: Write the failing test** — `from scripts.ddd.auth import resolve_base_url, resolve_token, DEFAULT_API, TOKEN_FILE`; assert env precedence (`CANOPY_WEB_API_URL`, `CANOPY_WEB_PAT`) and TOKEN_FILE fallback (monkeypatch a temp token file).
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement `auth.py`** — move the byte-identical `_resolve_base_url`/`_resolve_token`/`DEFAULT_API`/`TOKEN_FILE` bodies verbatim, renamed public (`resolve_base_url`/`resolve_token`). In `review.py` and `upload.py` replace the local defs with `from scripts.ddd.auth import resolve_base_url as _resolve_base_url, resolve_token as _resolve_token, DEFAULT_API, TOKEN_FILE` (alias to keep call sites unchanged).
- [ ] **Step 4: Run — expect PASS** (`uv run pytest tests/ddd/test_auth.py tests/ddd/test_review.py -v`).
- [ ] **Step 5: Commit** — `refactor(ddd): shared auth module (dedupe review.py/upload.py)`

### Task 11: Fix the `narrative status` NameError

**Files:**
- Modify: `scripts/ddd/review.py` (host `_REVIEW_ID_RE` + `_review_id_from_url`), `scripts/ddd/upload.py` (import it), `scripts/ddd/narrative.py` (import it)
- Test: `tests/ddd/test_narrative_status.py`

- [ ] **Step 1: Write the failing test**

```python
def test_review_id_from_url_shared():
    from scripts.ddd.review import _review_id_from_url
    u = "https://x/review/3cc7f6f1-f4d7-4fcc-b136-6d44fee3c287"
    assert _review_id_from_url(u) == "3cc7f6f1-f4d7-4fcc-b136-6d44fee3c287"
    assert _review_id_from_url(None) is None
```

- [ ] **Step 2: Run — expect FAIL** (not in review.py yet).
- [ ] **Step 3: Implement** — move `_REVIEW_ID_RE` + `_review_id_from_url` from `upload.py` into `review.py`; in `upload.py` add `from scripts.ddd.review import _review_id_from_url`; in `narrative.py` add the same import (fixes line 1383). 
- [ ] **Step 4: Run** — `uv run pytest tests/ddd/test_narrative_status.py tests/ddd/test_upload*.py -v`; also smoke: `uv run python -m scripts.ddd.narrative status nonexistent-run 2>&1 | grep -vi nameerror`.
- [ ] **Step 5: Commit** — `fix(ddd): share _review_id_from_url — narrative status no longer NameErrors`

---

## Phase 6 — cwd decoupling

### Task 12: `_resolve_ddd_dir(repo_root=None)` + `ddd_dir` overrides

**Files:**
- Modify: `scripts/ddd/runstate.py` (`_resolve_ddd_dir`, `load`, `save`, `new_run`), `scripts/ddd/escalation.py`
- Test: `tests/ddd/test_runstate.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
def test_resolve_ddd_dir_explicit_repo_root(tmp_path):
    from scripts.ddd.runstate import _resolve_ddd_dir
    got = _resolve_ddd_dir(repo_root=tmp_path)
    assert got == tmp_path / ".canopy" / "ddd"

def test_load_save_honor_ddd_dir(tmp_path):
    from scripts.ddd.runstate import save, load
    from scripts.ddd.schemas.models import RunState
    ddd = tmp_path / ".canopy" / "ddd"
    save(RunState(run_id="r-1", narrative_slug="r"), ddd_dir=ddd)
    assert load("r-1", ddd_dir=ddd).run_id == "r-1"
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — `_resolve_ddd_dir(repo_root: Path | None = None)`: if `repo_root` given, `return (repo_root/".canopy"/"ddd")` (mkdir); else honor `DDD_DIR` env; else the existing git logic. Add `ddd_dir: Path | None = None` to `load`/`save`/`new_run`; when given, use it directly instead of `_resolve_ddd_dir()`. Update `escalation._state_file(ddd_dir=None)` similarly. Update the `runstate` module docstring to document the resolution order (explicit `ddd_dir` > `repo_root` > `DDD_DIR` env > git cwd).
- [ ] **Step 4: Run — expect PASS** (`uv run pytest tests/ddd/test_runstate.py -v`).
- [ ] **Step 5: Commit** — `feat(ddd): decouple _resolve_ddd_dir from cwd (repo_root/ddd_dir/DDD_DIR overrides)`

---

## Phase 7 — SKILL ↔ code single source

### Task 13: `ddd-run` SKILL calls `compute_auto_iterate` instead of inlining it

**Files:**
- Modify: `plugins/canopy/skills/ddd-run/SKILL.md` (Step 4 + Step 5)
- Verify: `scripts/ddd/run_pipeline.py:142` `compute_auto_iterate` signature matches what the SKILL needs.

- [ ] **Step 1:** Confirm `compute_auto_iterate(state, concept_verdict, user_verdict, findings) -> (action, reason)` (it already exists). If its signature differs from the SKILL's inline logic inputs, reconcile in `run_pipeline.py` with a unit test first (`tests/ddd/test_run_pipeline.py`): cover done / concept_change / unclear / stalled / continue and assert `(action, reason)`.
- [ ] **Step 2:** Replace SKILL Step 5's inline Python block with: call `assemble_run_state(state, …, manifest=<loaded manifest>)` (Task 8 fills scenes_run/scene_filter — drop the hand-stamp in Step 4), then `action, reason = compute_auto_iterate(state, concept_v, user_v, findings); state.auto_iterate_next_action, state.auto_iterate_reason = action, reason; save(state)`. Keep the tail-message table; delete the duplicated `HARD_CAP`/`stalled` logic.
- [ ] **Step 3: Commit** — `docs(ddd): ddd-run SKILL calls compute_auto_iterate (single source of truth)`

---

## Phase 8 — Vocab / shape

### Task 14: `Gate` enum

**Files:**
- Modify: `scripts/narrative/models.py` (add `Gate`), `scripts/ddd/findings_review.py`, `scripts/ddd/upload.py`, `scripts/ddd/narrative.py`
- Test: `tests/ddd/test_gate_enum.py`

- [ ] **Step 1: Write the failing test** — `from scripts.narrative.models import Gate; assert Gate.PRODUCT_FINDINGS == "product_findings"` etc. (use `str, Enum`).
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — `class Gate(str, Enum): CONCEPT_CHANGE="concept_change"; PRODUCT_FINDINGS="product_findings"; EXTERNAL_RELEASE="external_release"`. Replace `findings_review.GATE = "product_findings"` with `GATE = Gate.PRODUCT_FINDINGS`; the `"external_release"` literal in upload.py and `"concept_change"` in narrative.py with the enum.
- [ ] **Step 4: Run — expect PASS** (`uv run pytest tests/ddd -v`).
- [ ] **Step 5: Commit** — `refactor(ddd): Gate enum replaces scattered gate string literals`

### Task 15: shared `Finding` model + single `derive_severity`

**Files:**
- Modify: `scripts/narrative/models.py` (add `Finding`), `scripts/ddd/findings_review.py` (use it; stop re-deriving severity)
- Test: `tests/ddd/test_findings_review.py` (extend)

- [ ] **Step 1: Write the failing test** — a `Finding` carrying `severity` from the judge flows through clustering with severity preserved (not recomputed); assert a `redesign`/low-score finding keeps the judge's severity.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — `class Finding(BaseModel): scene: str; dimension: str; route: str; fix_kind: str; severity: str; detail: str; fix_recommendation: str = ""`. In `findings_review.py`, accept `Finding`s; keep `derive_severity` ONLY as the fallback when a finding lacks `severity` (judge-set severity wins). Document that the judge is the severity source.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `refactor(ddd): shared Finding model; severity set by judge, not re-derived`

### Task 16: Phase-milestone doc + run/iteration glossary

**Files:**
- Modify: `scripts/ddd/schemas/models.py` (RunState docstring), `plugins/canopy/agents/ddd.md`, `plugins/canopy/skills/ddd-run/SKILL.md`

- [ ] **Step 1:** In the `RunState` docstring, note which `phase` values are code-set (`judged` by `assemble_run_state`, `uploaded` by `upload_run`) vs orchestrator-only milestones (`phase0/spec/render/converged`), and that `promoted` is a legacy read-alias.
- [ ] **Step 2:** Add a 3-line glossary (run = top-level flow w/ run_id; iteration = loop increment w/ artifacts iterN_*; gate = a human pause) to `agents/ddd.md` + the SKILL headers.
- [ ] **Step 3: Commit** — `docs(ddd): document phase milestones + run/iteration glossary`

### Task 17: ⑤ stub — client `resolve()` + follow-up note

**Files:**
- Modify: `scripts/ddd/review.py` (add a `resolve_review` stub that raises `NotImplementedError` with the API-needed message), `docs/superpowers/specs/...` (append a "Follow-up: canopy-web review-resolve API" note).

- [ ] **Step 1:** Add `def resolve_review(review_id, decision, *, base_url=None, token=None): raise NotImplementedError("canopy-web exposes /api/reviews/<id>/ as GET/DELETE only; resolution needs a server-side PATCH/resolve endpoint — see follow-up.")`. This documents the gap at the call site.
- [ ] **Step 2: Commit** — `docs(ddd): stub resolve_review + note the canopy-web API follow-up`

---

## Phase 9 — PROVING GROUND: fresh PAR render end-to-end (user directive)

### Task 18: Render PAR through the new engine and verify the whole package

**Files:** none (validation); fixes routed back into the relevant task's file if found.

- [ ] **Step 1:** Run a fresh PAR render via the engine WITH the new `--manifest`, from the labs worktree, labs venv on PATH (the documented invocation), writing `--manifest <run_dir>/walkthrough-run-data.json`.
- [ ] **Step 2:** Assert the manifest: every `scenes_run` slide has a `screenshot_b64`, a resolved `url` (no `${`), and scene-2/3/4's `urls_visited` includes the live `/audit/<id>/bulk` URL.
- [ ] **Step 3:** Run the judges + `assemble_run_state(manifest=…)`; assert `scenes_run` populated from the manifest.
- [ ] **Step 4:** Upload (auto-approved gate) and verify the package: **Walkthrough slides section is non-empty** (deck has one slide per scene), **External Systems links are all resolved** (no `${`), and **the audit URL is present**.
- [ ] **Step 5:** For any defect surfaced, fix it in the owning task's file, re-run that task's tests, re-render, and re-verify. Loop until the PAR package is clean.
- [ ] **Step 6: Commit** any fixes — `fix(ddd): <defect> found via PAR proving-ground render`

---

## Final: code review + finish

- [ ] Dispatch a final code-quality reviewer over the whole branch diff.
- [ ] Use `superpowers:finishing-a-development-branch` to land it (PR to canopy `main`).
