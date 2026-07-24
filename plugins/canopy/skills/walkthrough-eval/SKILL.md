---
name: walkthrough-eval
description: |
  Run the walkthrough skill against eval fixtures with known defects, compare
  scores to ground truth, compute accuracy metrics, and track trends over time.
  Use when asked to "eval the walkthrough", "run walkthrough eval", or
  "walkthrough-eval run".
---

# Walkthrough Eval

Run the walkthrough skill against test fixtures with planted defects. Compare
the walkthrough's scores against human-calibrated ground truth to measure
scoring accuracy, defect detection, and routing correctness. Track metrics
over time to measure whether prompt changes improve the walkthrough.

## Modes

- `/walkthrough-eval run` — Run all fixtures, report metrics
- `/walkthrough-eval run <fixture>` — Run a single fixture
- `/walkthrough-eval history` — Show metric trends over time
- `/walkthrough-eval compare <r1> <r2>` — Side-by-side comparison of two runs
- `/walkthrough-eval consistency <fixture>` — Run same fixture 3x, measure variance

## Setup

### Locate fixtures

```bash
_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
EVAL_DIR=""
for P in \
  "$_ROOT/evals/walkthrough" \
  "$CANOPY_ROOT/evals/walkthrough"; do
  [ -d "$P/fixtures" ] && EVAL_DIR="$P" && break
done
echo "${EVAL_DIR:-NOT_FOUND}"
```

If NOT_FOUND, tell the user to run `/walkthrough-defect-creator <name>` first
to generate fixtures.

### List available fixtures

```bash
ls -d "$EVAL_DIR/fixtures"/*/ 2>/dev/null | xargs -I{} basename {}
```

### Determine run version

```bash
mkdir -p "$EVAL_DIR/eval-runs"
TODAY=$(date +%Y-%m-%d)
LATEST=$(ls -d "$EVAL_DIR/eval-runs/$TODAY-v"* 2>/dev/null | sort | tail -1)
if [ -z "$LATEST" ]; then
  RUN_ID="$TODAY-v001"
else
  NUM=$(echo "$LATEST" | grep -o 'v[0-9]*$' | tr -d 'v')
  NEXT=$(printf "v%03d" $((10#$NUM + 1)))
  RUN_ID="$TODAY-$NEXT"
fi
RUN_DIR="$EVAL_DIR/eval-runs/$RUN_ID"
mkdir -p "$RUN_DIR"
echo "Run: $RUN_ID → $RUN_DIR"
```

## Run Mode

For each fixture (or a single fixture if specified):

### Step 1: Serve the fixture

```bash
cd "$EVAL_DIR/fixtures/<fixture>"
python3 -m http.server 0 &
SERVER_PID=$!
sleep 1
```

Parse the port from the server output. The server prints
`Serving HTTP on :: port NNNNN`. Extract the port number.

### Step 2: Prepare the walkthrough spec

Read the fixture's `spec.yaml`. Replace `{{PORT}}` with the actual port.
Write the resolved spec to a temp file:

```bash
RESOLVED_SPEC="/tmp/walkthrough-eval-spec-<fixture>.yaml"
sed "s/{{PORT}}/$PORT/g" "$EVAL_DIR/fixtures/<fixture>/spec.yaml" > "$RESOLVED_SPEC"
```

Also create the required `docs/walkthroughs/` directory structure so the
walkthrough skill can find the spec:

```bash
mkdir -p /tmp/walkthrough-eval-project/docs/walkthroughs
cp "$RESOLVED_SPEC" /tmp/walkthrough-eval-project/docs/walkthroughs/<fixture>.yaml
mkdir -p /tmp/walkthrough-eval-project/screenshots/walkthroughs
```

### Step 3: Run the walkthrough skill

Invoke the walkthrough skill against this fixture. The key constraint: you
need the walkthrough to produce its standard JSON sidecar output so we can
extract scores.

Use the Skill tool to invoke `/walkthrough <fixture>`. Point it at the
resolved spec. The walkthrough will:
- Set up browse
- Execute all scenes against `localhost:<port>`
- Score on 5 dimensions
- Write JSON sidecar to `/tmp/walkthrough-run-data.json`

**IMPORTANT:** Do NOT run the walkthrough in improve mode. Run it in plain
`run` mode only — we want raw scores, not the improvement loop.

After the walkthrough completes, read the JSON sidecar:

```bash
cat /tmp/walkthrough-run-data.json
```

### Step 4: Kill the server

```bash
kill $SERVER_PID 2>/dev/null
```

### Step 5: Extract scores

Parse the JSON sidecar. For each scene slide, extract:
- The 5-dimension scores from `ai_evaluation.commentary`
- The overall score from `ai_evaluation.score`
- Any issues from the summary slide

The commentary format is:
```
Overall: 3/5 (weakest: Content). A: Content 3/5 — generic. B: App Page 4/5 — clean. C: Screenshot 4/5 — good. D: Slide 4/5 — clear. E: Demo Ready 3/5 — needs polish.
```

Parse each dimension score from this string.

### Step 6: Compare against ground truth

Read `$EVAL_DIR/fixtures/<fixture>/ground-truth.json`.

**Calibration:** For each scene × dimension, check if the walkthrough's score
falls within the expected `[min, max]` range.

```
Score: 2, Expected: [1, 2] → HIT
Score: 4, Expected: [1, 2] → MISS (walkthrough scored too high — missed the defect)
Score: 1, Expected: [4, 5] → MISS (walkthrough scored too low — false alarm)
```

Count hits and misses. Calibration accuracy = hits / total.

**Detection:** For each item in `expected_detections`, search the walkthrough's
commentary, issues list, and "worst thing found" quotes for the `match_hint`
string (case-insensitive substring match). If found, the defect was detected.

Detection rate = found / planted.

**Severity accuracy:** For each detected defect, check if the walkthrough
attributed it to the correct `dimension`. Look at which dimension scored
lowest for the relevant scene — if it matches the expected dimension, it's
correct.

Severity accuracy = correct / detected.

**Routing accuracy:** If `expected_routing` exists in ground truth, check the
walkthrough's action list (the "Fix:" lines in scene scoring output). Map
fix categories to specialist skills:
- `[CODE]` on content issues → `/review` (expected_routing.review)
- `[CODE]` on styling issues → `/design-review` (expected_routing.design_review)
- `[CODE]` or `[INFRA]` on readiness issues → `/qa` (expected_routing.qa)

If the walkthrough recommended the right specialist for the right dimension
failure, the routing is correct.

Routing accuracy = correct / total decisions.

### Step 7: Save fixture results

```bash
mkdir -p "$RUN_DIR/fixtures/<fixture>"
```

Save:
- `sidecar.json` — copy of `/tmp/walkthrough-run-data.json`
- `screenshots/` — copy of walkthrough screenshots

### Step 8: Aggregate and report (after all fixtures)

After running all fixtures, compute aggregate metrics:

| Metric | Weight |
|--------|--------|
| Calibration accuracy | 0.35 |
| Detection rate | 0.30 |
| Severity accuracy | 0.15 |
| Routing accuracy | 0.20 |

Composite = weighted average.

Write `results.json` to `$RUN_DIR/`:

```json
{
  "run_id": "<RUN_ID>",
  "timestamp": "<ISO timestamp>",
  "walkthrough_skill_sha": "<git SHA of SKILL.md>",
  "fixtures": {
    "<fixture-name>": {
      "calibration": {"hits": 5, "total": 5, "accuracy": 1.0},
      "detection": {"found": 0, "planted": 0, "rate": null},
      "severity": {"correct": 0, "total": 0, "accuracy": null},
      "routing": {"correct": 0, "total": 0, "accuracy": null},
      "raw_scores": {
        "scene_1": {"content": 4, "app_page": 5, "screenshot": 4, "slide": 4, "demo_readiness": 5}
      }
    }
  },
  "aggregate": {
    "calibration": 0.84,
    "detection": 0.85,
    "severity": 0.91,
    "routing": 0.83,
    "composite": 0.85
  }
}
```

For metrics with no applicable data (e.g., detection rate for the clean fixture
which has 0 planted defects), use `null` and exclude from aggregation.

### Step 9: Update history

Append to `$EVAL_DIR/eval-history.json`:

```json
{
  "run_id": "<RUN_ID>",
  "timestamp": "<ISO timestamp>",
  "walkthrough_skill_sha": "<SHA>",
  "composite": 0.85,
  "calibration": 0.84,
  "detection": 0.85,
  "severity": 0.91,
  "routing": 0.83,
  "fixture_count": 5
}
```

If the file doesn't exist, create it as `[]` first.

### Step 10: Print report

```
Walkthrough Eval — <RUN_ID>
═══════════════════════════════════════════════════════════

Fixture              Calibr.    Detect.    Severity   Routing
──────────────────────────────────────────────────────────────
<name>-clean          5/5        -          -          -
<name>-bad-content    4/5        3/3        3/3        1/1
<name>-bad-styling    3/5        2/3        2/2        1/1
<name>-bad-demo       5/5        2/2        2/2        1/1
<name>-mixed          4/5        4/5        3/4        2/3
──────────────────────────────────────────────────────────────
AGGREGATE             84%        85%        91%        83%

Composite: 0.85
```

If a previous run exists in eval-history.json, also show:
```
vs previous: +0.07 (was 0.78)
Trend: improving / stable / regressing
```

## History Mode

Read `$EVAL_DIR/eval-history.json` and display:

```
Walkthrough Eval History
════════════════════════════════════════════════════════════════

Run              Composite  Calibr.  Detect.  Severity  Routing
────────────────────────────────────────────────────────────────
2026-03-26-v001    0.78      80%      75%      85%      80%
2026-03-26-v002    0.82      84%      80%      88%      80%
2026-03-27-v001    0.85      84%      85%      91%      83%
────────────────────────────────────────────────────────────────

Trend: improving (+0.07 over 3 runs)
Best: 2026-03-27-v001 (0.85)
```

## Compare Mode

Load two runs' `results.json`, show per-fixture comparison:

```
Comparing <r1> vs <r2>
════════════════════════════════════════════════════════════

Fixture              <r1> Comp.  <r2> Comp.  Delta
─────────────────────────────────────────────────────
connect-clean         1.00       1.00        =
connect-bad-content   0.72       0.85       +0.13  ▲
connect-bad-styling   0.68       0.70       +0.02  ▲
connect-bad-demo      0.80       0.82       +0.02  ▲
connect-mixed         0.65       0.72       +0.07  ▲
─────────────────────────────────────────────────────
OVERALL               0.78       0.82       +0.04  ▲
```

## Consistency Mode

Run the specified fixture 3 times. For each dimension across all scenes,
compute the standard deviation of scores.

```
Consistency Check — <fixture> (3 runs)
═══════════════════════════════════════════

Dimension        Run 1  Run 2  Run 3  StdDev   Status
──────────────────────────────────────────────────────
Content           2      2      3     0.47     STABLE
App Page          4      4      4     0.00     STABLE
Screenshot        4      3      4     0.47     STABLE
Slide             3      4      3     0.47     STABLE
Demo Readiness    2      2      2     0.00     STABLE
──────────────────────────────────────────────────────

Overall: STABLE (all dimensions stddev ≤ 0.5)
```

If any dimension has stddev > 0.5, flag it as NOISY — the scoring prompt
needs tightening for that dimension.
