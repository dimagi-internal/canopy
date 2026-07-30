"""Sign personas in OFF CAMERA, before the recording context exists.

A DDD narrative often moves between seats — the buyer publishes a tender, the
supplier bids, the reviewer scores it. Each seat is a different logged-in user,
and the recorder records ONE Playwright context whose video starts with the page
and cannot be paused. So any authentication performed on that page is in the
deliverable by construction: four persona switches meant four login forms on
film, in a demo whose subject is procurement.

The apps this was built for do have a persona-switch endpoint, and it is
correctly a hard 403 outside DEBUG — the site has open registration on a public
host, so an endpoint that logs a caller in as a procurement admin without a
password is only defensible while it refuses in production. Weakening that to get
a prettier video is the wrong trade.

So: log each persona in HERE, in a throwaway context that is never recorded, and
hand the resulting cookies to the recorder. Mid-render it swaps cookies and
navigates, so the identity changes between two frames of ordinary browsing.

Spec shape (``auth`` on the UnifiedSpec)::

    auth:
      type: form
      login_url: /supply/login/
      fields:
        email: input[name="email"]
        password: input[name="password"]
      submit: button[type="submit"]
      success: .navitem            # optional: proves the login landed
      password_env: SUPPLY_DEMO_PASSWORD
      personas:
        ada: oes-lead@oes.example
        tomas: oes-review@oes.example

Credentials are never in the spec: ``password_env`` names an environment
variable, and a per-persona mapping may do the same. The spec is committed; the
password is not.
"""
from __future__ import annotations

import os
from typing import Any


class IdentityError(RuntimeError):
    """A persona could not be signed in, so its scenes cannot be filmed."""


def personas_in_spec(spec: dict) -> list[str]:
    """Personas any scene actually asks for, in first-appearance order.

    Only these are signed in — a spec's ``auth.personas`` may describe more seats
    than a given narrative visits, and each login costs a browser context.
    """
    seen: list[str] = []
    for scene in spec.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        persona = (scene.get("persona") or "").strip()
        if persona and persona not in seen:
            seen.append(persona)
    return seen


def _password_for(persona: str, auth: dict) -> str:
    """Resolve this persona's password from the environment.

    Per-persona ``password_env`` wins over the shared one, so a spec can mix a
    common demo password with one account that has its own.
    """
    per = (auth.get("password_envs") or {}).get(persona)
    var = per or auth.get("password_env")
    if not var:
        raise IdentityError(
            f"auth.password_env is not set, so there is no way to sign in {persona!r}. "
            f"Name an environment variable holding the demo password."
        )
    value = os.environ.get(var)
    if not value:
        raise IdentityError(
            f"${var} is empty, so {persona!r} cannot be signed in. Export it "
            f"before rendering (it is deliberately not in the spec)."
        )
    return value


def mint_identities(
    browser: Any,
    spec: dict,
    base_url: str,
    *,
    personas: list[str] | None = None,
    timeout_ms: int = 30000,
) -> dict[str, list[dict]]:
    """Return ``{persona: cookies}``, signing each one in via the login form.

    Each persona gets its OWN throwaway context (no ``record_video``), so the
    logins leave no footage and no cookie bleed between seats. Raises
    :class:`IdentityError` if a persona does not end up authenticated — a render
    that silently filmed a logged-out page would look like a product bug and cost
    a whole judge cycle to diagnose.
    """
    auth = spec.get("auth") or {}
    if (auth.get("type") or "").strip() != "form":
        return {}

    wanted = personas if personas is not None else personas_in_spec(spec)
    mapping = auth.get("personas") or {}
    login_url = auth.get("login_url") or "/"
    fields = auth.get("fields") or {}
    email_sel = fields.get("email") or 'input[name="email"]'
    password_sel = fields.get("password") or 'input[name="password"]'
    submit_sel = auth.get("submit") or 'button[type="submit"]'
    success_sel = auth.get("success")
    root = (base_url or "").rstrip("/")

    identities: dict[str, list[dict]] = {}
    for persona in wanted:
        username = mapping.get(persona)
        if not username:
            # A label-only persona (the field predates this feature). Leave it
            # unmapped; the recorder treats an unmapped persona as "no switch".
            continue
        password = _password_for(persona, auth)
        context = browser.new_context()
        try:
            page = context.new_page()
            page.goto(f"{root}{login_url}", wait_until="networkidle", timeout=timeout_ms)
            page.fill(email_sel, username)
            page.fill(password_sel, password)
            page.click(submit_sel)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            if success_sel:
                try:
                    page.wait_for_selector(success_sel, timeout=timeout_ms)
                except Exception as exc:  # noqa: BLE001 — re-raised with context
                    raise IdentityError(
                        f"signed in as {persona} ({username}) but {success_sel!r} never "
                        f"appeared — the login probably failed and left the form up. "
                        f"Check the password in the environment."
                    ) from exc
            cookies = context.cookies()
            if not cookies:
                raise IdentityError(
                    f"login for {persona} ({username}) set no cookies, so there is no "
                    f"session to carry into the recording."
                )
            identities[persona] = cookies
            print(f"  · minted identity {persona} ({username}) off camera")
        finally:
            context.close()
    return identities
