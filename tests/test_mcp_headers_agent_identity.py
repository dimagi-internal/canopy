"""The canopy-web MCP headers helper resolves identity the way the canopy CLI does
(`canopy_web.resolve_token`): CANOPY_WEB_PAT, then the agent's own PAT from
`~/.<slug>/.env`, and only then the operator's workbench-token file.

It used to read only the file. On a headless cloud runner that file does not
exist by design, so no header was sent, canopy-web 401'd, and Claude Code fell
into OAuth discovery at the ORIGIN — where connect-labs, sharing the domain,
answered with its own resource metadata: "Protected resource
https://labs.connect.dimagi.com/mcp/ does not match expected". Every hal session
on cloud-ec2-1 had no canopy-web MCP (2026-09-22). On a laptop the same gap was
silent in the other direction: every agent's MCP calls ran as the OPERATOR.
"""
import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "plugins/canopy/scripts/canopy-web-mcp-headers.js"
OPERATOR = "canopy_pat_OPERATOR"
HAL = "canopy_pat_HAL"


def _run(home, cwd, env=None):
    e = {"PATH": os.environ["PATH"], "HOME": str(home), **(env or {})}
    r = subprocess.run(["node", str(SCRIPT)], cwd=str(cwd), env=e, capture_output=True,
                       text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout).get("Authorization", "")


def _home(tmp_path, *, operator=True, hal_env=None):
    home = tmp_path / "home"
    (home / ".claude" / "canopy").mkdir(parents=True)
    if operator:
        (home / ".claude" / "canopy" / "workbench-token").write_text(OPERATOR)
    if hal_env is not None:
        (home / ".hal").mkdir()
        (home / ".hal" / ".env").write_text(hal_env)
    return home


def _hal_repo(tmp_path):
    repo = tmp_path / "agents" / "hal"
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "hal"}))
    (repo / "skills" / "turn").mkdir(parents=True)
    return repo


def test_the_cloud_box_case_agent_env_with_no_operator_file(tmp_path):
    home = _home(tmp_path, operator=False, hal_env=f"GDRIVE_ROOT_FOLDER=x\nCANOPY_WEB_PAT={HAL}\n")
    assert _run(home, _hal_repo(tmp_path)) == f"Bearer {HAL}"


def test_the_agent_acts_as_itself_even_where_the_operator_file_exists(tmp_path):
    home = _home(tmp_path, hal_env=f'CANOPY_WEB_PAT="{HAL}"\n')
    repo = _hal_repo(tmp_path)
    assert _run(home, repo) == f"Bearer {HAL}"
    assert _run(home, repo / "skills" / "turn") == f"Bearer {HAL}"   # from a subdirectory


def test_the_env_var_wins(tmp_path):
    home = _home(tmp_path, hal_env=f"CANOPY_WEB_PAT={HAL}\n")
    assert _run(home, _hal_repo(tmp_path), {"CANOPY_WEB_PAT": "canopy_pat_PINNED"}) == "Bearer canopy_pat_PINNED"


def test_an_agent_repo_with_no_pat_falls_back_to_the_operator(tmp_path):
    home = _home(tmp_path, hal_env="GDRIVE_ROOT_FOLDER=x\n")
    assert _run(home, _hal_repo(tmp_path)) == f"Bearer {OPERATOR}"


def test_outside_any_agent_repo_it_is_the_operator(tmp_path):
    home = _home(tmp_path, hal_env=f"CANOPY_WEB_PAT={HAL}\n")
    plain = tmp_path / "somewhere"
    plain.mkdir()
    assert _run(home, plain) == f"Bearer {OPERATOR}"


def test_a_confined_session_never_gets_the_agents_pat_either(tmp_path):
    # Confinement outranks every PAT, the agent's included: a caller's session
    # reaching canopy as the agent would be the owner's full profile by another name.
    home = _home(tmp_path, hal_env=f"CANOPY_WEB_PAT={HAL}\n")
    (home / ".canopy" / "profiles").mkdir(parents=True)
    got = _run(home, _hal_repo(tmp_path), {"CANOPY_WEB_PAT": HAL,
                                           "CANOPY_PROFILE": str(tmp_path / "missing.json")})
    assert HAL not in got and got.startswith("Bearer cct_missing")


def test_canopy_agent_names_the_agent_where_cwd_and_the_pat_env_cannot(tmp_path):
    # Exactly how Claude Code runs the helper: from the PLUGIN's directory, with
    # CANOPY_WEB_PAT stripped. Only CANOPY_AGENT (not a secret) gets through.
    home = _home(tmp_path, operator=False, hal_env=f"CANOPY_WEB_PAT={HAL}\n")
    plugin_root = tmp_path / "plugins" / "cache" / "canopy" / "0.2.513"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "canopy"}))
    assert _run(home, plugin_root, {"CANOPY_AGENT": "hal"}) == f"Bearer {HAL}"


def test_canopy_agent_outranks_the_operator_file(tmp_path):
    home = _home(tmp_path, hal_env=f"CANOPY_WEB_PAT={HAL}\n")
    assert _run(home, tmp_path, {"CANOPY_AGENT": "hal"}) == f"Bearer {HAL}"


def test_a_malformed_canopy_agent_reads_no_file(tmp_path):
    home = _home(tmp_path)
    assert _run(home, tmp_path, {"CANOPY_AGENT": "../.claude/canopy"}) == f"Bearer {OPERATOR}"


def test_confinement_outranks_canopy_agent(tmp_path):
    home = _home(tmp_path, hal_env=f"CANOPY_WEB_PAT={HAL}\n")
    got = _run(home, tmp_path, {"CANOPY_AGENT": "hal", "CANOPY_PROFILE": str(tmp_path / "none.json")})
    assert HAL not in got and got.startswith("Bearer cct_missing")
