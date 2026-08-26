"""Unit tests for the docs-sync CI gate.

Covers the pure `check_docs_sync` logic — no subprocess, no gh. The CLI/gh
plumbing is exercised on real PRs (this one's own check, deliberately
sync-failed once during landing verification).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# The script lives under .github/scripts/, which isn't a Python package. Load
# it directly via importlib so we don't have to restructure for tests.
# Register in sys.modules BEFORE exec_module so dataclass's forward-ref
# resolver can find the module by name (otherwise CheckResult's "TriggerMiss"
# forward ref blows up at class-creation time).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / ".github" / "scripts" / "docs_sync_check.py"

_spec = importlib.util.spec_from_file_location("docs_sync_check", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
docs_sync_check = importlib.util.module_from_spec(_spec)
sys.modules["docs_sync_check"] = docs_sync_check
_spec.loader.exec_module(docs_sync_check)

check_docs_sync = docs_sync_check.check_docs_sync
has_opt_out_marker = docs_sync_check.has_opt_out_marker
TRIGGER_PATHS = docs_sync_check.TRIGGER_PATHS


# -----------------------------------------------------------------------------
# has_opt_out_marker
# -----------------------------------------------------------------------------


class TestOptOutMarker:
    def test_no_body_no_marker(self):
        assert has_opt_out_marker(None) == (False, None)
        assert has_opt_out_marker("") == (False, None)

    def test_marker_with_reason(self):
        body = "Some description.\n\nDocs-not-needed: pure refactor with no API change\n"
        assert has_opt_out_marker(body) == (
            True,
            "pure refactor with no API change",
        )

    def test_marker_empty_reason(self):
        body = "Docs-not-needed:"
        present, reason = has_opt_out_marker(body)
        assert present is True
        assert reason == "(no reason given)"

    def test_marker_must_be_at_line_start_after_strip(self):
        # Embedded in prose — doesn't trigger.
        body = "We said Docs-not-needed: nope earlier but actually do."
        assert has_opt_out_marker(body) == (False, None)

    def test_marker_indented_still_works(self):
        # Leading whitespace shouldn't defeat the marker — authors will paste
        # it into a markdown bullet sometimes.
        body = "  Docs-not-needed: indented but valid"
        assert has_opt_out_marker(body) == (True, "indented but valid")


# -----------------------------------------------------------------------------
# check_docs_sync — required scenarios from the spec
# -----------------------------------------------------------------------------


class TestCheckDocsSync:
    def test_models_only_change_fails_listing_both_skill_mds(self):
        # New Action verb shipped without touching either spec or walkthrough.
        result = check_docs_sync(
            changed=["scripts/ddd/schemas/models.py"],
            pr_body="No opt-out here.",
        )
        assert result.failed
        assert len(result.missing) == 1
        miss = result.missing[0]
        assert miss.trigger == "scripts/ddd/schemas/models.py"
        assert sorted(miss.missing_docs) == sorted(
            [
                "plugins/canopy/skills/ddd-spec/SKILL.md",
                "plugins/canopy/skills/walkthrough/SKILL.md",
            ]
        )

    def test_models_plus_ddd_spec_only_lists_walkthrough(self):
        # Author updated one of two — gate fails listing only the still-missing one.
        result = check_docs_sync(
            changed=[
                "scripts/ddd/schemas/models.py",
                "plugins/canopy/skills/ddd-spec/SKILL.md",
            ],
            pr_body="",
        )
        assert result.failed
        assert len(result.missing) == 1
        assert result.missing[0].missing_docs == [
            "plugins/canopy/skills/walkthrough/SKILL.md"
        ]

    def test_models_plus_both_skill_mds_passes(self):
        result = check_docs_sync(
            changed=[
                "scripts/ddd/schemas/models.py",
                "plugins/canopy/skills/ddd-spec/SKILL.md",
                "plugins/canopy/skills/walkthrough/SKILL.md",
            ],
            pr_body="",
        )
        assert result.passed
        assert result.missing == []
        assert result.opt_out_reason is None

    def test_models_only_with_opt_out_passes_with_reason(self):
        result = check_docs_sync(
            changed=["scripts/ddd/schemas/models.py"],
            pr_body="Refactor of Pydantic model imports.\n\nDocs-not-needed: pure import reorganization\n",
        )
        assert result.passed
        assert result.opt_out_reason == "pure import reorganization"
        # The misses are still recorded so the workflow log can show which
        # triggers were opted out of.
        assert len(result.missing) == 1
        assert result.missing[0].trigger == "scripts/ddd/schemas/models.py"

    def test_rubric_change_without_concept_eval_skill_fails(self):
        result = check_docs_sync(
            changed=["plugins/canopy/skills/ddd-concept-eval/rubric.yaml"],
            pr_body="",
        )
        assert result.failed
        assert len(result.missing) == 1
        miss = result.missing[0]
        assert miss.trigger == "plugins/canopy/skills/ddd-concept-eval/rubric.yaml"
        assert miss.missing_docs == ["plugins/canopy/skills/ddd-concept-eval/SKILL.md"]

    def test_unrelated_change_passes_silently(self):
        # PR touches only non-trigger files — gate is a no-op.
        result = check_docs_sync(
            changed=[
                "README.md",
                "tests/test_something_unrelated.py",
                "scripts/walkthrough/_lib/results.py",  # NOT in TRIGGER_PATHS (telemetry shape, not author surface)
            ],
            pr_body="",
        )
        assert result.passed
        assert result.missing == []
        assert result.opt_out_reason is None

    def test_empty_pr_passes(self):
        # Defensive — empty PR (somehow) shouldn't crash the gate.
        result = check_docs_sync(changed=[], pr_body="")
        assert result.passed
        assert result.missing == []


# -----------------------------------------------------------------------------
# Recorder + record_video flag triggers — extra coverage for the other two
# categories in the spec.
# -----------------------------------------------------------------------------


class TestRecorderAndRecordVideoTriggers:
    def test_recorder_change_requires_both_skill_mds(self):
        result = check_docs_sync(
            changed=["scripts/walkthrough/_lib/recorder.py"],
            pr_body="",
        )
        assert result.failed
        miss = result.missing[0]
        assert sorted(miss.missing_docs) == sorted(
            [
                "plugins/canopy/skills/ddd-spec/SKILL.md",
                "plugins/canopy/skills/walkthrough/SKILL.md",
            ]
        )

    def test_record_video_cli_change_requires_ddd_run(self):
        # CLI flag landed without telling the orchestrator skill — fail.
        result = check_docs_sync(
            changed=["scripts/walkthrough/record_video.py"],
            pr_body="",
        )
        assert result.failed
        miss = result.missing[0]
        assert miss.trigger == "scripts/walkthrough/record_video.py"
        assert miss.missing_docs == ["plugins/canopy/skills/ddd-run/SKILL.md"]

    def test_multi_trigger_change_reports_all_misses(self):
        # Touches two triggers — both should appear in result.missing.
        result = check_docs_sync(
            changed=[
                "scripts/walkthrough/record_video.py",
                "plugins/canopy/skills/ddd-concept-eval/rubric.yaml",
            ],
            pr_body="",
        )
        assert result.failed
        assert len(result.missing) == 2
        triggers = {m.trigger for m in result.missing}
        assert triggers == {
            "scripts/walkthrough/record_video.py",
            "plugins/canopy/skills/ddd-concept-eval/rubric.yaml",
        }


# -----------------------------------------------------------------------------
# Smoke test: the failure message string contains the structured info the spec
# requires — trigger paths, missing docs, the "Why this matters" rationale,
# and the opt-out marker syntax.
# -----------------------------------------------------------------------------


class TestFailureMessage:
    def test_message_includes_required_pieces(self):
        result = check_docs_sync(
            changed=["scripts/ddd/schemas/models.py"],
            pr_body="",
        )
        assert result.failed
        msg = docs_sync_check.format_failure_message(result)
        # Trigger path called out.
        assert "scripts/ddd/schemas/models.py" in msg
        # Missing doc enumerated.
        assert "plugins/canopy/skills/ddd-spec/SKILL.md" in msg
        assert "plugins/canopy/skills/walkthrough/SKILL.md" in msg
        # Rationale cites the prior gap PRs.
        assert "#100" in msg and "#115" in msg
        # Opt-out marker syntax taught.
        assert "Docs-not-needed:" in msg


# -----------------------------------------------------------------------------
# TRIGGER_PATHS sanity: every key and value points at a real file in the repo
# right now. If someone renames a path without updating the mapping, this
# test fails loudly (vs the gate silently no-opping in CI).
# -----------------------------------------------------------------------------


class TestTriggerPathsAreReal:
    @pytest.mark.parametrize("trigger", list(TRIGGER_PATHS.keys()))
    def test_trigger_path_exists(self, trigger: str):
        path = _REPO_ROOT / trigger
        assert path.exists(), (
            f"TRIGGER_PATHS key {trigger!r} doesn't exist in the repo —"
            " the gate will silently no-op for this trigger. Update the"
            " mapping in .github/scripts/docs_sync_check.py to match the"
            " renamed source path."
        )

    @pytest.mark.parametrize(
        "doc",
        sorted({d for docs in TRIGGER_PATHS.values() for d in docs}),
    )
    def test_required_doc_exists(self, doc: str):
        path = _REPO_ROOT / doc
        assert path.exists(), (
            f"TRIGGER_PATHS value {doc!r} doesn't exist in the repo — the"
            " gate would fail every PR that touches the trigger because the"
            " required doc can't be updated. Update the mapping."
        )


# -----------------------------------------------------------------------------
# fetch_pr_context / main — behaviour when the GitHub API cannot be read.
#
# canopy #496: during the 2026-08-17 GitHub incident the gate crashed with a
# CalledProcessError traceback and went red on a PR whose diff was fine. A red
# gate reads as "my PR is wrong", so the expensive direction of this error is
# someone editing a correct diff to satisfy a check that never evaluated it.
# A gate that cannot look must SKIP loudly, not fail.
# -----------------------------------------------------------------------------

fetch_pr_context = docs_sync_check.fetch_pr_context
PRContextUnavailable = docs_sync_check.PRContextUnavailable


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _runner(responses):
    """Build a subprocess.run stand-in that replays `responses` in order."""
    calls = []

    def run(cmd, *args, **kwargs):
        calls.append(cmd)
        resp = responses[min(len(calls) - 1, len(responses) - 1)]
        return resp

    run.calls = calls
    return run


class TestFetchPrContextResilience:
    def test_transient_failure_is_retried_then_succeeds(self, monkeypatch):
        # Fail once (a 503 blip), then serve files, then the body.
        responses = [
            _FakeProc(1, stderr="HTTP 503: Service Unavailable"),
            _FakeProc(0, stdout="scripts/ddd/schemas/models.py\n"),
            _FakeProc(0, stdout="a body"),
        ]
        run = _runner(responses)
        monkeypatch.setattr(docs_sync_check.subprocess, "run", run)

        changed, body = fetch_pr_context("495", "o/r", sleep=lambda _s: None)

        assert changed == ["scripts/ddd/schemas/models.py"]
        assert body == "a body"
        assert len(run.calls) == 3  # one retry + files + body

    def test_persistent_failure_raises_pr_context_unavailable_not_called_process_error(
        self, monkeypatch
    ):
        run = _runner([_FakeProc(1, stderr="HTTP 503: Service Unavailable")])
        monkeypatch.setattr(docs_sync_check.subprocess, "run", run)

        with pytest.raises(PRContextUnavailable) as exc:
            fetch_pr_context("495", "o/r", attempts=2, sleep=lambda _s: None)

        # The message must name the API failure, not just the exit status —
        # the whole point is that the log says what actually went wrong.
        assert "503" in str(exc.value)
        assert len(run.calls) == 2

    def test_empty_file_list_is_a_failed_read_not_a_clean_pass(self, monkeypatch):
        # A PR always changes at least one file. If the gate treated an empty
        # list as "no trigger paths touched" it would silently APPROVE exactly
        # the PRs it exists to catch.
        run = _runner([_FakeProc(0, stdout="\n  \n")])
        monkeypatch.setattr(docs_sync_check.subprocess, "run", run)

        with pytest.raises(PRContextUnavailable) as exc:
            fetch_pr_context("495", "o/r", sleep=lambda _s: None)

        assert "no changed files" in str(exc.value)

    def test_reads_via_rest_not_graphql(self, monkeypatch):
        # REST stayed up through the incident that GraphQL did not survive.
        run = _runner([_FakeProc(0, stdout="README.md\n"), _FakeProc(0, stdout="b")])
        monkeypatch.setattr(docs_sync_check.subprocess, "run", run)

        fetch_pr_context("495", "o/r", sleep=lambda _s: None)

        files_cmd = run.calls[0]
        assert files_cmd[:2] == ["gh", "api"]
        assert "repos/o/r/pulls/495/files" in files_cmd
        assert "view" not in files_cmd
        # REST names this field `filename`; the GraphQL call this replaced
        # named it `path`. Asking for `.path` against REST yields a column of
        # nulls, i.e. a successful call that returns no filenames — caught
        # live while landing #496, and pinned here so it cannot come back.
        assert ".[].filename" in files_cmd
        assert ".[].path" not in files_cmd


class TestMainSkipsWhenApiUnreadable:
    def test_main_exits_zero_and_says_skipped(self, monkeypatch, capsys):
        def boom(*_a, **_k):
            raise PRContextUnavailable("HTTP 503: Service Unavailable")

        monkeypatch.setattr(docs_sync_check, "fetch_pr_context", boom)

        rc = docs_sync_check.main(["--pr-number", "495", "--repo", "o/r"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "SKIPPED" in out
        assert "503" in out
        # It must not read as a pass — a skipped gate that looks green is how
        # a real docs miss would sail through unnoticed.
        assert "docs-sync: ok" not in out
        assert "::warning::" in out

    def test_real_miss_still_fails(self, monkeypatch, capsys):
        # The skip path must not soften a genuine finding.
        monkeypatch.setattr(
            docs_sync_check,
            "fetch_pr_context",
            lambda *_a, **_k: (["scripts/ddd/schemas/models.py"], ""),
        )

        rc = docs_sync_check.main(["--pr-number", "495", "--repo", "o/r"])

        assert rc == 1
        assert "::error::" in capsys.readouterr().out


class TestChangedFilesSeparators:
    """`--changed-files` must not silently swallow a mis-separated list.

    The gate's help said "newline- or comma-separated", and it split on exactly
    those. The natural local invocation is to paste `git diff --name-only`
    output, which arrives SPACE-separated through `$(...)` or `tr` — so all the
    paths became one string containing spaces, matched no trigger path, and the
    gate printed its own success line having evaluated nothing.

    That is the worst failure shape a gate has: a confident false PASS. It
    happened on canopy#529 (2026-08-26), whose PR body then claimed a docs-sync
    pass the gate had never given; CI failed on the same commit minutes later.
    """

    def _run(self, capsys, changed, body=""):
        rc = docs_sync_check.main(
            ["--changed-files", changed, "--pr-body", body]
        )
        return rc, capsys.readouterr().out

    def test_space_separated_paths_are_split(self, capsys):
        rc, out = self._run(
            capsys,
            "scripts/ddd/recipe_preflight.py scripts/walkthrough/record_video.py",
        )
        assert rc == 1, out
        assert "record_video.py" in out

    def test_comma_separated_still_works(self, capsys):
        rc, out = self._run(
            capsys,
            "scripts/ddd/recipe_preflight.py,scripts/walkthrough/record_video.py",
        )
        assert rc == 1, out

    def test_newline_separated_still_works(self, capsys):
        rc, out = self._run(
            capsys,
            "scripts/ddd/recipe_preflight.py\nscripts/walkthrough/record_video.py",
        )
        assert rc == 1, out

    def test_mixed_separators_and_padding(self, capsys):
        rc, out = self._run(
            capsys,
            "  a.py,\n  scripts/walkthrough/record_video.py \t b.py ",
        )
        assert rc == 1, out

    def test_a_clean_diff_still_passes_in_every_form(self, capsys):
        for changed in ("tests/foo.py README.md",
                        "tests/foo.py,README.md",
                        "tests/foo.py\nREADME.md"):
            rc, out = self._run(capsys, changed)
            assert rc == 0, (changed, out)

    def test_the_docs_not_needed_optout_still_applies(self, capsys):
        rc, out = self._run(
            capsys,
            "scripts/walkthrough/record_video.py other.py",
            body="Docs-not-needed: pure refactor, no authoring surface changed.",
        )
        assert rc == 0, out
