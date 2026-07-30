"""Resolve every selector in a recipe against the live app, before recording.

A render is expensive — ninety seconds of browser, a reseed, an mp4 encode — and
a scene whose target does not resolve still produces a screenshot. The frame
looks plausible, the judge scores it, and the finding that comes back is about
the wrong thing entirely. The first real render of a four-scene narrative went
15/22 actions ok; five of the seven failures were knowable without recording
anything: a class that did not exist, two ``text:`` targets that resolved
ambiguously once the same words appeared in a table row, and a tab switch the
recipe never undid.

This walks the recipe's scenes in order, in one browser, applying navigations
and the state-changing actions that later scenes depend on, and reports every
target that will not resolve. It is deliberately NOT a dry-run of the render:
it does not screenshot, does not record, and does not encode. It answers one
question — will these selectors find their elements — in a few seconds.

Exit codes: 0 clean, 1 unresolved targets found, 2 usage/setup error.

    python -m scripts.ddd.recipe_preflight <recipe.yaml> [--json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Actions that change what is on screen and therefore what LATER scenes can
# resolve against. A preflight that skipped these would report false failures
# for every scene after the first click — which is exactly the tab-switch bug
# that motivated this script.
_STATE_CHANGING = {"click", "fill", "select", "press", "type"}

# `scroll_to` changes nothing about the DOM, so it looks like it belongs with the
# camera moves — but it changes what is REACHABLE. A control below the fold, or
# under a sticky header, resolves and then reports "intercepted or detached" when
# preflight tries to click it, while the render (which does apply the scroll)
# clicks it without complaint. That is a false failure on a correct recipe, and
# it is expensive: it reads exactly like a real one, and the natural response is
# to rewrite a selector that was never wrong.
_SCROLLING = {"scroll_to"}

# `goto` changes the page as completely as a click does, but its target is a URL
# rather than a selector, so it is neither checkable nor state-changing in the
# sense above — it needs its own handling. Skipping it entirely (the original
# behaviour) walks every action AFTER a mid-scene goto against the pre-goto
# page: a false failure when the recipe is right, and worse, a false PASS when a
# selector happens to exist on both pages. Recipes switch persona mid-scene this
# way (a dev-login goto between "what the buyer publishes" and "what the
# supplier receives"), so this is a normal shape, not an exotic one.
_NAVIGATING = {"goto"}

# Actions carrying a target we should check. `hold` has none; `goto` is a URL.
_TARGETED = {
    "click",
    "fill",
    "select",
    "hover",
    "scroll_to",
    "wait_for",
    "press",
    "type",
    "draw",
    "capture",
}


def _setup_command(setup) -> tuple[str | None, int]:
    """The spec's reseed command and its timeout, from a model OR a dict.

    SetupBlock is a pydantic model on a parsed spec and a plain dict on a raw
    one. Reading it as a dict only returned None for the model case, so the
    reseed silently never ran and preflight kept walking a world its own
    previous run had mutated — the exact failure the reseed exists to prevent.
    """
    if setup is None:
        return None, 600
    if not isinstance(setup, dict):
        setup = setup.model_dump() if hasattr(setup, "model_dump") else dict(setup)
    return setup.get("command"), setup.get("timeout_seconds") or 600


def _scene_steps(scene: dict) -> list[tuple[int, str, str]]:
    """(action_index, kind, target) for every action preflight must walk.

    Both the ones it CHECKS (a selector to resolve) and the ones it merely
    REPLAYS to keep the page honest for the checks that follow — a mid-scene
    ``goto``. Order is the recipe's own; the caller depends on it.
    """
    out = []
    for index, action in enumerate(scene.get("actions") or []):
        kind = (action or {}).get("kind")
        target = (action or {}).get("target")
        if not target:
            continue
        if kind in _TARGETED or kind in _NAVIGATING:
            out.append((index, kind, target))
    return out


def preflight(recipe_path: str | Path, *, base_url: str | None = None, timeout_ms: int = 4000) -> dict:
    """Walk the recipe against a live browser and report unresolved targets."""
    from scripts.ddd.spec_io import load_spec

    spec = load_spec(str(recipe_path))
    resolved_base = base_url or getattr(spec, "base_url", None)
    if not resolved_base:
        raise SystemExit("preflight: no base_url on the spec and none passed")

    auth = getattr(spec, "auth", None) or {}
    auth_url = auth.get("url") if isinstance(auth, dict) and auth.get("type") == "url" else None

    # Preflight APPLIES state-changing actions, so that a scene which depends on
    # an earlier click is checked against the screen it will really face. That
    # makes it a mutator: walking a recipe that awards two lots leaves those
    # lots awarded, and the next preflight — or the next render — finds the
    # controls gone. Found by using it: a second run reported every Award
    # target missing, because the first run had clicked them all.
    #
    # So it reseeds first, exactly as the recorder does. The spec's own setup
    # command is the contract for "put the world back"; a recipe without one is
    # assumed non-mutating and walked as-is.
    command, setup_timeout = _setup_command(getattr(spec, "setup", None))
    if command:
        import subprocess

        repo_root = Path(recipe_path).resolve().parents[2]
        print(f"preflight: reseeding via {command}", flush=True)
        result = subprocess.run(
            command, shell=True, cwd=repo_root, capture_output=True, text=True,
            timeout=setup_timeout,
        )
        if result.returncode != 0:
            raise SystemExit(
                f"preflight: setup failed ({command}) — the world is not in a "
                f"checkable state.\n{result.stderr[-800:]}"
            )

    from playwright.sync_api import sync_playwright

    from scripts.walkthrough._lib.targets import resolve_target

    # The recorder's own comparison, imported rather than reimplemented — two
    # normalisers would drift, and the whole value of preflight is that it
    # navigates the way the recorder will.
    from scripts.walkthrough._lib.urls import normalize_url

    findings: list[dict[str, Any]] = []
    checked = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        # Sign the spec's personas in first, in their own throwaway contexts —
        # same helper the recorder uses, so preflight authenticates the way the
        # render will.
        from scripts.walkthrough.identities import mint_identities

        raw_spec = spec.model_dump() if hasattr(spec, "model_dump") else dict(spec)
        identities = mint_identities(browser, raw_spec, resolved_base)

        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            if auth_url:
                page.goto(f"{resolved_base}{auth_url}", wait_until="networkidle", timeout=30000)

            current_identity: str | None = None
            for scene_no, scene in enumerate(spec.scenes, start=1):
                # Become the scene's persona before its nav, exactly as the
                # recorder does — otherwise preflight resolves every selector as
                # whoever happened to be signed in first, and a recipe whose
                # later scenes are a different seat passes here and fails on
                # camera (or worse, silently checks the wrong screen).
                persona = (getattr(scene, "persona", None) or "").strip()
                if persona and persona in identities and persona != current_identity:
                    page.context.clear_cookies()
                    page.context.add_cookies(identities[persona])
                    current_identity = persona

                # Navigate on the browser's ACTUAL url, not on the previous
                # scene's declared one. `SkipSameUrlRecorder` compares against
                # `page.url`, so a scene reached through a redirect (a
                # dev-login `?next=`, an SPA canonicalisation) counts as
                # already-there and its predecessor's state survives. Comparing
                # declared strings instead made preflight navigate where the
                # recorder would not, wiping an open modal the next scene was
                # written to act on — and reporting the recipe as broken.
                scene_url = getattr(scene, "url", None)
                if scene_url:
                    want = f"{resolved_base}{scene_url}"
                    if normalize_url(page.url) != normalize_url(want):
                        page.goto(want, wait_until="networkidle", timeout=30000)

                raw = scene.model_dump() if hasattr(scene, "model_dump") else dict(scene)
                for action_index, kind, target in _scene_steps(raw):
                    if kind in _NAVIGATING:
                        # Replay it, don't check it: the "target" is a URL. Every
                        # later action in this scene has to be resolved against
                        # the page this lands on, not the one before it.
                        page.goto(f"{resolved_base}{target}", wait_until="networkidle", timeout=30000)
                        continue

                    checked += 1
                    try:
                        hit = resolve_target(page, target, timeout_ms=timeout_ms)
                    except Exception as exc:  # a malformed selector is a finding, not a crash
                        hit = None
                        note = f"{type(exc).__name__}: {exc}"
                    else:
                        note = None

                    if hit is None:
                        findings.append(
                            {
                                "scene": scene_no,
                                "scene_id": raw.get("id"),
                                "action_index": action_index,
                                "kind": kind,
                                "target": target,
                                "error": note or "target did not resolve",
                            }
                        )
                        continue

                    # Apply the action when it changes state, so later scenes are
                    # checked against the screen they will really face.
                    if kind in _SCROLLING:
                        try:
                            hit.locator.scroll_into_view_if_needed(timeout=timeout_ms)
                            page.wait_for_timeout(150)
                        except Exception:  # noqa: BLE001
                            # A scroll that cannot complete is not itself a
                            # finding — the element resolved, which is what this
                            # script checks. Later actions will report if the
                            # page ended up somewhere unusable.
                            pass
                        continue

                    if kind in _STATE_CHANGING:
                        try:
                            if kind == "click":
                                hit.locator.click(timeout=timeout_ms)
                            elif kind == "fill":
                                hit.locator.fill(str(raw["actions"][action_index].get("value") or ""), timeout=timeout_ms)
                            elif kind == "select":
                                hit.locator.select_option(str(raw["actions"][action_index].get("value") or ""))
                            page.wait_for_timeout(350)
                        except Exception:
                            # The click resolving is what we are testing; a click
                            # that resolves but is intercepted is a real finding
                            # too, so record it and keep walking.
                            findings.append(
                                {
                                    "scene": scene_no,
                                    "scene_id": raw.get("id"),
                                    "action_index": action_index,
                                    "kind": kind,
                                    "target": target,
                                    "error": "resolved but could not be actioned (intercepted or detached)",
                                }
                            )
        finally:
            browser.close()

    return {
        "recipe": str(recipe_path),
        "base_url": resolved_base,
        "targets_checked": checked,
        "unresolved": len(findings),
        "findings": findings,
        "verdict": "pass" if not findings else "fail",
    }


def _cli() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    as_json = "--json" in sys.argv
    result = preflight(args[0])
    if as_json:
        print(json.dumps(result, indent=1))
    else:
        print(f"preflight: {result['verdict']}  ({result['targets_checked']} targets checked)")
        for finding in result["findings"]:
            print(
                f"  scene {finding['scene']} ({finding['scene_id']}) "
                f"action {finding['action_index']} {finding['kind']}: {finding['target']}"
            )
            print(f"      {finding['error']}")
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
