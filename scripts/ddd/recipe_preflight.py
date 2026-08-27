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

Auth is the recorder's, not a second one. A spec whose surfaces sit behind a
login is preflighted with the SAME session the render will use — pass
``--storage-state`` (or ``--cookies``) exactly as ``record_video.py`` takes it.
Without that, preflight walked such a spec logged out and reported every target
as ``target did not resolve``, which is indistinguishable from a genuinely
broken recipe: a full-red report on a correct recipe (canopy#532).

Exit codes: 0 clean, 1 unresolved targets found, 2 usage/setup error.

    python -m scripts.ddd.recipe_preflight <recipe.yaml> [--json]
        [--storage-state /tmp/state.json | --cookies /tmp/cookies.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scripts.narrative.substitution import (
    UnresolvedPlaceholderError,
    scene_capture_vars,
    scenes_placeholders,
    substitute_scenes,
)
from scripts.walkthrough._lib.urls import absolutize_url

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


def _setup_block(setup) -> dict:
    """The spec's ``setup:`` block as a plain dict, from a model OR a dict.

    SetupBlock is a pydantic model on a parsed spec and a plain dict on a raw
    one. Reading it as a dict only returned None for the model case, so the
    reseed silently never ran and preflight kept walking a world its own
    previous run had mutated — the exact failure the reseed exists to prevent.
    """
    if setup is None:
        return {}
    if not isinstance(setup, dict):
        setup = setup.model_dump() if hasattr(setup, "model_dump") else dict(setup)
    return dict(setup)


def _setup_command(setup) -> tuple[str | None, int]:
    """The reseed command and its timeout. Thin reader over :func:`_setup_block`."""
    block = _setup_block(setup)
    if not block:
        return None, 600
    return block.get("command"), block.get("timeout_seconds") or 600


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


def logged_out_hint(
    *, checked: int, unresolved: int, session_supplied: bool, authenticated: bool
) -> str | None:
    """Name the likely cause when EVERY target missed and nobody was signed in.

    A 100%-unresolved result is far more likely "you are logged out" than "every
    selector is wrong", and saying so is the difference between re-running with
    a session and rewriting selectors that were never broken (canopy#532). It is
    deliberately narrow: one surviving target proves the browser could see the
    app, so the hint stays silent.
    """
    if checked and unresolved == checked and not session_supplied and not authenticated:
        return (
            "every target missed and this run had no session: no --storage-state, "
            "no --cookies, no auth: block on the spec, and no form identities to "
            "mint. A logged-out preflight misses every selector on a correct "
            "recipe — re-run with the session file the recorder uses before "
            "changing anything."
        )
    return None


def preflight(
    recipe_path: str | Path,
    *,
    base_url: str | None = None,
    timeout_ms: int = 4000,
    storage_state: str | Path | None = None,
    cookies: str | Path | None = None,
) -> dict:
    """Walk the recipe against a live browser and report unresolved targets.

    ``storage_state`` / ``cookies`` are the recorder's own auth inputs, with the
    recorder's precedence (storage_state wins) and the recorder's consequence
    (a supplied session skips the spec's URL-auth nav). Preflight exists to
    navigate the way the recorder will; before canopy#532 it could not, because
    it had no way to be handed a session at all.
    """
    from scripts.ddd.spec_io import load_spec

    spec = load_spec(str(recipe_path))
    resolved_base = base_url or getattr(spec, "base_url", None)
    if not resolved_base:
        raise SystemExit("preflight: no base_url on the spec and none passed")

    auth = getattr(spec, "auth", None) or {}
    auth_url = auth.get("url") if isinstance(auth, dict) and auth.get("type") == "url" else None

    # The recorder's precedence, imported as behaviour rather than re-invented:
    # storage_state wins over cookies, and either one means the spec's URL-auth
    # nav is skipped (record_video.py: `if not args.cookies and not
    # args.storage_state`). storage_state must be handed to new_context —
    # Playwright cannot load it onto a context that already exists — which is
    # why preflight opens a context explicitly instead of browser.new_page().
    cookies_data: list | None = None
    if cookies and not storage_state:
        cookies_data = json.loads(Path(cookies).read_text()) or None
    session_supplied = bool(storage_state or cookies_data)

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
    # Everything the recorder derives from the setup block is imported from it,
    # never re-derived here. Three separate re-derivations of this contract had
    # already drifted (the cwd, the outputs load, the url join) and each one
    # turns preflight into a check of a world the render will not see.
    from scripts.walkthrough.record_video import (
        SetupError,
        load_setup_outputs,
        resolve_setup_cwd,
    )

    setup_block = _setup_block(getattr(spec, "setup", None))
    command, setup_timeout = _setup_command(getattr(spec, "setup", None))
    # The recorder runs setup from the git toplevel holding the spec, because
    # setup commands are written repo-root-relative. Preflight used to guess
    # `parents[2]` — right only for a spec that happens to sit exactly two
    # directories below the root, and silently the wrong tree otherwise.
    setup_cwd = resolve_setup_cwd(Path(recipe_path))
    outputs_rel = setup_block.get("outputs")
    outputs_path = (setup_cwd / outputs_rel) if outputs_rel else None

    if command:
        import subprocess

        print(f"preflight: reseeding via {command}", flush=True)
        result = subprocess.run(
            command, shell=True, cwd=str(setup_cwd), capture_output=True, text=True,
            timeout=setup_timeout,
        )
        if result.returncode != 0:
            raise SystemExit(
                f"preflight: setup failed ({command}) — the world is not in a "
                f"checkable state.\n{result.stderr[-800:]}"
            )

    # The setup command MINTS the ids the scenes address (run ids, entity ids,
    # dates) and writes them to its outputs file; the spec refers to them as
    # ${var}. Running the command and then walking the RAW spec — which is what
    # this did — navigates to a literal "https://host${primary_par_url}/" and
    # dies at the first goto, so every spec using the late-binding contract was
    # un-preflightable. Load and substitute exactly as record_video does.
    raw_scenes = [
        scene.model_dump() if hasattr(scene, "model_dump") else dict(scene)
        for scene in spec.scenes
    ]
    setup_vars: dict = {}
    if outputs_path is not None:
        try:
            setup_vars = load_setup_outputs(outputs_path)
        except SetupError as exc:
            raise SystemExit(f"preflight: {exc}")
    if setup_vars or scenes_placeholders(raw_scenes):
        # Vars minted ON CAMERA by a `capture` action cannot be known here —
        # preflight does not capture. The recorder permits them to survive
        # substitution for lazy resolution at runtime, and so must preflight,
        # or a legitimate spec turns a wrong-URL walk into a hard crash.
        capture_bound: set[str] = set()
        for scene in raw_scenes:
            capture_bound.update(scene_capture_vars(scene))
        try:
            raw_scenes = substitute_scenes(
                raw_scenes, setup_vars, allow_unresolved=capture_bound
            )
        except UnresolvedPlaceholderError as exc:
            raise SystemExit(f"preflight: {exc}")

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
        # Mint against the SUBSTITUTED scenes — personas are read out of them,
        # and the rest of this function walks the substituted copy.
        raw_spec["scenes"] = raw_scenes
        identities = mint_identities(browser, raw_spec, resolved_base)

        context_kwargs: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}}
        if storage_state:
            context_kwargs["storage_state"] = str(storage_state)
        context = browser.new_context(**context_kwargs)
        if cookies_data:
            context.add_cookies(cookies_data)
        page = context.new_page()
        try:
            if auth_url and not session_supplied:
                page.goto(absolutize_url(resolved_base, auth_url), wait_until="networkidle", timeout=30000)

            current_identity: str | None = None
            for scene_no, raw in enumerate(raw_scenes, start=1):
                # Become the scene's persona before its nav, exactly as the
                # recorder does — otherwise preflight resolves every selector as
                # whoever happened to be signed in first, and a recipe whose
                # later scenes are a different seat passes here and fails on
                # camera (or worse, silently checks the wrong screen).
                persona = (raw.get("persona") or "").strip()
                switched_identity = False
                if persona and persona in identities and persona != current_identity:
                    page.context.clear_cookies()
                    page.context.add_cookies(identities[persona])
                    current_identity = persona
                    switched_identity = True

                # Navigate on the browser's ACTUAL url, not on the previous
                # scene's declared one. `SkipSameUrlRecorder` compares against
                # `page.url`, so a scene reached through a redirect (a
                # dev-login `?next=`, an SPA canonicalisation) counts as
                # already-there and its predecessor's state survives. Comparing
                # declared strings instead made preflight navigate where the
                # recorder would not, wiping an open modal the next scene was
                # written to act on — and reporting the recipe as broken.
                scene_url = raw.get("url")
                if scene_url:
                    # Absolute once ${var} resolved to a generator-minted url —
                    # the guard the recorder has always had.
                    want = absolutize_url(resolved_base, scene_url)
                    # A cookie swap does not repaint the page, so an identity
                    # change must re-navigate even when the url is unchanged —
                    # otherwise the session is the new persona and the DOM is
                    # still the old one's, and every role-dependent selector
                    # fails against the wrong seat's app.
                    if switched_identity or normalize_url(page.url) != normalize_url(want):
                        page.goto(want, wait_until="networkidle", timeout=30000)

                for action_index, kind, target in _scene_steps(raw):
                    if kind in _NAVIGATING:
                        # Replay it, don't check it: the "target" is a URL. Every
                        # later action in this scene has to be resolved against
                        # the page this lands on, not the one before it.
                        page.goto(absolutize_url(resolved_base, target), wait_until="networkidle", timeout=30000)
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
        "hint": logged_out_hint(
            checked=checked,
            unresolved=len(findings),
            session_supplied=session_supplied,
            authenticated=bool(auth_url or identities),
        ),
    }


def _cli() -> int:
    # Real parsing, not a `startswith("--")` filter: that filter drops the flag
    # and KEEPS its value as a positional, so any value-bearing option silently
    # became the recipe path's neighbour (canopy#532).
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ddd.recipe_preflight",
        description="Resolve every selector in a recipe against the live app.",
    )
    parser.add_argument("recipe", nargs="?", help="path to the unified spec / recipe YAML")
    parser.add_argument("--json", action="store_true", help="emit the full result as JSON")
    parser.add_argument(
        "--storage-state",
        help=(
            "Playwright storage_state JSON (path), applied at context creation — "
            "the recorder's own flag. Use for any spec whose surfaces need a "
            "pre-existing session. Wins over --cookies."
        ),
    )
    parser.add_argument(
        "--cookies",
        help="cookies JSON exported by `browse cookies`; ignored when --storage-state is given",
    )
    ns = parser.parse_args()
    if not ns.recipe:
        print(__doc__, file=sys.stderr)
        return 2
    as_json = ns.json
    result = preflight(
        ns.recipe, storage_state=ns.storage_state, cookies=ns.cookies
    )
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
        if result.get("hint"):
            print(f"  hint: {result['hint']}")
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
