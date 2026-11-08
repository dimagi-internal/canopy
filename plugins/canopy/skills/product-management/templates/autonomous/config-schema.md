# autonomous.yaml — Schema and Example

This file lives at `<repo>/.canopy/pm/autonomous.yaml` for any project that adopts the autonomous mode of `canopy:product-management`. PM resolves the path via `scripts/resolve_pm_dir.sh` (committed source-controlled state, portable across machines). If it's missing on first run, Phase 0 of `cycle.md` **auto-bootstraps it from project signals** (git config, repo basename, deploy workflows in `.github/workflows/`, `pyproject.toml`/`package.json`, README health URLs, docker-compose presence) and continues without prompting. The user can edit the file later if any defaults are wrong; the skill never silently overwrites an already-existing config.

The skill is deliberately project-agnostic — every project-specific knob lives here.

## Required keys

| Key | Type | Notes |
|-----|------|-------|
| `email.to` | str | Recipient address (single user) |
| `email.from` | str | Sender address; must match the `sender_skill`'s configured account |
| `email.subject_prefix` | str | Wraps every release-notes subject, e.g. `[ace-web]` |
| `email.sender_skill` | str | Fully-qualified skill name — autonomous mode invokes this skill to send mail |
| `shipping.branch_prefix` | str | All autonomous PRs branch from this prefix, e.g. `ace-web/auto/` |
| `shipping.pr_label` | str | GitHub label applied to every autonomous PR (visibility) |
| `shipping.merge` | str | One of `squash`, `merge`, `rebase` |
| `shipping.deploy_command` | str | Shell command to trigger deploy after merge |
| `shipping.deploy_workflow` | str | Workflow filename; polled for deploy status |
| `shipping.post_deploy_health` | list[str] | Non-empty list of URLs polled after deploy |
| `testing.unit` / `testing.lint` / `testing.types` | str | Mechanical-check commands run by the gate |
| `testing.prepare` | str (optional) | Bootstrap command run once per sprint at the top of Phase 0, BEFORE any mechanical check. Use this when fresh worktrees lack `.venv` / `node_modules` / similar. Mechanical checks fail in fresh worktrees whose deps aren't built. **Must be lockfile-non-mutating** (`npm ci`, `uv sync --frozen`, `pnpm install --frozen-lockfile`, etc.) — Phase 0 enforces this. A mutating prepare both drags lockfile churn into the autonomous PR and re-trips the clean-worktree precondition on every re-run |
| `testing.dogfood.start_command` | str | Brings the local stack up |
| `testing.dogfood.wait_for` | str | URL polled until the stack is ready |
| `testing.dogfood.base_url` | str | Root URL the headless browser drives |
| `testing.dogfood.headless_browser_skill` | str | Skill name (`gstack`, `browse`, etc.) |
| `guardrails.one_pr_in_flight` | bool | Hardcoded `true` for v1; rejecting any other value is fine |
| `guardrails.diff_size_limit_lines` | int > 0 | Cap fed to `diff_size_check.py` |
| `guardrails.max_fix_forward_attempts` | int > 0 | After this many failed cycles on the same red signal, the sprint logs "stuck" and stops |
| `theme_detection.lens_rotation` | list[str] | Starting lenses for Phase A scout; the sprint is free to mix |

## Validation

Run the validator manually with:

```bash
PLUGIN_PATH=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
CANOPY_PM_DIR=$(bash "$PLUGIN_PATH/skills/product-management/scripts/resolve_pm_dir.sh")
uv run --script "$PLUGIN_PATH/skills/product-management/scripts/validate_autonomous_config.py" "$CANOPY_PM_DIR/autonomous.yaml"
```

The validator's YAML dependency is declared inline (PEP 723) so `uv run --script` resolves it on the fly. Plain `python3` will fail unless your system python already has PyYAML.

Phase 0 of `cycle.md` calls this script automatically.

## Canonical example (ace-web — for illustration; the actual file lives in the ace-web repo, not here)

```yaml
email:
  to: jjackson@dimagi.com
  from: ace@dimagi-ai.com
  subject_prefix: "[ace-web]"
  sender_skill: ace:email-communicator

shipping:
  branch_prefix: ace-web/auto/
  pr_label: autonomous
  merge: squash
  deploy_command: gh workflow run deploy-labs.yml --ref main -f run_migrations=false
  deploy_workflow: deploy-labs.yml
  post_deploy_health:
    - https://labs.connect.dimagi.com/ace/api/health

testing:
  # Optional: runs once at the top of Phase 0. Skip if your worktrees always
  # have deps built. ace-web needs this because emdash worktrees start empty.
  # Must be lockfile-non-mutating: use `uv sync --frozen` / `npm ci` /
  # `pnpm install --frozen-lockfile`. A mutating prepare drags lockfile
  # churn into the autonomous PR's diff and re-trips the clean-worktree
  # precondition on every re-run; Phase 0 enforces this.
  prepare: bash -c "uv sync --frozen && uv pip install pytest pytest-django pytest-asyncio fakeredis ruff && (cd frontend && npm ci)"
  unit:    .venv/bin/python -m pytest -q
  lint:    .venv/bin/python -m ruff check .
  types:   bash -c "cd frontend && node_modules/.bin/tsc -b"
  dogfood:
    base_url: http://localhost:8000/ace
    start_command: docker compose up -d
    wait_for: http://localhost:8000/ace/api/health
    headless_browser_skill: gstack

guardrails:
  one_pr_in_flight: true
  diff_size_limit_lines: 1500
  max_fix_forward_attempts: 3

theme_detection:
  lens_rotation:
    - user-value
    - adoption-blockers
    - integration-depth
    - trust-reliability
    - tech-debt
```
