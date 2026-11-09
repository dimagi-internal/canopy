"""Every route canopy calls on canopy-web exists there (src/orchestrator/canopy_web_routes.py).

Why: canopy's canopy-web calls are method + path strings in ~60 modules, and every unit
test drives them through a fake transport, so a route canopy-web removed would keep passing
here until a real call 404'd (for example a CLI PATCHing a removed
`/api/agents/{slug}/device-binding`, or a listing from a removed `/api/agent-runs/projects/`).

The route table is vendored (`tests/fixtures/canopy_web_routes_schema.json`, trimmed from
canopy-web's OpenAPI document). Refresh it from a canopy-web checkout on origin/main:

    uv run python -m orchestrator.canopy_web_routes refresh-schema --checkout <canopy-web>

canopy-web holds the other half: it vendors `... canopy_web_routes routes` and fails a
PR that removes a route on this list.
"""
from __future__ import annotations

import json
from collections import Counter

import pytest

from orchestrator import canopy_web_routes as R

ALLOWED_MISSING = R.ALLOWED_MISSING

# A call whose path canopy cannot read statically (a URL canopy-web handed back, a path
# kept on an object, a presigned upload). Per file, shrink-only: a new one should be
# written so the path is visible (a module constant or a small path helper), or counted
# here with a reason in the PR.
UNRESOLVED_CEILING = {
    "src/orchestrator/act_as_cli.py": 1,
    "src/orchestrator/connections/access.py": 7,
    "src/orchestrator/connections/keep.py": 2,
    "src/orchestrator/cred_resolve.py": 1,
    "src/orchestrator/google_account.py": 2,   # Google's Admin API, not canopy-web
    "src/orchestrator/kb_cli.py": 2,
    "src/orchestrator/kb_remote.py": 3,        # presigned S3 URLs canopy-web returns
    "src/orchestrator/project_history.py": 1,
    "src/orchestrator/secret_cli.py": 1,
    "src/orchestrator/vault_keeper.py": 3,     # paths in the keeper's work items
    "src/orchestrator/vault_zk_cli.py": 8,
    "src/orchestrator/workspace_cli.py": 1,
}


@pytest.fixture(scope="module")
def extracted():
    return R.extract()


@pytest.fixture(scope="module")
def routes():
    return R.schema_routes(json.loads(R.SCHEMA_FIXTURE.read_text(encoding="utf-8")))


def _fmt(calls):
    return "\n".join(f"  {c['method']} {c['path']}  ({', '.join(c['where'])})" for c in calls)


def test_every_canopy_web_call_is_a_route_canopy_web_serves(extracted, routes):
    gone = [c for c in R.missing(extracted["calls"], routes)
            if (c["method"], c["path"]) not in ALLOWED_MISSING]
    assert not gone, (
        "canopy calls canopy-web routes its schema does not serve — repoint or remove them "
        "(or, if canopy-web just added the route, refresh the vendored schema):\n" + _fmt(gone))


def test_the_allowlist_holds_only_calls_that_are_still_made_and_still_missing(extracted, routes):
    still_missing = {(c["method"], c["path"]) for c in R.missing(extracted["calls"], routes)}
    stale = sorted(set(ALLOWED_MISSING) - still_missing)
    assert not stale, f"drop these from ALLOWED_MISSING — fixed or no longer called: {stale}"


def test_unresolved_calls_do_not_grow(extracted):
    got = Counter(u["where"].rsplit(":", 1)[0] for u in extracted["unresolved"])
    grew = {f: (n, UNRESOLVED_CEILING.get(f, 0)) for f, n in got.items()
            if n > UNRESOLVED_CEILING.get(f, 0)}
    assert not grew, (
        "canopy-web calls whose path cannot be read statically, so no route check covers "
        f"them (file: (now, ceiling)): {grew}\n"
        + "\n".join(f"  {u['method']} {u['expr']}  ({u['where']})" for u in extracted["unresolved"]
                    if u["where"].rsplit(":", 1)[0] in grew))
    shrank = {f: (got.get(f, 0), n) for f, n in UNRESOLVED_CEILING.items() if got.get(f, 0) < n}
    assert not shrank, f"lower UNRESOLVED_CEILING to match (file: (now, ceiling)): {shrank}"


def test_the_extractor_sees_canopys_calls(extracted):
    """A floor, so a broken extractor cannot pass the route check by seeing nothing."""
    got = {(c["method"], c["path"]) for c in extracted["calls"]}
    assert len(got) >= 150
    for known in [("POST", "/api/harness/turns/{}/abort"),          # f-string
                  ("GET", "/api/me/"),                               # imported constant
                  ("GET", "/api/slack-config/{}"),                   # inlined path helper
                  ("POST", "/api/checkins/runs/{}/finished"),        # constant + f-string
                  ("GET", "/api/projects/")]:                        # scripts/ddd
        assert known in got, known


def test_the_vendored_schema_is_canopy_webs_route_table(routes):
    assert len(routes) > 500 and ("GET", "/api/me/") in routes


# ── the matcher ───────────────────────────────────────────────────────────────

SERVED = {("GET", "/api/agents/{}/"), ("GET", "/api/agent-runs/{}/"),
          ("POST", "/api/agents/{}/projects/"), ("GET", "/api/agents/")}


@pytest.mark.parametrize("method,path,served", [
    ("GET", "/api/agents/{}/", True),
    ("GET", "/api/{}/", True),                       # a runtime segment matches a literal
    ("GET", "/api/agent-runs/projects/", False),     # a literal is not a route parameter
    ("GET", "/api/agents/{}", True),                 # GET follows APPEND_SLASH's 301
    ("POST", "/api/agents/{}/projects", False),      # a POST does not
    ("PATCH", "/api/agents/{}/", False),             # wrong method
])
def test_missing_matches_by_segment(method, path, served):
    got = R.missing([{"method": method, "path": path, "where": []}], SERVED)
    assert (got == []) is served


@pytest.mark.parametrize("raw,want", [
    ("/api/w/{ws}/harness/runners/?x=1", "/api/harness/runners/"),
    ("/api/agents/{slug}/projects/{ref}/", "/api/agents/{}/projects/{}/"),
    ("/api/harness/runners//private", "/api/harness/runners/{}/private"),
])
def test_normalise(raw, want):
    assert R.normalise(raw) == want


def test_extract_reads_constants_fstrings_locals_and_helpers(tmp_path):
    pkg = tmp_path / "src" / "orchestrator"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "paths.py").write_text('BASE = "/api/things/"\n')
    (pkg / "caller.py").write_text(
        "from orchestrator.paths import BASE\n"
        "from orchestrator import canopy_web\n"
        "from urllib.parse import quote\n"
        "def _cfg(ws):\n"
        "    return f'/api/config/{ws}'\n"
        "def run(slug, ws, q):\n"
        "    canopy_web.call('GET', f'{BASE}{quote(slug)}/x?y={q}')\n"
        "    path = f'/api/w/{ws}/items/' if ws else '/api/items/'\n"
        "    canopy_web.call('POST', path, {})\n"
        "    canopy_web.call('DELETE', _cfg(ws) + '/allowed')\n"
        "    canopy_web.call('PUT', some_url_from_the_server, {})\n"
        "    other.call('GET', 'https://www.googleapis.com/x')\n")
    got = R.extract(tmp_path)
    assert [(c["method"], c["path"]) for c in got["calls"]] == [
        ("DELETE", "/api/config/{}/allowed"), ("POST", "/api/items/"),
        ("GET", "/api/things/{}/x")]
    assert [u["expr"] for u in got["unresolved"]] == ["some_url_from_the_server"]
