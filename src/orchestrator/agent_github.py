"""The GitHub identity an agent's session acts as — the laptop twin of the cloud
runner's per-turn GitHub env (canopy#832).

canopy-web holds each agent's GitHub credential (`AgentDelegation`, set at
/agents/<slug>/settings → Credentials → GitHub). The cloud runner asks for it per
turn and puts it in that turn's environment (`cloud_runner.github_env`). A laptop
/ emdash session got nothing, so its git and gh fell through to whatever the
machine had — the owner's `gh auth` login — and an agent's PRs were attributed to
the human with the human's rights (ace#2825).

This module resolves the agent's credential from the same custodian
(`/credentials/resolve`, operator-authorized: the route the runner itself uses,
gated to whoever pairs a live runner this agent routes to), asks GitHub who it
acts as, and builds the environment that makes git (author, committer, credential
helper) and gh (`GH_TOKEN`) act as it. The SessionStart hook `agent_op_env.py`
writes that environment into the session; with no credential the hook writes a
refusal instead, never a fallback.

Whose identity the credential IS is canopy-web's business, not this module's: an
owner's fine-grained token (the default) or a dedicated machine user's — this code
reads whatever is configured and says who it is.

FRAMEWORK tier: stdlib + `canopy_web` only.
"""
from __future__ import annotations

import json
import shlex
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from orchestrator import canopy_web

GITHUB_API = "https://api.github.com"

#: The credential helper a session's git uses. It reads GH_TOKEN from the
#: environment at the moment git asks, so it holds nothing itself — the same
#: helper the cloud runner installs (`cloud_runner._GIT_HELPER`).
GIT_HELPER = ('!f() { test "$1" = get || exit 0; echo username=x-access-token; '
              'echo "password=$GH_TOKEN"; }; f')
_HELPER_KEY = "credential.https://github.com.helper"


class GitHubIdentityError(RuntimeError):
    """This agent has no usable GitHub identity; the message says why and who fixes it."""


@dataclass(frozen=True)
class GitHubIdentity:
    slug: str
    token: str
    login: str
    name: str
    user_id: int

    @property
    def email(self) -> str:
        """GitHub's noreply address: links a commit to the account without
        publishing anyone's email (what the cloud runner commits with too)."""
        return f"{self.user_id}+{self.login}@users.noreply.github.com"

    def describe(self) -> str:
        return f"@{self.login} ({self.name} <{self.email}>)"


def _settings_hint(slug: str) -> str:
    return f"/agents/{slug}/settings → Credentials → GitHub on canopy-web"


def fetch_token(slug: str, *, call: Callable = canopy_web.call,
                token: Optional[str] = None) -> str:
    """The agent's GitHub credential from canopy-web. Raises GitHubIdentityError."""
    if token is None:
        from orchestrator.agent_bootstrap import _operator_token
        token = _operator_token()
    try:
        body = call("GET", f"/api/agents/{slug}/credentials/resolve", token=token) or {}
    except Exception as e:  # noqa: BLE001 — any transport/auth failure is a refusal reason
        raise GitHubIdentityError(
            f"could not read {slug}'s GitHub credential from canopy-web: "
            f"{str(e).splitlines()[0][:200] if str(e) else type(e).__name__}") from e
    gh = str(body.get("github_token") or "").strip()
    if not gh:
        raise GitHubIdentityError(
            f"canopy-web holds no GitHub credential for {slug} — an admin sets one at "
            f"{_settings_hint(slug)}")
    return gh


def _github_user(gh_token: str) -> dict:
    req = urllib.request.Request(f"{GITHUB_API}/user", headers={
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — fixed https URL
        return json.loads(resp.read().decode("utf-8"))


def resolve_identity(slug: str, *, call: Callable = canopy_web.call,
                     token: Optional[str] = None,
                     github_user: Callable[[str], dict] = _github_user) -> GitHubIdentity:
    """Who `slug`'s sessions act as on GitHub. Raises GitHubIdentityError when the
    credential is missing, rejected (expired / revoked), or GitHub cannot be asked."""
    gh = fetch_token(slug, call=call, token=token)
    try:
        user = github_user(gh) or {}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise GitHubIdentityError(
                f"GitHub rejected {slug}'s credential (401 — expired or revoked); an admin "
                f"replaces it at {_settings_hint(slug)}") from e
        raise GitHubIdentityError(f"GitHub answered {e.code} for /user") from e
    except Exception as e:  # noqa: BLE001
        raise GitHubIdentityError(f"could not ask GitHub who {slug}'s credential is: {e}") from e
    login = str(user.get("login") or "")
    if not login:
        raise GitHubIdentityError(f"GitHub returned no login for {slug}'s credential")
    return GitHubIdentity(slug=slug, token=gh, login=login,
                          name=str(user.get("name") or login),
                          user_id=int(user.get("id") or 0))


def git_config_parameters(pairs: list[tuple[str, str]]) -> str:
    """`GIT_CONFIG_PARAMETERS` for `pairs`, in git's own quoting (`'key'='value'`).

    This variable — not GIT_CONFIG_COUNT — because it is the one emdash sets to
    install ITS helper (the machine's gh login); replacing it is what keeps that
    helper out of an agent session."""
    def sq(s: str) -> str:
        return "'" + s.replace("'", "'\\''") + "'"
    return " ".join(f"{sq(k)}={sq(v)}" for k, v in pairs)


def session_env(identity: GitHubIdentity) -> dict:
    """The environment that makes git and gh act as `identity` — the shape of
    `cloud_runner.github_env`. The empty first helper clears every helper
    configured anywhere else (osxkeychain, `gh auth git-credential`, emdash's)."""
    return {
        "GH_TOKEN": identity.token,
        "GITHUB_TOKEN": identity.token,
        "GIT_CONFIG_PARAMETERS": git_config_parameters(
            [(_HELPER_KEY, ""), (_HELPER_KEY, GIT_HELPER)]),
        "GIT_AUTHOR_NAME": identity.name,
        "GIT_AUTHOR_EMAIL": identity.email,
        "GIT_COMMITTER_NAME": identity.name,
        "GIT_COMMITTER_EMAIL": identity.email,
        "CANOPY_AGENT_GITHUB": identity.login,
    }


def export_lines(env: dict) -> str:
    """`export K=V` lines for `$CLAUDE_ENV_FILE`, which Claude Code sources before
    every Bash command of the session."""
    return "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in env.items())
