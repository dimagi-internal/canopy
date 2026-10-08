"""A project's Drive folder is a property of the project.

2026-10-08: a connect-labs session told to "write to Hal's project folder" found P1 with
drive_folder_id='' and P4 with an empty folder, asked the human which was meant, then ran
`canopy gdoc publish` outside an agent repo and got an AgentEmailError traceback. These pin
the three walls it hit: the folder is linked onto the project, `--project` resolves through
the project record, and `--agent` works from any directory with a one-line error otherwise.
"""
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from orchestrator import agent_email, agent_gdoc, project_folder
from orchestrator.agent_gdoc import GdocIdentity
from orchestrator.cli import main
from orchestrator.project_folder import (
    ensure_project_folder,
    find_project,
    resolve_project_destination,
)

FOLDER = "application/vnd.google-apps.folder"


class FakeDrive:
    """`gog drive ls|mkdir` over an in-memory {parent: [children]} tree."""

    def __init__(self, tree=None):
        self.tree = {k: list(v) for k, v in (tree or {}).items()}
        self.created = []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        parent = cmd[cmd.index("--parent") + 1]
        if cmd[2] == "ls":
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout=json.dumps({"files": self.tree.get(parent, [])}))
        fid = f"NEW{len(self.created) + 1}"
        self.created.append((parent, cmd[3]))
        self.tree.setdefault(parent, []).append({"id": fid, "name": cmd[3], "mimeType": FOLDER})
        return SimpleNamespace(returncode=0, stderr="",
                               stdout=json.dumps({"folder": {"id": fid}}))


class FakeClient:
    def __init__(self, projects=(), fail_list=False, fail_patch=False):
        self.projects = [dict(p) for p in projects]
        self.patches = []
        self.fail_list, self.fail_patch = fail_list, fail_patch

    def list_projects(self):
        if self.fail_list:
            raise RuntimeError("canopy-web down")
        return self.projects

    def patch_project(self, ref, **fields):
        if self.fail_patch:
            raise RuntimeError("403")
        self.patches.append((ref, fields))
        return {"ext_id": ref, **fields}


def _ident():
    return GdocIdentity(slug="hal", account="hal@dimagi-ai.com", client="canopy",
                        root_folder="ROOT")


def _drive_with_projects(**children):
    return FakeDrive({"ROOT": [{"id": "PROJ", "name": "Projects", "mimeType": FOLDER}],
                      "PROJ": [{"id": v, "name": k, "mimeType": FOLDER}
                               for k, v in children.items()]})


def _p(ext_id="P1", name="Supply tender review", fid="", url=""):
    return {"ext_id": ext_id, "name": name, "status": "active",
            "drive_folder_id": fid, "drive_folder_url": url}


# -- ensure_project_folder ------------------------------------------------------------

def test_a_project_with_no_folder_gets_one_created_and_linked():
    drive, client = _drive_with_projects(), FakeClient()

    got = ensure_project_folder(client, _ident(), _p(), runner=drive)

    assert drive.created == [("PROJ", "Supply tender review")]
    assert client.patches == [("P1", {
        "drive_folder_id": "NEW1",
        "drive_folder_url": "https://drive.google.com/drive/folders/NEW1"})]
    assert got["created"] and got["linked"] and got["folder_id"] == "NEW1"


def test_an_existing_same_named_folder_is_reused_not_duplicated():
    drive, client = _drive_with_projects(**{"Supply tender review": "OLD"}), FakeClient()

    got = ensure_project_folder(client, _ident(), _p(), runner=drive)

    assert drive.created == [] and got["folder_id"] == "OLD" and not got["created"]
    assert client.patches[0][1]["drive_folder_id"] == "OLD"


def test_a_linked_folder_wins_and_nothing_is_touched():
    drive, client = FakeDrive(), FakeClient()

    got = ensure_project_folder(client, _ident(), _p(fid="LINKED", url="https://x/LINKED"),
                                runner=drive)

    assert got["folder_id"] == "LINKED" and client.patches == [] and drive.created == []


def test_a_url_only_link_gets_its_id_filled_without_touching_drive():
    drive, client = FakeDrive(), FakeClient()
    url = "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345"

    got = ensure_project_folder(client, _ident(), _p(url=url), runner=drive)

    assert drive.created == []
    assert client.patches == [("P1", {"drive_folder_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345",
                                      "drive_folder_url": url})]
    assert got["linked"]


def test_dry_run_writes_nothing():
    drive, client = _drive_with_projects(), FakeClient()
    ensure_project_folder(client, _ident(), _p(), runner=drive, dry_run=True)
    assert drive.created == [] and client.patches == []


# -- --project resolves through the project record --------------------------------------

def test_publish_to_a_P_ref_files_into_the_projects_linked_folder():
    """Before: `--project P1` find-or-created a Drive folder literally named "P1"."""
    drive = _drive_with_projects()
    client = FakeClient([_p(fid="LINKED", url="https://x/LINKED")])

    fid = resolve_project_destination(_ident(), "P1", client=client, runner=drive)

    assert fid == "LINKED" and drive.created == []


def test_publish_to_a_project_with_no_folder_writes_the_folder_back():
    """Before: the folder was made by name and the project stayed drive_folder_id=''."""
    drive, client = _drive_with_projects(), FakeClient([_p()])

    fid = resolve_project_destination(_ident(), "supply tender review",
                                      client=client, runner=drive)

    assert fid == "NEW1"
    assert client.patches == [("P1", {
        "drive_folder_id": "NEW1",
        "drive_folder_url": "https://drive.google.com/drive/folders/NEW1"})]


def test_a_name_with_no_project_files_by_name_and_says_so():
    drive, client, notes = _drive_with_projects(), FakeClient([_p()]), []

    fid = resolve_project_destination(_ident(), "Loose Thing", client=client, runner=drive,
                                      warn=notes.append)

    assert fid == "NEW1" and drive.created == [("PROJ", "Loose Thing")]
    assert client.patches == [] and "project-add" in notes[0]


def test_an_unreadable_board_never_fails_the_publish():
    drive, notes = _drive_with_projects(), []
    fid = resolve_project_destination(_ident(), "X", client=FakeClient(fail_list=True),
                                      runner=drive, warn=notes.append)
    assert fid == "NEW1" and "could not read" in notes[0]


def test_a_failed_write_back_still_files_and_names_the_fix():
    drive, notes = _drive_with_projects(), []
    fid = resolve_project_destination(_ident(), "P1", client=FakeClient([_p()], fail_patch=True),
                                      runner=drive, warn=notes.append)
    assert fid == "NEW1" and "canopy agent project-folder --slug hal --project P1" in notes[0]


def test_find_project_refuses_an_ambiguous_name():
    rows = [_p("P1", "Same"), _p("P2", "Same")]
    assert find_project(rows, "Same") is None
    assert find_project(rows, "p2")["ext_id"] == "P2"


# -- identity: --agent from any directory, no traceback ---------------------------------

def test_gdoc_outside_an_agent_repo_is_one_line_naming_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    md = tmp_path / "x.md"
    md.write_text("# hi\n")

    r = CliRunner().invoke(main, ["gdoc", "publish", "--md", str(md), "--name", "x",
                                  "--project", "P1", "--dry-run"])

    assert r.exit_code == 1
    assert isinstance(r.exception, SystemExit)  # a ClickException, not a traceback
    assert "--agent <slug>" in r.output and "Traceback" not in r.output


def test_agent_resolves_from_the_installed_plugin_when_no_checkout(tmp_path, monkeypatch):
    plugin = tmp_path / "cache" / "hal" / "hal" / "0.1.0"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "hal"}')
    reg = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True)
    reg.write_text(json.dumps({"plugins": {"hal@hal": [{"installPath": str(plugin)}]}}))
    monkeypatch.setattr(agent_email.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(agent_email, "resolve_repo_path", lambda slug: None)

    assert agent_email.find_agent_repo("hal") == plugin
    assert agent_email.installed_plugin_root("eva", home=tmp_path) is None


def test_agent_env_value_reads_the_agents_own_env(tmp_path):
    env = tmp_path / ".hal" / ".env"
    env.parent.mkdir()
    env.write_text("# x\nexport GDRIVE_ROOT_FOLDER=\"HALROOT\"\nOTHER=1\n")
    assert agent_email.agent_env_value("hal", "GDRIVE_ROOT_FOLDER", home=tmp_path) == "HALROOT"
    assert agent_email.agent_env_value("eva", "GDRIVE_ROOT_FOLDER", home=tmp_path) == ""


@pytest.mark.parametrize("agent,want", [("hal", "HALROOT"), (None, "SESSIONROOT")])
def test_explicit_agent_takes_its_own_drive_root_over_the_sessions(monkeypatch, agent, want):
    """In Ada's turn GDRIVE_ROOT_FOLDER is Ada's; `--agent hal` must file into Hal's."""
    monkeypatch.setenv("GDRIVE_ROOT_FOLDER", "SESSIONROOT")
    monkeypatch.setattr(agent_gdoc, "_identity_from_opts", lambda *a: agent_email.EmailIdentity(
        slug="hal", account="hal@dimagi-ai.com", client="canopy", repo=None))
    monkeypatch.setattr(agent_gdoc, "agent_env_value", lambda slug, var: "HALROOT")

    assert agent_gdoc._gdoc_identity_from_opts(None, agent, None, None).root_folder == want


# -- CLI --------------------------------------------------------------------------------

@pytest.fixture
def board(monkeypatch):
    calls, responses = [], {}

    def transport(method, url, headers, body):
        calls.append((method, url.split("/api/")[1], json.loads(body) if body else None))
        return responses.get((method, url.split("/api/")[1]), (200, "{}"))

    monkeypatch.setenv("CANOPY_WEB_PAT", "t")
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://x.test")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)
    monkeypatch.setattr(agent_gdoc, "_gdoc_identity_from_opts", lambda *a: _ident())
    made = []
    monkeypatch.setattr(project_folder, "resolve_subfolder",
                        lambda ident, project, **kw: made.append(project) or f"F-{len(made)}")
    return calls, responses, made


def test_project_add_links_a_folder_by_default(board):
    calls, responses, made = board
    responses[("POST", "agents/hal/projects/")] = (200, json.dumps(_p("P5", "New Work")))

    r = CliRunner().invoke(main, ["agent", "project-add", "--slug", "hal", "--name", "New Work"])

    assert r.exit_code == 0, r.output
    assert made == ["New Work"]
    assert ("PATCH", "agents/hal/projects/P5/", {
        "drive_folder_id": "F-1",
        "drive_folder_url": "https://drive.google.com/drive/folders/F-1"}) in calls
    assert json.loads(r.output)["folder"]["linked"] is True


def test_project_add_no_folder_leaves_drive_alone(board):
    calls, responses, made = board
    responses[("POST", "agents/hal/projects/")] = (200, json.dumps(_p("P5", "New Work")))
    r = CliRunner().invoke(main, ["agent", "project-add", "--slug", "hal", "--name", "New Work",
                                  "--no-folder"])
    assert r.exit_code == 0 and made == [] and all(c[0] != "PATCH" for c in calls)


def test_project_folder_all_backfills_only_active_unlinked(board):
    calls, responses, made = board
    rows = [_p("P1", "Linked", fid="X"), _p("P2", "Empty"),
            {**_p("P3", "Done"), "status": "done"}]
    responses[("GET", "agents/hal/projects/")] = (200, json.dumps(rows))

    r = CliRunner().invoke(main, ["agent", "project-folder", "--slug", "hal", "--all"])

    assert r.exit_code == 0, r.output
    assert made == ["Empty"]
    assert [c[1] for c in calls if c[0] == "PATCH"] == ["agents/hal/projects/P2/"]


# -- a dry run never writes -------------------------------------------------------------

def test_a_dry_run_of_a_P_ref_never_creates_a_folder_named_after_it():
    """2026-10-08: `gdoc publish --agent hal --project P3 --dry-run` created `Projects/P3`."""
    drive, client = _drive_with_projects(), FakeClient([_p("P3", "Reliability")])

    got = resolve_project_destination(_ident(), "P3", client=client, runner=drive, dry_run=True)

    assert drive.created == [] and client.patches == []
    assert got == "(would create Projects/Reliability)"


def test_a_dry_run_uses_the_linked_folder():
    client = FakeClient([_p("P3", "Reliability", fid="LINKED")])
    assert resolve_project_destination(_ident(), "P3", client=client, runner=FakeDrive(),
                                      dry_run=True) == "LINKED"


def test_resolve_subfolder_create_false_finds_but_never_creates():
    drive = _drive_with_projects(Existing="EX")
    assert agent_gdoc.resolve_subfolder(_ident(), project="Existing", runner=drive,
                                        create=False) == "EX"
    assert agent_gdoc.resolve_subfolder(_ident(), project="Missing", runner=drive,
                                        create=False) == ""
    assert agent_gdoc.resolve_subfolder(_ident(), area="Process State", runner=drive,
                                        create=False) == ""
    assert drive.created == []
