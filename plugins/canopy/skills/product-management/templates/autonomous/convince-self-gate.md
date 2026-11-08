# Convince-self-it's-clean gate

Runs before each PR opens during a `/canopy:pm-autonomous` sprint. Goal: not just "tests pass" but "I'd defend this in a code review." Layered, in order. Any layer failing means the proposal is dropped (logged in the cycle log under `self-review-blocked`) and the loop moves on to re-derive the corresponding email highlight.

Don't skip layers. Don't merge them. The point of the gate is to make a different kind of mistake visible at each stage.

## 3a. Mechanical checks

Run all of these against the staged change. They are CHEAP — run them in parallel where possible.

1. `testing.unit` — must exit 0
2. `testing.lint` — must exit 0
3. `testing.types` — must exit 0
4. **Secret-leak scan (hardcoded patterns; not configurable):**

   ```bash
   git diff --staged | python3 "$PLUGIN_PATH/skills/product-management/scripts/secret_scan.py" --env-file .env
   ```

   Where `$PLUGIN_PATH` is resolved via `installed_plugins.json` as in `config-schema.md`. The `--env-file` flag is omitted if `.env` does not exist.

5. **Diff-size cap:**

   ```bash
   git diff --staged --stat | python3 "$PLUGIN_PATH/skills/product-management/scripts/diff_size_check.py" --limit "$DIFF_LIMIT"
   ```

   Where `$DIFF_LIMIT` is `guardrails.diff_size_limit_lines` from `autonomous.yaml`.

If any mechanical check fails, abandon this proposal — log it in the run log, do NOT try to "fix" by reducing scope. The cycle simply re-derives the email highlight.

## 3b. Self-review pass — five questions, written to the cycle log

Re-read the diff. Answer each question IN WRITING in the cycle log. The act of writing the answer is the gate — vague or evasive answers fail.

1. **What invariant did I just change?** Name a specific contract — input format, return semantics, side-effect ordering, persisted-state shape. Answer "none" or "I don't know" → FAIL.
2. **What's the riskiest line in this diff?** Quote a specific line. "Nothing is risky" on any non-trivial change → FAIL.
3. **What would a senior eng object to in code review?** Name a concrete objection, even if you disagree with it. "Nothing comes to mind" on a non-trivial change → FAIL (probable blind spot).
4. **Did I touch a test that codifies a behavior I'm changing?** If yes, did I update the test's *intent* or merely patch its assertions to match? "Patched the assertions" → FAIL.
5. **Would I be comfortable if this shipped while I was on vacation?** Hesitation → FAIL.

A failure on any question DROPS the proposal — write the question number and a one-sentence reason in the run log under `self-review-blocked`, then re-derive the corresponding email highlight (see Phase C in `cycle.md`).

## 3c. Dogfood pass — prod, not localhost

For any change that's named in a `Try it:` line of the target email, the dogfood is on **deployed prod**, not on a local stack. Localhost dogfood does not count and is not an acceptable substitute.

This is a hard rule: the local stack is often unreachable on the dev's machine (docker socket permissions, port owned by another tenant, no local Postgres binary), and the post-deploy capture is the real proof; a localhost step is ceremony in front of it.

### Why prod, not localhost

- **Localhost runs different code.** `DEBUG=True`, dev fixtures, in-memory backends, missing prod env vars, dev-only feature flags. A green localhost dogfood does not certify what users see.
- **Localhost screenshots cannot be used for the email** (per `email-format.md` Hard rule #3 — they leak port numbers and seeded fake data). So even when localhost dogfood succeeds, you have to repeat the click-through against prod to gather hero shots — the localhost work is wasted.
- **Localhost is not always reachable.** Worktree-first workflows, shared dev hosts, rotating credentials all break localhost reproducibly. Treating localhost as required forces the cycle into a "skip the gate" branch, which is worse than codifying the prod-only rule.
- **Post-deploy is where the cycle must verify anyway** (Section 3d health check + Phase E.1 prod screenshot capture). Folding dogfood into the same prod-driving harness eliminates a duplicate code path and removes the localhost-dogfood / prod-screenshot drift class of bugs.

### The procedure

The dogfood pass moves AFTER deploy, between 3d (health check) and Phase E (send). The sequence inside Phase C becomes:

1. 3a / 3b → open PR → CI green → merge
2. `shipping.deploy_command` → poll `shipping.deploy_workflow` to completion
3. **3d post-deploy health check** (Section 3d below)
4. **3c dogfood + prod screenshots** (this section, against the deployed change)
5. Phase E.4 render check + Phase E.5 send

Concretely, for each `Try it:` line in the target email:

1. Drive the configured `testing.dogfood.headless_browser_skill` against `<deployed-base-url>` (NOT `testing.dogfood.base_url` — that field's localhost URL is now unused; see deprecation note below).
2. Authenticate via the project's automation login (e.g. `/auth/e2e-login/` for ace-web, project-specific for others; the `headless_browser_skill` should know its own auth path or accept a token via env).
3. Click through the actual user path. Verify the expected behavior visibly happens.
4. Capture the hero screenshot for the highlight at the same time — the `Try it:` dogfood and the `email-format.md` Hard-rule-3 prod screenshot are the **same artifact**. Save into `$EMAIL_WORKDIR/screenshots/` (the Phase E temp working dir) so Phase E.2 can push them straight to the asset branch.
5. Record the gate verdict in the run log. If the click-through fails, this is a fix-forward (counts against `guardrails.max_fix_forward_attempts`), not a "skip dogfood".

### What the cycle is allowed to substitute

If the change is purely backend (no user-visible surface), it CAN skip 3c entirely — but then it CANNOT appear as a `Try it:` highlight; it lives only in the email's footer / internal `*` section. This is the same rule as before, unchanged.

There is **no** escape hatch for "prod is hard to reach today." Prod must be reachable for the deploy itself to have happened (3d already verified the health endpoint); driving it as `ace@<automation-account>` afterward is the same level of access. If you genuinely cannot reach the deployed app (broken auth, region outage, etc.), the cycle is in a stuck-state — Phase D's stuck-state branch applies.

### Deprecating `testing.dogfood.base_url` / `start_command` / `wait_for`

These fields existed for the old localhost-dogfood flow. They are not removed yet — existing configs validate as before — but new projects should leave them unset. A future canopy version will remove them entirely. The `headless_browser_skill` field stays; it now drives prod.

## 3d. Post-deploy health check

After deploy:

1. Poll each `shipping.post_deploy_health` URL with backoff (5s, 10s, 20s, 40s, 80s — total ~5 min) for 200 OK
2. If any URL fails or stays 5xx: do NOT auto-revert. The broken-prod state becomes the next scout finding; switch into a fix-forward investigation cycle (still autonomous)
3. If still red after `guardrails.max_fix_forward_attempts` cycles: log "stuck" in the run log, send a minimal "no email this sprint, here's why" note via `email.sender_skill`, stop.
