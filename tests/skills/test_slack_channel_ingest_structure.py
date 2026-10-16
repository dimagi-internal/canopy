"""Structural invariants for the slack-channel-ingest skill (canopy-web#1533).

Pins what the skill's behaviour hangs on: the CLI verbs it runs (a rename breaks the
skill silently), the exit codes it branches on (they must match `slack_ingest_cli`),
and the consent rule in the description, which is what skills are matched on.
"""
from __future__ import annotations

from pathlib import Path

from orchestrator import slack_ingest_cli

SKILL = (Path(__file__).parent.parent.parent / "plugins" / "canopy" / "skills"
         / "slack-channel-ingest" / "SKILL.md")


def _parts() -> tuple[str, str]:
    text = SKILL.read_text()
    assert text.startswith("---\n")
    front, _, body = text[4:].partition("\n---\n")
    return front, body


def test_names_every_cli_verb_it_runs() -> None:
    _front, body = _parts()
    for verb in ("request", "wait", "status", "transcript", "revoke", "list"):
        assert f"canopy slack ingest {verb}" in body, verb
        assert verb in slack_ingest_cli.ingest_group.commands, verb


def test_the_exit_codes_it_branches_on_are_the_clis() -> None:
    _front, body = _parts()
    assert (slack_ingest_cli.EXIT_READY, slack_ingest_cli.EXIT_REFUSED,
            slack_ingest_cli.EXIT_FAILED, slack_ingest_cli.EXIT_WAITING) == (0, 4, 5, 6)
    assert "exit 0 ready · 4 declined/revoked · 5 failed · 6 still open" in body


def test_description_states_the_consent_rule() -> None:
    front, _body = _parts()
    assert "ONLY with" in front and "permission" in front
    assert len(front) <= 1024
