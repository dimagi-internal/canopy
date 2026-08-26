"""Preflight must resolve the setup block the way the recorder does.

A spec using the ``setup:`` + ``${var}`` late-binding contract — the shape ACE's
`demo-narrative` emits — was un-preflightable: the setup command ran, wrote its
realized map, and nothing read it, so preflight walked the RAW spec and died at
the first navigation on a literal ``https://host${primary_par_url}/``.

Three re-derivations of one contract had drifted, and they only fix together:

  * the outputs file was never loaded and ``${var}`` never substituted;
  * the base-url join was unconditional, so a substituted ABSOLUTE url became
    ``https://host`` + ``https://host/labs/...``;
  * the setup command ran in a guessed ``parents[2]`` rather than the git
    toplevel the recorder uses — a different tree for any spec not sitting
    exactly two directories down, which resolves ``outputs:`` against the
    wrong root.

These are unit-level: preflight's own browser walk needs chromium, but every
defect above is in the pure setup-resolution path, which is where they belong.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.ddd.recipe_preflight import _setup_block, _setup_command
from scripts.narrative.substitution import (
    scene_capture_vars,
    scenes_placeholders,
    substitute_scenes,
)
from scripts.walkthrough._lib.urls import absolutize_url
from scripts.walkthrough.record_video import load_setup_outputs, resolve_setup_cwd


def test_an_absolute_scene_url_is_not_concatenated_onto_the_base():
    """The bug: substitution yields a full url, the join doubled it."""
    base = "https://labs.connect.dimagi.com"
    resolved = "https://labs.connect.dimagi.com/labs/par/7/"
    assert absolutize_url(base, resolved) == resolved
    assert absolutize_url(base, "/labs/par/7/") == f"{base}/labs/par/7/"


def test_the_recorder_and_preflight_share_one_join():
    """Not "both call something equivalent" — literally the same function."""
    from scripts.walkthrough import record_video

    assert record_video.absolutize_url is absolutize_url


def test_setup_outputs_substitute_into_scene_urls(tmp_path):
    outputs = tmp_path / "realized.json"
    outputs.write_text(json.dumps({"primary_par_url": "/labs/par/7/"}))

    scenes = [{"id": "s1", "url": "${primary_par_url}", "actions": []}]
    resolved = substitute_scenes(scenes, load_setup_outputs(outputs))

    assert resolved[0]["url"] == "/labs/par/7/"
    # The input is not mutated — preflight walks the copy, the spec file is untouched.
    assert scenes[0]["url"] == "${primary_par_url}"


def test_a_capture_bound_var_survives_substitution_instead_of_crashing():
    """Preflight cannot capture, so an on-camera var has no value here.

    The recorder permits these to survive for lazy runtime resolution. If
    preflight refused them, wiring substitution in would convert a
    walks-the-wrong-url bug into a hard crash on a legitimate spec.
    """
    scenes = [
        {
            "id": "s1",
            "url": "/runs/",
            "actions": [{"kind": "capture", "target": "css:.run-id", "var": "run_id"}],
        },
        {"id": "s2", "url": "/runs/${run_id}/", "actions": []},
    ]
    capture_bound = set()
    for scene in scenes:
        capture_bound.update(scene_capture_vars(scene))
    assert "run_id" in capture_bound

    resolved = substitute_scenes(scenes, {}, allow_unresolved=capture_bound)
    assert resolved[1]["url"] == "/runs/${run_id}/"


def test_setup_block_reads_a_model_as_well_as_a_dict():
    class _Model:
        def model_dump(self):
            return {"command": "seed.sh", "outputs": "7-synthetic/realized.json",
                    "timeout_seconds": 900}

    assert _setup_block(None) == {}
    assert _setup_block({"command": "x"})["command"] == "x"
    block = _setup_block(_Model())
    assert block["outputs"] == "7-synthetic/realized.json"
    assert _setup_command(_Model()) == ("seed.sh", 900)


def test_outputs_path_resolves_against_the_recorders_cwd_not_a_guess(tmp_path):
    """`parents[2]` is only right when the spec sits exactly two levels down."""
    spec_path = tmp_path / "a" / "b" / "c" / "spec.yaml"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("scenes: []\n")

    # No git repo here, so resolve_setup_cwd falls back to the spec's directory.
    cwd = resolve_setup_cwd(spec_path)
    assert cwd == spec_path.parent
    # The old guess pointed three directories away from it.
    assert spec_path.resolve().parents[2] != cwd


def test_the_reported_symptom_cannot_be_produced_from_a_real_spec(tmp_path):
    """The end-to-end shape, on the fixture whose class actually broke.

    Issue #523's traceback is one string:

        net::ERR_NAME_NOT_RESOLVED at
        https://labs.connect.dimagi.com${primary_par_url}/

    That string needs BOTH defects: no substitution (the ``${...}`` survives)
    and an unconditional join (the base is prepended to it). This walks the
    real `program-admin-report` fixture — ``base_url: https://labs...`` and
    ``url: ${par_url}`` — through the resolution preflight now does, with a
    realized map holding an ABSOLUTE url, which is what the generator writes.
    """
    from scripts.ddd.spec_io import load_spec

    fixture = Path("tests/ddd/fixtures/live_specs/program-admin-report.yaml")
    spec = load_spec(str(fixture))
    base = spec.base_url

    raw_scenes = [
        s.model_dump() if hasattr(s, "model_dump") else dict(s) for s in spec.scenes
    ]
    capture_bound = set()
    for scene in raw_scenes:
        capture_bound.update(scene_capture_vars(scene))

    # Mint every var the spec actually declares, the way the generator does:
    # anything named *_url comes back ABSOLUTE (that is the second defect's
    # trigger), everything else a plain token.
    realized = tmp_path / "realized.json"
    realized.write_text(json.dumps({
        var: (f"{base}/labs/par/4321/" if var.endswith("_url") else f"seed-{var}")
        for var in sorted(scenes_placeholders(raw_scenes) - capture_bound)
    }))
    resolved = substitute_scenes(
        raw_scenes, load_setup_outputs(realized), allow_unresolved=capture_bound
    )

    navigated = [
        absolutize_url(base, s["url"]) for s in resolved if s.get("url")
    ]
    assert navigated, "fixture should declare scene urls"
    for url in navigated:
        assert "${" not in url, f"unsubstituted placeholder survived into a navigation: {url}"
        assert url.count("https://") == 1, f"base doubled onto an absolute url: {url}"
    assert f"{base}/labs/par/4321/" in navigated
