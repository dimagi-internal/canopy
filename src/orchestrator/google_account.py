"""`canopy google-account` — give an agent a Google Workspace account, its password kept
only in the agent's canopy vault.

    canopy google-account create         --agent X --email x@dimagi-ai.com --name "X Agent"
    canopy google-account reset-password --agent X --email x@dimagi-ai.com

Both act as ONE Google service account, the *provisioner*, through the Admin SDK Directory
API — never as a person and never with domain-wide delegation. What it may do is fenced by
Google, not by this code: the service account holds a custom admin role (Admin API
privileges Users → Read, Create, Update) assigned for one organizational unit only
(``/Non-person/AI`` by default). A leaked provisioner key can therefore create and
re-password accounts in that OU and touch nothing else in the tenant. Licenses are out of
its reach too; seat assignment stays a person's job in the Admin console.

The password is generated here, sent to Google once over TLS, and written to the agent's
zero-knowledge vault — encrypted on this device (`vault_zk_cli.run_set`), or, for an agent
with no vault key yet, sealed to its readers by canopy-web's credentials write — as
``GOOGLE_ACCOUNT`` (the address) and ``GOOGLE_PASSWORD``. It is never printed, logged or
written to disk. Ordering keeps a password from ever being lost:

* ``create`` refuses an address that already exists, stores the password, then creates the
  user — a failed create leaves an unused value in the vault, overwritten by the retry.
* ``reset-password`` sets the password at Google, then stores it — if the store fails, the
  command says so and a rerun sets a fresh one.

The provisioner's key (its JSON key file) comes from, in order: ``--key-file``, the
``GOOGLE_PROVISIONER_KEY`` environment variable (the JSON itself), or the
``GOOGLE_PROVISIONER_KEY`` value in the ``--key-agent`` vault (default ``ada``, the fleet
conductor), read through `cred_resolve.resolve` like any agent credential.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import click

DEFAULT_OU = "/Non-person/AI"
KEY_REF = "GOOGLE_PROVISIONER_KEY"
ACCOUNT_REF = "GOOGLE_ACCOUNT"
PASSWORD_REF = "GOOGLE_PASSWORD"
SCOPE = "https://www.googleapis.com/auth/admin.directory.user"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERS_URL = "https://admin.googleapis.com/admin/directory/v1/users"

# (method, url, body or None, headers) -> (status, parsed JSON body)
Http = Callable[[str, str, Optional[bytes], dict], tuple]
# {name: value} -> one line saying what was stored
Store = Callable[[dict], str]


class GoogleAccountError(click.ClickException):
    pass


def _http(method: str, url: str, body: Optional[bytes], headers: dict) -> tuple:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, {"error": {"message": raw.decode("utf-8", "replace")[:300]}}


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def access_token(key: dict, *, http: Http = _http, now: Optional[float] = None) -> str:
    """An OAuth access token for the service account ITSELF (no ``sub``: no impersonation),
    from the JWT-bearer grant signed with its private key."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    for field in ("client_email", "private_key"):
        if not key.get(field):
            raise GoogleAccountError(f"provisioner key has no {field} — is it a service "
                                     "account JSON key?")
    iat = int(now if now is not None else time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps({"iss": key["client_email"], "scope": SCOPE,
                                 "aud": TOKEN_URL, "iat": iat, "exp": iat + 600}).encode())
    pk = serialization.load_pem_private_key(key["private_key"].encode(), password=None)
    sig = pk.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    form = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": f"{header}.{claims}.{_b64url(sig)}"}).encode()
    status, body = http("POST", TOKEN_URL, form,
                        {"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200 or not body.get("access_token"):
        raise GoogleAccountError(f"Google refused the provisioner key ({status}): "
                                 f"{_message(body)}")
    return body["access_token"]


def _message(body: dict) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(body.get("error_description") or err or body)


class Directory:
    """The three Directory API calls the provisioner's role allows."""

    def __init__(self, token: str, *, http: Http = _http):
        self.token, self.http = token, http

    def _call(self, method: str, url: str, payload: Optional[dict] = None) -> tuple:
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        return self.http(method, url, data, headers)

    def _user_url(self, email: str) -> str:
        return f"{USERS_URL}/{urllib.parse.quote(email)}"

    def get(self, email: str, *, unreadable_is_absent: bool = False) -> Optional[dict]:
        """The user, or None if no such user. Google answers an OU-fenced admin 403, not
        404, for an address that does not exist — and the same 403 for a user outside its
        OU, so the two cannot be told apart here. A 403 is an error unless the caller
        says otherwise (`create`, whose insert Google refuses for an address already
        taken anywhere in the tenant)."""
        status, body = self._call("GET", self._user_url(email))
        if status == 404 or (status == 403 and unreadable_is_absent):
            return None
        if status == 200:
            return body
        raise GoogleAccountError(f"cannot read {email} ({status}): {_message(body)} — "
                                 "if it exists outside the provisioner's OU this is expected")

    def insert(self, user: dict) -> dict:
        status, body = self._call("POST", USERS_URL, user)
        if status not in (200, 201):
            if status == 409:
                raise GoogleAccountError(f"{user['primaryEmail']} already exists outside "
                                         "the provisioner's OU — nothing was created")
            raise GoogleAccountError(f"Google did not create {user['primaryEmail']} "
                                     f"({status}): {_message(body)}")
        return body

    def set_password(self, email: str, password: str) -> None:
        status, body = self._call("PATCH", self._user_url(email),
                                  {"password": password, "changePasswordAtNextLogin": False})
        if status != 200:
            raise GoogleAccountError(f"Google did not set {email}'s password "
                                     f"({status}): {_message(body)}")


def new_password() -> str:
    return secrets.token_urlsafe(24)


def _in_ou(user: dict, ou: str) -> bool:
    path = str(user.get("orgUnitPath") or "")
    return path == ou or path.startswith(ou.rstrip("/") + "/")


def run_create(d: Directory, store: Store, *, email: str, given: str, family: str, ou: str,
               recovery_email: str = "", password: Callable[[], str] = new_password) -> list:
    if d.get(email, unreadable_is_absent=True) is not None:
        raise GoogleAccountError(f"{email} already exists — use `reset-password` to put a "
                                 "fresh password in the vault")
    pw = password()
    lines = [store({ACCOUNT_REF: email, PASSWORD_REF: pw})]
    user = {"primaryEmail": email, "name": {"givenName": given, "familyName": family},
            "password": pw, "changePasswordAtNextLogin": False, "orgUnitPath": ou}
    if recovery_email:
        user["recoveryEmail"] = recovery_email
    made = d.insert(user)
    lines.append(f"created {made.get('primaryEmail', email)} in "
                 f"{made.get('orgUnitPath', ou)}")
    return lines


def run_reset(d: Directory, store: Store, *, email: str, ou: str,
              password: Callable[[], str] = new_password) -> list:
    user = d.get(email)
    if user is None:
        raise GoogleAccountError(f"{email} does not exist — use `create`")
    if not _in_ou(user, ou):
        raise GoogleAccountError(f"{email} is in {user.get('orgUnitPath')}, not {ou} — "
                                 "refusing to re-password it")
    pw = password()
    d.set_password(email, pw)
    try:
        stored = store({ACCOUNT_REF: email, PASSWORD_REF: pw})
    except Exception as e:
        raise GoogleAccountError(f"{email}'s password was CHANGED but storing it failed "
                                 f"({e}) — fix the vault and rerun reset-password") from e
    return [f"set a new password for {email}", stored]


# ──────────────────────────────────────────────────────────────────────────────
# wiring: where the key comes from, where the password goes
# ──────────────────────────────────────────────────────────────────────────────

def load_key(key_file: str, key_agent: str, *, env=os.environ, resolve=None) -> dict:
    if key_file:
        raw = Path(key_file).expanduser().read_text(encoding="utf-8")
    elif env.get(KEY_REF):
        raw = env[KEY_REF]
    else:
        if resolve is None:
            from orchestrator.cred_resolve import resolve
            from orchestrator.vault_cli import _token
            raw = resolve(key_agent, token=_token(), refs=[KEY_REF],
                          purpose="google-account").get("values", {}).get(KEY_REF)
        else:
            raw = resolve(key_agent, [KEY_REF])
        if not raw:
            raise GoogleAccountError(
                f"no provisioner key: pass --key-file, set ${KEY_REF}, or store it in "
                f"{key_agent}'s vault (`canopy vault zk set --agent {key_agent} {KEY_REF} "
                f"< key.json`) and declare {KEY_REF} in {key_agent}'s .env.tpl")
    try:
        return json.loads(raw)
    except ValueError:
        raise GoogleAccountError("the provisioner key is not JSON") from None


def vault_store(slug: str, vault=None) -> Store:
    """Store into the agent's zero-knowledge vault. Checked up front so a vault this
    device cannot write to stops the command before Google is touched.

    An agent born zero-knowledge has no vault key yet (kv 0) — every new agent until its
    first machine is approved. `run_set` then writes through canopy-web, which seals each
    value to the agent's readers as it arrives; nobody sets the vault up by hand first.
    With a vault key, this device must hold its copy, and the values are encrypted here."""
    from orchestrator import vault_zk_cli as zc

    v = vault or zc._vault(slug)
    try:
        state = v.state()
    except zc.ZkCliError as e:
        raise GoogleAccountError(f"cannot write {slug}'s vault: {e}") from None
    if not state.get("enabled"):
        raise GoogleAccountError(f"'{slug}' is not zero-knowledge — `canopy vault zk "
                                 f"enable --agent {slug}` first")
    if state.get("kv") == 0:
        return lambda values: zc.render(zc.run_set(v, values))
    try:
        v.my_wrap()
    except zc.ZkCliError as e:
        raise GoogleAccountError(f"this device cannot write {slug}'s vault: {e}") from None

    def store(values: dict) -> str:
        return zc.render(zc.run_set(v, values))
    return store


def _split_name(name: str, slug: str) -> tuple:
    parts = (name or slug.capitalize()).split()
    return parts[0], " ".join(parts[1:]) or "Agent"


AGENT_OPT = click.option("--agent", "slug", required=True,
                         help="The agent whose vault receives the password.")
EMAIL_OPT = click.option("--email", required=True, help="The account's address.")
OU_OPT = click.option("--ou", default=DEFAULT_OU, show_default=True,
                      help="The organizational unit the provisioner is fenced to.")
KEY_FILE_OPT = click.option("--key-file", default="", metavar="PATH",
                            help=f"The provisioner's JSON key (else ${KEY_REF}, else the "
                                 "--key-agent vault).")
KEY_AGENT_OPT = click.option("--key-agent", default="ada", show_default=True,
                             help=f"Whose vault holds {KEY_REF}.")


@click.group("google-account")
def google_account_group():
    """An agent's Google Workspace account, created or re-passworded by the OU-fenced
    provisioner service account; the password goes only into the agent's canopy vault."""


@google_account_group.command("create")
@AGENT_OPT
@EMAIL_OPT
@click.option("--name", default="", help='Display name, e.g. "Ada Agent" (default: the slug).')
@click.option("--recovery-email", default="",
              help="Where Google's account notices go (e.g. the agent's canopy mail address).")
@OU_OPT
@KEY_FILE_OPT
@KEY_AGENT_OPT
def create_cmd(slug, email, name, recovery_email, ou, key_file, key_agent):
    """Create the account in the OU and store its password in the agent's vault."""
    store = vault_store(slug)
    d = Directory(access_token(load_key(key_file, key_agent)))
    given, family = _split_name(name, slug)
    for line in run_create(d, store, email=email, given=given, family=family, ou=ou,
                           recovery_email=recovery_email):
        click.echo(line)
    click.echo("license: none assigned by this command — check it in the Admin console")


@google_account_group.command("reset-password")
@AGENT_OPT
@EMAIL_OPT
@OU_OPT
@KEY_FILE_OPT
@KEY_AGENT_OPT
def reset_cmd(slug, email, ou, key_file, key_agent):
    """Give an existing account in the OU a fresh password, stored in the agent's vault."""
    store = vault_store(slug)
    d = Directory(access_token(load_key(key_file, key_agent)))
    for line in run_reset(d, store, email=email, ou=ou):
        click.echo(line)
