"""Per-pass provenance: a multi-pass judge must prove its passes returned (canopy#548).

Reproduces both incidents from ``hh-poverty-targeting-answer-quality-2026-08-27-001``:
the actionability judge's derivation sub-agents were killed and the parent wrote a
verdict quoting passes that never returned; the arc judge stalled and its output
was invented. Both verdicts were well-formed and passed the model check — which is
why the only thing that can catch them is the sealed pass files on disk.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.ddd import passes
from scripts.ddd.validate import validate


def _write_pass(run_dir: Path, kind: str, pass_id: str, text: str, *, sealed: bool = True) -> Path:
    pdir = passes.passes_dir(run_dir, kind)
    pdir.mkdir(parents=True, exist_ok=True)
    p = pdir / f"{pass_id}.md"
    p.write_text(text)
    if sealed:
        passes.seal(p)
    return p


def _verdict(run_dir: Path, kind: str, **extra) -> Path:
    body = {
        "schema_version": 1,
        "kind": kind,
        "dimensions": {"coverage": {"score": 3, "weight": 1.0}},
        "overall_score": 3,
        "verdict": "warn",
    }
    body.update(extra)
    p = run_dir / f"verdict-{kind}.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


PLANS = {
    "d1": "POST /tasks creates a task\nStatus dropdown on the task card",
    "d2": "POST /tasks endpoint\nA status select with open/closed",
    "d3": "tasks table with status column",
}


def _three_sealed(run_dir: Path, kind: str = "actionability") -> None:
    for pid, text in PLANS.items():
        _write_pass(run_dir, kind, pid, text)


class TestIncidentOneKilledDerivations:
    def test_a_verdict_with_no_passes_block_is_rejected_at_emit(self, tmp_path) -> None:
        """The measured shape: plausible scores, no proof any pass returned."""
        ok, problems = validate("verdict", _verdict(tmp_path, "actionability"))
        assert ok is False
        assert any("passes: required" in p for p in problems), problems

    def test_manifest_refuses_when_a_pass_never_sealed(self, tmp_path) -> None:
        _write_pass(tmp_path, "actionability", "d1", PLANS["d1"])
        _write_pass(tmp_path, "actionability", "d2", PLANS["d2"][:10], sealed=False)  # killed mid-write
        with pytest.raises(ValueError, match="'d2'.*never sealed"):
            passes.manifest(tmp_path, "actionability", expect=3)

    def test_manifest_refuses_when_a_pass_left_no_file_at_all(self, tmp_path) -> None:
        _write_pass(tmp_path, "actionability", "d1", PLANS["d1"])
        _write_pass(tmp_path, "actionability", "d2", PLANS["d2"])
        with pytest.raises(ValueError, match="2 of 3"):
            passes.manifest(tmp_path, "actionability", expect=3)

    def test_a_quote_no_pass_wrote_is_caught(self, tmp_path) -> None:
        """The invented per-pass quote — the strongest-looking evidence, and false."""
        _three_sealed(tmp_path)
        block = passes.manifest(tmp_path, "actionability", expect=3)
        v = _verdict(
            tmp_path,
            "actionability",
            passes=block,
            pass_quotes=[{"pass_id": "d2", "quote": "a Kanban board with drag-and-drop lanes"}],
        )
        ok, problems = validate("verdict", v)
        assert ok is False
        assert any("does not appear" in p and "'d2'" in p for p in problems), problems

    def test_a_quote_attributed_to_an_unlisted_pass_is_caught(self, tmp_path) -> None:
        _three_sealed(tmp_path)
        block = passes.manifest(tmp_path, "actionability", expect=3)
        v = _verdict(
            tmp_path,
            "actionability",
            passes=block,
            pass_quotes=[{"pass_id": "d4", "quote": "POST /tasks"}],
        )
        ok, problems = validate("verdict", v)
        assert ok is False
        assert any("'d4'" in p for p in problems), problems


class TestIncidentTwoStalledArcJudge:
    def test_a_verdict_citing_a_pass_that_never_returned_is_rejected(self, tmp_path) -> None:
        v = _verdict(
            tmp_path,
            "arc",
            passes=[{"pass_id": "arc-1", "returned_at": "2026-08-27T08:44:00+00:00", "sha256": "0" * 64}],
        )
        ok, problems = validate("verdict", v)
        assert ok is False
        assert any("never returned" in p for p in problems), problems

    def test_kind_is_inferred_from_the_standard_filename(self, tmp_path) -> None:
        """An arc verdict that omits `kind` is still an arc verdict."""
        p = tmp_path / "verdict-arc.yaml"
        p.write_text(yaml.safe_dump({"dimensions": {}, "overall_score": 3, "verdict": "warn"}))
        ok, problems = validate("verdict", p)
        assert ok is False
        assert any("passes: required" in x for x in problems), problems


class TestTamperAndControls:
    def test_the_real_run_passes(self, tmp_path) -> None:
        """Control: every pass sealed, manifest used verbatim, quotes real."""
        _three_sealed(tmp_path)
        block = passes.manifest(tmp_path, "actionability", expect=3)
        v = _verdict(
            tmp_path,
            "actionability",
            passes=block,
            pass_quotes=[{"pass_id": "d1", "quote": "Status   dropdown on the\ntask card"}],
        )
        ok, problems = validate("verdict", v)
        assert ok is True, problems

    def test_a_payload_edited_after_sealing_is_caught(self, tmp_path) -> None:
        _three_sealed(tmp_path)
        block = passes.manifest(tmp_path, "actionability", expect=3)
        (passes.passes_dir(tmp_path, "actionability") / "d3.md").write_text("rewritten by the parent")
        ok, problems = validate("verdict", _verdict(tmp_path, "actionability", passes=block))
        assert ok is False
        assert any("changed after it was sealed" in p for p in problems), problems

    def test_a_hand_edited_digest_is_caught(self, tmp_path) -> None:
        _three_sealed(tmp_path)
        block = passes.manifest(tmp_path, "actionability", expect=3)
        block[0]["sha256"] = "f" * 64
        ok, problems = validate("verdict", _verdict(tmp_path, "actionability", passes=block))
        assert ok is False
        assert any("does not match its seal" in p for p in problems), problems

    def test_a_pass_returns_once(self, tmp_path) -> None:
        p = _write_pass(tmp_path, "arc", "arc-1", "the story in one sentence")
        with pytest.raises(ValueError, match="returns once"):
            passes.seal(p)

    def test_seal_refuses_a_payload_outside_the_passes_layout(self, tmp_path) -> None:
        stray = tmp_path / "d1.md"
        stray.write_text("x")
        with pytest.raises(ValueError, match="passes/<kind>"):
            passes.seal(stray)

    def test_seal_refuses_an_empty_payload(self, tmp_path) -> None:
        pdir = passes.passes_dir(tmp_path, "arc")
        pdir.mkdir(parents=True)
        (pdir / "arc-1.md").write_text("")
        with pytest.raises(ValueError, match="empty payload"):
            passes.seal(pdir / "arc-1.md")

    def test_a_blocked_verdict_needs_no_passes(self, tmp_path) -> None:
        """blocked is the honest outcome for a pass that would not return — the
        gate must not push a judge toward synthesising one to get past it."""
        v = _verdict(
            tmp_path,
            "actionability",
            verdict="blocked",
            blocking_reason="derivation d2 did not return after re-dispatch",
        )
        ok, problems = validate("verdict", v)
        assert ok is True, problems

    def test_single_pass_kinds_are_not_required_to_carry_passes(self, tmp_path) -> None:
        """user_artifact / timing / why have no children to narrate around."""
        ok, problems = validate("verdict", _verdict(tmp_path, "user_artifact"))
        assert ok is True, problems

    def test_the_cli_manifest_exits_nonzero_naming_the_missing_pass(self, tmp_path, capsys) -> None:
        _write_pass(tmp_path, "arc", "arc-1", "story")
        rc = passes._main(["manifest", str(tmp_path), "arc", "--expect", "2"])
        assert rc == 1
        assert "1 of 2" in capsys.readouterr().err

    def test_the_cli_manifest_output_round_trips_into_a_valid_verdict(self, tmp_path, capsys) -> None:
        _write_pass(tmp_path, "arc", "arc-1", "story")
        assert passes._main(["manifest", str(tmp_path), "arc", "--expect", "1"]) == 0
        block = yaml.safe_load(capsys.readouterr().out)["passes"]
        ok, problems = validate("verdict", _verdict(tmp_path, "arc", passes=block))
        assert ok is True, problems
