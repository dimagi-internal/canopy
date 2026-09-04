"""Tests for the OpenClaw harvester (inventory / compare / bootstrap / reconcile)."""
import pytest

from orchestrator.agent_factory import AgentSpec, create_agent
from orchestrator.openclaw_harvest import (
    HarvestError,
    bootstrap_from_snapshot,
    compare,
    inventory_snapshot,
    port_new_skills,
)


def _fake_openclaw(tmp_path):
    """A minimal OpenClaw workspace snapshot on disk."""
    d = tmp_path / "snap"
    (d / "skills" / "outreach").mkdir(parents=True)
    (d / "skills" / "weekly-digest").mkdir(parents=True)
    (d / "memory").mkdir(parents=True)
    (d / "SOUL.md").write_text("# Soul\nEva is warm, concise, relentless.\n")
    (d / "IDENTITY.md").write_text("# Identity\nEva — partner outreach agent.\n")
    # OpenClaw skill WITHOUT canopy frontmatter (freeform) — name falls back to dir, desc to heading
    (d / "skills" / "outreach" / "SKILL.md").write_text("# Outreach\nDraft warm intros to partners.\n")
    # OpenClaw skill WITH canopy-style frontmatter
    (d / "skills" / "weekly-digest" / "SKILL.md").write_text(
        "---\nname: weekly-digest\ndescription: Summarize the week for stakeholders.\n---\n# Body\n"
    )
    # bundled asset alongside a skill — must be ported too, not just SKILL.md
    (d / "skills" / "outreach" / "templates").mkdir()
    (d / "skills" / "outreach" / "templates" / "intro.md").write_text("Hi {{name}},\n")
    (d / "memory" / "partner-acme.md").write_text("ACME prefers Tuesday calls.\n")
    return d


def test_inventory_parses_persona_skills_memory(tmp_path):
    inv = inventory_snapshot(_fake_openclaw(tmp_path))
    assert inv["has_persona"]
    assert "SOUL.md" in inv["persona"] and "IDENTITY.md" in inv["persona"]
    keys = {s["key"] for s in inv["skills"]}
    assert keys == {"outreach", "weekly-digest"}
    # freeform skill: description falls back to the markdown heading
    outreach = next(s for s in inv["skills"] if s["key"] == "outreach")
    assert "Outreach" in outreach["description"]
    # frontmatter skill: description from frontmatter
    wd = next(s for s in inv["skills"] if s["key"] == "weekly-digest")
    assert "Summarize the week" in wd["description"]
    assert len(inv["memory"]) == 1


def test_inventory_missing_dir_raises(tmp_path):
    with pytest.raises(HarvestError):
        inventory_snapshot(tmp_path / "nope")


def test_compare_no_repo_recommends_bootstrap(tmp_path):
    inv = inventory_snapshot(_fake_openclaw(tmp_path))
    result = compare(inv, None)
    assert result["recommendation"] == "bootstrap"
    assert not result["repo_exists"]
    assert set(result["only_in_openclaw"]) == {"outreach", "weekly-digest"}


def test_compare_existing_repo_finds_novel_skills(tmp_path):
    inv = inventory_snapshot(_fake_openclaw(tmp_path))
    repo = tmp_path / "eva"
    create_agent(AgentSpec(slug="eva", display_name="Eva", mandate="x."), repo)
    # repo has turn + self-review; OpenClaw adds outreach + weekly-digest
    result = compare(inv, repo)
    assert result["recommendation"] == "reconcile"
    assert set(result["only_in_openclaw"]) == {"outreach", "weekly-digest"}
    assert "turn" in result["only_in_repo"]


def test_compare_up_to_date_when_repo_has_all(tmp_path):
    snap = _fake_openclaw(tmp_path)
    repo = tmp_path / "eva"
    create_agent(AgentSpec(slug="eva", display_name="Eva", mandate="x."), repo)
    port_new_skills(inventory_snapshot(snap), repo)        # port everything in
    result = compare(inventory_snapshot(snap), repo)
    assert result["recommendation"] != "bootstrap"
    assert result["only_in_openclaw"] == []


def test_bootstrap_seeds_persona_and_ports_skills(tmp_path):
    inv = inventory_snapshot(_fake_openclaw(tmp_path))
    out = bootstrap_from_snapshot(
        inv, slug="eva", display_name="Eva", mandate="partner outreach.", into=tmp_path / "eva-new",
    )
    repo = tmp_path / "eva-new"
    assert set(out["ported_skills"]) == {"outreach", "weekly-digest"}
    assert (repo / "skills" / "outreach" / "SKILL.md").exists()
    # bundled assets are ported, not just SKILL.md
    assert (repo / "skills" / "outreach" / "templates" / "intro.md").exists()
    # factory skills survive (not clobbered)
    assert (repo / "skills" / "turn" / "SKILL.md").exists()
    # persona seeded with the OpenClaw soul/identity
    persona = (repo / "persona.md").read_text()
    assert "Ported from the OpenClaw" in persona and "relentless" in persona


def test_port_new_skills_never_clobbers(tmp_path):
    inv = inventory_snapshot(_fake_openclaw(tmp_path))
    repo = tmp_path / "eva"
    create_agent(AgentSpec(slug="eva", display_name="Eva", mandate="x."), repo)
    # pre-existing 'outreach' with custom content must NOT be overwritten
    (repo / "skills" / "outreach").mkdir(parents=True)
    (repo / "skills" / "outreach" / "SKILL.md").write_text("CUSTOM — keep me\n")
    ported = port_new_skills(inv, repo)
    assert "outreach" not in ported and "weekly-digest" in ported
    assert (repo / "skills" / "outreach" / "SKILL.md").read_text() == "CUSTOM — keep me\n"


# ── bootstrap fidelity: what an OpenClaw actually loses in the crossing ──────────────────

def _rich_openclaw(tmp_path):
    """A snapshot with the files bootstrap used to drop on the floor."""
    d = _fake_openclaw(tmp_path)
    (d / "USER.md").write_text("# User\nWorks for Jonathan. Never DM anyone but him.\n")
    (d / "TOOLS.md").write_text("# Tools\nslack, gcal\n")
    (d / "HEARTBEAT.md").write_text("# Heartbeat\nevery 15m\n")
    (d / "memory" / "dm-workflow-notes.md").write_text("Slack user ID is U01234ABCDE.\n")
    (d / "memory" / "2026-07-06.md").write_text("Scheduled DM failed: channel_not_found.\n")
    (d / "workspace-state.json").write_text('{"last_run": "2026-07-06"}\n')
    return d


def _spec_args():
    return dict(slug="fizzy", display_name="Fizzy", mandate="be useful", mailbox="fizzy@dimagi-ai.com")


def test_bootstrap_carries_memory_and_user_md(tmp_path):
    """The whole reason to pick bootstrap over create-agent is that it preserves the agent.

    It used to fold SOUL.md and IDENTITY.md into persona.md and silently drop everything
    else — USER.md (who the agent works for) and the entire memory/ directory — while
    reporting persona_seeded: true. A real harvest lost four months of daily memory and the
    agent's hard rule about who it may message; one dropped memory file held the fix to a
    blocker the same agent had logged three times.
    """
    inv = inventory_snapshot(_rich_openclaw(tmp_path))
    out = bootstrap_from_snapshot(inv, into=tmp_path / "repo", **_spec_args())
    repo = tmp_path / "repo"

    persona = (repo / "persona.md").read_text(encoding="utf-8")
    assert "Never DM anyone but him" in persona, "USER.md did not reach persona.md"

    assert (repo / "memory" / "dm-workflow-notes.md").is_file()
    assert (repo / "memory" / "2026-07-06.md").is_file()
    assert set(out["carried"]["memory"]) >= {"dm-workflow-notes.md", "2026-07-06.md", "partner-acme.md"}

    # Reference text lands under openclaw/ — readable, but not passed off as ours.
    assert (repo / "openclaw" / "TOOLS.md").is_file()
    assert "TOOLS.md" in out["carried"]["workspace_text"]


def test_bootstrap_git_inits_like_create_agent(tmp_path):
    """create-agent inits a repo; bootstrap called create_agent() directly and skipped it,
    so the harvest route handed you a directory that was not a repository — while the docs
    promised 'an initialised git repo with one commit'."""
    inv = inventory_snapshot(_rich_openclaw(tmp_path))
    out = bootstrap_from_snapshot(inv, into=tmp_path / "repo", **_spec_args())
    assert out["git_initialized"] is True
    assert (tmp_path / "repo" / ".git").is_dir()


def test_bootstrap_can_skip_git_init(tmp_path):
    inv = inventory_snapshot(_rich_openclaw(tmp_path))
    out = bootstrap_from_snapshot(inv, into=tmp_path / "repo", git_init=False, **_spec_args())
    assert out["git_initialized"] is False
    assert not (tmp_path / "repo" / ".git").exists()


def test_bootstrap_accepts_stakeholders(tmp_path):
    inv = inventory_snapshot(_rich_openclaw(tmp_path))
    bootstrap_from_snapshot(
        inv, into=tmp_path / "repo", stakeholders="Jonathan (CEO)", **_spec_args()
    )
    body = (tmp_path / "repo" / "persona.md").read_text(encoding="utf-8")
    assert "Jonathan (CEO)" in body


def test_secret_scan_catches_a_token_inside_a_file(tmp_path):
    """SECRET_EXCLUDES is filename-shaped only. It matched ZERO files on a real harvest,
    which reads as 'clean' and actually means 'not checked' — a token pasted into a memory
    note has an innocent filename and sails straight into the first commit."""
    from orchestrator.openclaw_harvest import scan_snapshot_for_secrets

    d = _rich_openclaw(tmp_path)
    # Assembled at runtime, never written as a literal: a token-SHAPED string in a committed
    # test file is itself what GitHub push protection blocks (it cannot tell a fixture from a
    # live credential, and it is right not to try). Same reason the AWS id below is split.
    fake_slack = "xox" + "b-11112222333-abcdefghijklmnop"
    (d / "memory" / "notes.md").write_text(
        f"reminder, the slack app creds are {fake_slack}\n", encoding="utf-8"
    )
    findings = scan_snapshot_for_secrets(d)
    assert any(f["file"] == "memory/notes.md" for f in findings), findings


def test_bootstrap_holds_the_commit_when_a_secret_is_found(tmp_path):
    """A secret caught after `git commit` is already in history — so hold the commit."""
    from orchestrator.openclaw_harvest import scan_snapshot_for_secrets  # noqa: F401

    d = _rich_openclaw(tmp_path)
    fake_aws = "AKIA" + "IOSFODNN7EXAMPLE"          # split — see the note above
    (d / "memory" / "leak.md").write_text(
        f"AWS_ACCESS_KEY_ID={fake_aws}\n", encoding="utf-8"
    )
    inv = inventory_snapshot(d)
    out = bootstrap_from_snapshot(inv, into=tmp_path / "repo", **_spec_args())
    assert out["secret_findings"], "content scan found nothing"
    assert out["git_initialized"] is False
    assert not (tmp_path / "repo" / ".git").exists()


def test_secret_scan_is_quiet_on_a_clean_snapshot(tmp_path):
    """A scanner that cries wolf gets ignored, which is worse than not having one."""
    from orchestrator.openclaw_harvest import scan_snapshot_for_secrets

    assert scan_snapshot_for_secrets(_rich_openclaw(tmp_path)) == []


def test_snapshot_falls_back_to_ssh_tar_when_rsync_is_absent(tmp_path, monkeypatch):
    """rsync does not exist on Windows, so the documented harvest route was unreachable
    there — the module raised before doing anything. ssh and tar both ship with Windows 10+."""
    import io as _io
    import tarfile as _tarfile
    import subprocess as _sp
    from orchestrator import openclaw_harvest as oh

    payload = _io.BytesIO()
    with _tarfile.open(fileobj=payload, mode="w:gz") as tf:
        src = tmp_path / "SOUL.md"
        src.write_text("# Soul\n", encoding="utf-8")
        tf.add(src, arcname="SOUL.md")
    blob = payload.getvalue()

    monkeypatch.setattr(oh.shutil, "which", lambda name: None if name == "rsync" else f"/usr/bin/{name}")
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return _sp.CompletedProcess(cmd, 0, stdout=blob, stderr=b"")

    monkeypatch.setattr(oh.subprocess, "run", fake_run)
    pulled = oh.snapshot_via_ssh("root@host", tmp_path / "out")

    assert calls["cmd"][0] == "ssh"
    assert "tar cz" in calls["cmd"][-1]
    assert "--exclude=node_modules" in calls["cmd"][-1], "bulk excludes must be applied remotely"
    assert "--exclude=*.pem" in calls["cmd"][-1], "secrets must never cross the wire"
    assert pulled == ["SOUL.md"]


def test_ssh_tar_extraction_refuses_path_traversal(tmp_path, monkeypatch):
    """The droplet is explicitly assumed to be possibly compromised, so a `../` member name
    is exactly the thing to defend against — it would be arbitrary file write on the operator."""
    import io as _io
    import tarfile as _tarfile
    import subprocess as _sp
    from orchestrator import openclaw_harvest as oh

    evil = tmp_path / "evil.md"
    evil.write_text("pwned\n", encoding="utf-8")
    payload = _io.BytesIO()
    with _tarfile.open(fileobj=payload, mode="w:gz") as tf:
        tf.add(evil, arcname="../../escaped.md")
        tf.add(evil, arcname="fine.md")
    blob = payload.getvalue()

    monkeypatch.setattr(oh.shutil, "which", lambda name: None if name == "rsync" else f"/usr/bin/{name}")
    monkeypatch.setattr(
        oh.subprocess, "run",
        lambda cmd, **kw: _sp.CompletedProcess(cmd, 0, stdout=blob, stderr=b""),
    )
    out = tmp_path / "out"
    pulled = oh.snapshot_via_ssh("root@host", out)
    assert pulled == ["fine.md"]
    assert not (tmp_path / "escaped.md").exists()
    assert not (out.parent.parent / "escaped.md").exists()
