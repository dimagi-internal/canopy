---
name: walkthrough-defect-creator
description: |
  Generate walkthrough eval fixtures by injecting calibrated defects into a clean
  HTML page. Produces test pages targeting specific scoring dimensions, with
  documented defects and ground-truth expected scores.
  Use when asked to "create walkthrough fixtures", "generate eval fixtures",
  or "walkthrough-defect-creator <name>".
---

# Walkthrough Defect Creator

Take a clean source HTML page and produce fixture variants with known, documented
defects. Each variant targets a specific walkthrough scoring dimension so the
eval suite can measure scoring accuracy.

## Input

A clean page at `evals/walkthrough/source/<name>/index.html` in the current repo,
or at `evals/walkthrough/source/<name>/index.html` in the canopy runtime
(resolved via `scripts/canopy-runtime.sh`).

```bash
_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
SOURCE=""
for P in \
  "$_ROOT/evals/walkthrough/source/$1/index.html" \
  "$CANOPY_ROOT/evals/walkthrough/source/$1/index.html"; do
  [ -f "$P" ] && SOURCE="$P" && break
done
echo "${SOURCE:-NOT_FOUND}"
```

If NOT_FOUND, tell the user to place a clean HTML page at
`evals/walkthrough/source/<name>/index.html`.

## Output

For each defect category, write files to `evals/walkthrough/fixtures/<name>-<category>/`:

```
evals/walkthrough/fixtures/
├── <name>-clean/
│   ├── index.html          — unmodified copy of source
│   ├── spec.yaml           — walkthrough spec
│   └── ground-truth.json   — expected scores (all 4-5/5)
├── <name>-bad-content/
│   ├── index.html          — source with content defects
│   ├── spec.yaml
│   ├── ground-truth.json
│   └── defects.json        — audit trail of changes
├── <name>-bad-styling/
│   └── ...
├── <name>-bad-demo-readiness/
│   └── ...
└── <name>-mixed/
    └── ...
```

## Step 1: Read and understand the source page

Read the source HTML file. Understand:
- What product/company is this page about?
- What are the major page sections (hero, features, impact stats, CTA, etc.)?
- What specific claims, numbers, and names appear in the content?
- What CSS frameworks or patterns are used?

You need to understand the content to inject realistic, targeted defects.

## Step 2: Generate the walkthrough spec

Create a `spec.yaml` that works for ALL fixtures (they share the same structure,
just different page quality). The spec should have:

- One persona (e.g., "stakeholder" — a potential buyer/funder evaluating the product)
- One scene per major page section (typically 3-5 scenes)
- Each scene's `show` field describes what section to view
- Each scene's `ai_quality` field (where applicable) describes what to check
- `base_url` set to `http://localhost:{{PORT}}` — the eval skill fills this in

```yaml
name: "<name> Walkthrough Eval"
narrative: "Evaluating <name> marketing page for stakeholder readiness"
base_url: "http://localhost:{{PORT}}"

personas:
  stakeholder:
    name: "Dana Chen"
    role: "VP of Programs, potential customer"
    color: "#2563eb"
    intro: "Dana is evaluating this product for her organization."

scenes:
  - persona: stakeholder
    title: "Hero and first impression"
    show: "The top of the page — hero section with headline and CTA"
    impressive_because: "Clear value proposition, professional design, immediate credibility"
    ai_quality: "Hero headline should be specific to the product, not generic marketing copy"

  - persona: stakeholder
    title: "Features overview"
    show: "The features or capabilities section"
    impressive_because: "Each feature is concrete and specific to this product"
    ai_quality: "Feature descriptions should reference real product capabilities, not placeholder text"

  - persona: stakeholder
    title: "Impact and proof points"
    show: "The impact stats, testimonials, or social proof section"
    impressive_because: "Numbers are specific, verifiable, and impressive"
    ai_quality: "Impact numbers should be consistent with each other and the rest of the page"

  - persona: stakeholder
    title: "Call to action"
    show: "The final CTA section and footer"
    impressive_because: "Clear next step, professional finish"
```

Adjust the number of scenes based on the actual page sections. Write the same
`spec.yaml` to every fixture directory.

## Step 3: Create the clean fixture

1. Copy the source `index.html` unchanged to `evals/walkthrough/fixtures/<name>-clean/`
2. Copy the `spec.yaml`
3. Write `ground-truth.json`:

```json
{
  "expected_scores": {
    "scene_1": {
      "content": {"min": 4, "max": 5},
      "app_page": {"min": 4, "max": 5},
      "screenshot": {"min": 3, "max": 5},
      "slide": {"min": 3, "max": 5},
      "demo_readiness": {"min": 4, "max": 5}
    }
  },
  "expected_detections": [],
  "expected_routing": {
    "review": false,
    "design_review": false,
    "qa": false
  }
}
```

Include one entry per scene in `expected_scores`. For a clean page, all content/
app_page/demo_readiness dimensions should be 4-5. Screenshot and slide depend on
capture quality, so allow 3-5.

## Step 4: Create the bad-content fixture

Read the source page content carefully. Inject these types of defects:

**Content defects to inject (aim for 3-5 total):**

1. **Generic placeholder text:** Replace the hero headline or a key value
   proposition with generic marketing copy like "Your Solution For Success"
   or "A Platform That Delivers Results". The walkthrough should catch that
   this isn't specific to the product.

2. **Factual inconsistency:** Change an impact stat to contradict the body text.
   For example, if the page says "101,000+ services delivered" in one place,
   change it to "5,000+" in another. The walkthrough should catch the mismatch.

3. **Demo data artifact:** Add a visible "Unknown Organization" or "Test User"
   somewhere in the content. Or duplicate a testimonial/stat with identical text.

4. **Missing specificity:** Replace a concrete feature description with vague
   text like "Our innovative approach helps you achieve your goals."

**Implementation:** Use the Edit tool to make targeted replacements in the
copied `index.html`. Do NOT rewrite the whole file — make surgical edits.

**Write `defects.json`** documenting each change with: id, description,
dimension, severity, original text, replacement text.

**Write `ground-truth.json`** with:
- Content scores: min 1, max 2 for scenes with defects
- App page scores: unchanged (min 4, max 5)
- Demo readiness: min 1, max 3 (content problems hurt demo readiness too)
- Expected detections: one per injected defect, with `match_hint` containing
  a string from the defective text the walkthrough should quote
- Expected routing: `review: true` (content issues route to /review)

## Step 5: Create the bad-styling fixture

Inject CSS/layout defects:

**Styling defects to inject (aim for 3-4 total):**

1. **Kill visual hierarchy:** Add a `<style>` block that sets all headings to
   the same font-size (e.g., `h1, h2, h3, h4 { font-size: 16px !important; }`).

2. **Break spacing:** Add `* { margin: 0 !important; padding: 0 !important; }`
   to a key section, collapsing all whitespace.

3. **Clashing colors:** Override the primary color on a section with something
   that clashes (e.g., bright red text on bright green background).

4. **Break responsive:** Add `@media (max-width: 768px) { .features { display: none !important; } }`
   or similar to hide a key section on mobile.

**Write `ground-truth.json`** with:
- App page scores: min 1, max 2 for affected scenes
- Content scores: unchanged (min 4, max 5)
- Demo readiness: min 2, max 3
- Expected routing: `design_review: true`

## Step 6: Create the bad-demo-readiness fixture

Inject demo-killing defects:

**Demo readiness defects to inject (aim for 3-4 total):**

1. **Loading spinner:** Add a visible `<div class="spinner" style="position:fixed;top:50%;left:50%;z-index:9999;font-size:24px;">Loading...</div>`
   that covers content.

2. **Error overlay:** Add a `<div style="position:fixed;bottom:0;left:0;right:0;background:red;color:white;padding:16px;z-index:9999;">TypeError: Cannot read properties of undefined</div>`.

3. **Broken images:** Change an `<img>` src to a nonexistent path, or add
   `<img src="/missing-image.png" alt="Product screenshot" style="width:100%;height:300px;border:1px solid #ccc;">`.

4. **TODO markers:** Add visible `<!-- TODO: replace with real content -->`
   that renders, or add `[TODO: Add testimonial]` in visible text.

**Write `ground-truth.json`** with:
- Demo readiness scores: min 1, max 2
- Content scores: may be affected (min 2, max 4)
- Expected routing: `qa: true`

## Step 7: Create the mixed fixture

Combine 1-2 moderate defects from each category:

- One content issue (generic text, not as severe as bad-content)
- One styling issue (minor spacing break)
- One demo readiness issue (one broken image)

Scores should be in the 2-3 range across multiple dimensions. This tests
whether the walkthrough can handle multiple simultaneous problems and
prioritize correctly.

**Write `ground-truth.json`** with moderate expected scores (min 2, max 3)
across affected dimensions and routing to multiple specialists.

## Step 8: Report

After creating all fixtures, report:

```
Created 5 fixtures from source '<name>':
  <name>-clean           — baseline, expecting 4-5/5
  <name>-bad-content     — {n} content defects injected
  <name>-bad-styling     — {n} styling defects injected
  <name>-bad-demo-ready  — {n} demo-readiness defects injected
  <name>-mixed           — {n} mixed defects across dimensions

Total defects planted: {total}
Ground truth files: {count} scenes × 5 dimensions = {total} score expectations

Run /walkthrough-eval run to score these fixtures.
```
