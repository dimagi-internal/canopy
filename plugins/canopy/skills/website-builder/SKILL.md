---
name: website-builder
description: |
  Evaluation framework for the website builder agent. Scores generated websites
  on 6 dimensions (visual quality, brand consistency, content accuracy,
  responsiveness, code quality, depth sharpness), tracks scores over time, and
  enables A/B comparison between runs. The depth sharpness dimension uses an
  LLM-judge as a skeptical foundation officer — it surfaces the single best
  "smart nugget" per depth page so the user can see what each run actually
  knows, not just how it looks. Use when asked to "eval", "score", or
  "compare" website builder output.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - AskUserQuestion
---

# Website Builder Evaluation

## Eval Workflow

When the agent invokes `eval <product>`:

### Step 1: Setup

Determine the product name from the argument. Set EVAL_DIR to `evals/<product>` and CONTEXT_DIR to `evals/<product>/context`.

1. Verify the context directory exists and has files.
2. Determine run ID: check for existing runs today in the runs directory, increment version number. Format: `YYYY-MM-DD-vNNN` (e.g., `2026-03-25-v001`).
3. Create run directory.

### Step 2: Generate

1. Create a temporary working directory with the eval context:
   - Copy context dir to temp dir
   - Create output and screenshots subdirs

2. Run the generation pipeline by reading context files and invoking /frontend-design. Follow the same pipeline as the agent's generate command, but skip user review (Stage 5) — eval runs are fully automated.

3. Copy outputs to the run directory (output/ and screenshots/ subdirs).

4. Save the exact input prompt to input-prompt.md in the run directory.

5. Save the design system to design-system.md in the run directory.

### Step 3: Score

Score each dimension 1-10 by analyzing the generated output.

**visual_quality (weight: 0.20)**
Score mechanically by checking these criteria in the generated HTML/CSS source.
Award 1 point for each (max 10):

1. **Font pairing**: Uses at least 2 distinct font families (display + body)
2. **Font size contrast**: Largest heading is >= 3x body font size
3. **Color palette coherence**: All colors used are from a defined set in CSS variables
4. **Whitespace**: Sections have >= 80px vertical padding
5. **Visual hierarchy**: Hero section has larger text than all other sections
6. **Animation/motion**: At least one CSS animation or transition present
7. **Layout variety**: Not all sections use the same layout pattern (e.g., mix of grid, centered, cards)
8. **Dark/light contrast section**: At least one section uses inverted colors (dark bg + light text)
9. **Hover/interaction states**: At least 2 interactive elements have hover styles
10. **No visual clutter**: No more than 3 CTAs visible per viewport height

Count how many criteria pass. That's the score.
Write which criteria passed and which failed.

**brand_consistency (weight: 0.15)**
Read the generated HTML/CSS source and the brand guidelines from context. Check mechanically:
- Font families match brand guidelines
- Color hex values match brand palette
- Overall tone matches brand voice guidelines
Count deviations. 0 deviations = 10, 1 = 9, 2 = 8, etc. Floor at 1.
Write which specific deviations were found.

**content_accuracy (weight: 0.20)**
Read the generated HTML text content and the product brief from context. Extract key terms from the product brief (listed under "Key Terms" if present, otherwise extract product name, feature names, value props). Count how many appear in the generated output. Score: (terms_found / total_terms) * 10, rounded. Write which terms were found and which were missing.

**responsiveness (weight: 0.10)**
Score mechanically by checking these criteria in the HTML/CSS source.
Award 1 point for each that passes (max 10, scale proportionally):

1. **Viewport meta tag** present
2. **At least 1 media query** for mobile (max-width: ~768px)
3. **At least 1 media query** for tablet or intermediate size
4. **Fluid typography** (uses clamp(), vw units, or calc() for font sizes)
5. **Flexible layout** (uses flexbox or grid, not fixed pixel widths for containers)
6. **Images are responsive** (max-width: 100% or object-fit, no fixed pixel widths)
7. **Touch targets** (buttons/links have min 44px height on mobile)
8. **No horizontal overflow** (no elements wider than viewport — check for overflow-x: hidden on body as a proxy)
9. **Stack on mobile** (grid/flex items wrap or stack at small viewports)
10. **Mobile navigation** adapts (nav changes layout or collapses at small widths)

Count passing criteria, scale to 10. Write which passed/failed.

**code_quality (weight: 0.05)**
Read the generated HTML source. Check:
- Valid HTML structure (doctype, head, body)
- All images have alt text
- No broken resource references
- Viewport meta tag present
- Semantic HTML elements used (header, main, section, footer)
Count issues. 0 issues = 10, 1 = 9, 2 = 8, etc. Floor at 1.

**depth_sharpness (weight: 0.30)**

This dimension is the difference between a pretty site and a smart one. It
asks: when a sophisticated funder clicks two or three pages deep, do they
encounter content that makes them think this team is sharper than they
expected — or generic marketing copy?

The other dimensions are mechanical. This one requires judgment. Run it as an
LLM-judge pass.

**Step A — Pick the depth pages.** From the generated site, identify every
page that is at least one click away from the homepage (LDVP step pages,
program detail pages, /insights index page, etc.). These are "depth pages."
Skip the homepage itself.

**Step B — Run the judge prompt against each depth page.** Use this prompt
verbatim, substituting the page's HTML text content:

> You are a skeptical program officer at a major foundation (Gates, GiveWell,
> CIFF, Founders Pledge). You are evaluating a $10M+ grant. You have read
> hundreds of pitches and seen every "data-driven, evidence-based, scalable"
> marketing site. You are looking for reasons to say no, and you reward
> teams that show their work, name what didn't work, and scope claims
> precisely.
>
> Read the following page. Then answer:
>
> 1. What is the single most memorable claim or sentence on this page?
>    Quote it verbatim. (If nothing memorable, say "Nothing.")
> 2. What evidence tier supports it? (hard data with named source /
>    triangulated qualitative / hypothesis or lived experience / unsupported)
> 3. Is the scope of every numeric claim accurate? (program-specific stats
>    presented as program-specific, platform claims drawn from cross-program
>    evidence) — yes / partially / no
> 4. Does the page name at least one thing that didn't work, one open
>    question, or one limitation? — yes / no
> 5. Would you come away from this page thinking the team is sharper than
>    you expected? — yes / mixed / no
>
> Then score 1–10 where:
> - 1–3: Generic marketing copy. Could appear on any competitor site.
> - 4–6: Some real claims, but mostly polish. Scope errors or unsupported
>   numbers are present. The team is competent but not distinctive.
> - 7–8: Substantive content. Claims are scoped accurately. At least one
>   genuinely surprising or counterintuitive finding. The team shows their
>   work.
> - 9–10: Foundation-officer ready. Specific, scoped, benchmarked claims;
>   honest about limitations; surfaces a non-obvious insight that changes
>   how the reader thinks about the problem.

**Step C — Aggregate.** The dimension score is the median of the per-page
judge scores (median, not mean — one excellent page should not rescue a site
of generic ones, and one weak page should not sink a strong site).

**Step D — Surface the nuggets.** For every depth page, save the verbatim
"most memorable claim" the judge surfaced, plus its evidence tier and the
1–10 score. This list is the most valuable artifact the eval produces.
Persist it to the run directory as `nuggets.json`:

```json
{
  "run_id": "...",
  "pages": [
    {
      "url": "/programs/chc",
      "nugget": "We paid FLWs bonuses to defeat our fraud detection. They couldn't — 97.5% still flagged.",
      "evidence_tier": "hard data",
      "scope_accurate": "yes",
      "names_a_limitation": "yes",
      "score": 9,
      "judge_notes": "Adversarial test framing. Specific number. Scoped to CHC."
    },
    {
      "url": "/deliver",
      "nugget": "Nothing.",
      "evidence_tier": "unsupported",
      "scope_accurate": "no",
      "names_a_limitation": "no",
      "score": 3,
      "judge_notes": "Page is generic LDVP copy. No surfaced insight."
    }
  ]
}
```

This artifact is what the user reviews to know whether the run delivered.
Pretty pages with generic content score in the 3–5 band. Pages that surface
specific scoped insights with named evidence score 7+.

**Composite score:**
overall = (visual * 0.20) + (brand * 0.15) + (content * 0.20) + (responsive * 0.10) + (code * 0.05) + (depth * 0.30)

### Step 4: Save Scores

Write scores.json to the run directory with this format:

```json
{
  "run_id": "RUN_ID",
  "timestamp": "ISO_TIMESTAMP",
  "agent_version": "VERSION_FROM_PLUGIN_JSON",
  "dimensions": {
    "visual_quality": { "score": N, "notes": "..." },
    "brand_consistency": { "score": N, "notes": "..." },
    "content_accuracy": { "score": N, "notes": "...", "terms_found": N, "terms_total": N },
    "responsiveness": { "score": N, "notes": "..." },
    "code_quality": { "score": N, "notes": "...", "issues": N },
    "depth_sharpness": { "score": N, "notes": "...", "pages_judged": N, "median_page_score": N, "nuggets_path": "nuggets.json" }
  },
  "overall": N.NN,
  "context_hash": "MD5_OF_CONTEXT_DIR",
  "vs_baseline": "+/-N.NN or null if no baseline"
}
```

### Step 5: Compare Against Baseline

Read baseline.json from the eval directory. If it has a non-null run_id, calculate delta for each dimension and overall. Report improvements and regressions. If no baseline set, note: "No baseline yet. Run with --update-baseline to set one."

### Step 6: Update History

Read eval-history.json. Append this run's scores to the runs array with: run_id, timestamp, overall, and each dimension score. Write the updated file back.

### Step 7: Report

Print a formatted report showing all dimensions with scores, baseline comparisons, and the overall score. Include the run save path and instructions for setting baseline.

Use a box-drawing format like:
```
EVAL: <product> — Run <run_id>
Dimension          Score    Baseline    Delta
Visual Quality      7/10     —           —
Brand Consistency   8/10     —           —
Content Accuracy    9/10     —           —
Responsiveness      6/10     —           —
Code Quality        8/10     —           —
Depth Sharpness     6/10     —           —
OVERALL             7.20     —           —

Top nuggets surfaced this run:
  /programs/chc  (9)  "We paid FLWs bonuses to defeat fraud detection. They couldn't — 97.5% still flagged."
  /verify        (8)  "[verbatim quote from page]"
  /deliver       (3)  Nothing memorable.

(Full nugget list: <run_dir>/nuggets.json)
```

## --update-baseline

Read the most recent run from the runs directory. Copy its scores to baseline.json. Confirm: "Baseline updated to run {RUN_ID} (overall: {SCORE})."

## --history

Read eval-history.json. Print a table of all runs with scores. If more than 5 runs exist, also print a trend summary.

## --compare <run1> <run2>

Read scores.json from both runs. Print side-by-side comparison with deltas for each dimension and overall.
