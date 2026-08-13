"""Tests for the shared agent gdoc engine (canopy gdoc — shared-gog-gdrive.md §5)."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.agent_gdoc import (
    REPLACE_SENTINEL,
    AgentGdocError,
    GdocIdentity,
    build_export_command,
    build_replace_commands,
    replace_degradations,
    build_share_command,
    build_upload_command,
    find_child_folder,
    md_to_html,
    parse_list_result,
    parse_mkdir_result,
    parse_upload_result,
    parse_sheet_create_result,
    parse_tab_spec,
    publish,
    publish_sheet,
    read_table,
    folder_contents,
    _a1_col,
    resolve_gdoc_identity,
    resolve_subfolder,
    verify_permissions,
)


# --------------------------------------------------------------------------------------
# identity resolution
# --------------------------------------------------------------------------------------

def _agent_repo(tmp_path, *, email="hal@dimagi-ai.com", gog_client=None, slug="hal",
                root_folder=None, share_default=None):
    repo = tmp_path / slug
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": slug}))
    agent = {"name": slug.title(), "email": email}
    if gog_client is not None:
        agent["gog_client"] = gog_client
    if root_folder is not None:
        agent["gdrive_root_folder"] = root_folder
    if share_default is not None:
        agent["gdrive_share_default"] = share_default
    (repo / "config").mkdir()
    (repo / "config" / "agent.json").write_text(json.dumps(agent))
    return repo


def test_resolve_identity_from_agent_json(tmp_path):
    repo = _agent_repo(tmp_path, gog_client="hal-oauth", root_folder="FOLDER123",
                       share_default="anyone")
    ident = resolve_gdoc_identity(repo)
    assert ident.slug == "hal"
    assert ident.account == "hal@dimagi-ai.com"
    assert ident.client == "hal-oauth"
    assert ident.root_folder == "FOLDER123"
    assert ident.share_default == "anyone"


def test_resolve_identity_defaults(tmp_path):
    ident = resolve_gdoc_identity(_agent_repo(tmp_path))
    assert ident.client == "hal"           # client defaults to slug
    assert ident.root_folder == ""         # optional
    assert ident.share_default == "domain"  # safe default


def test_root_folder_from_env_wins_over_agent_json(tmp_path, monkeypatch):
    # The Drive root is environment-specific and vault-resolved: the reconciler injects
    # GDRIVE_ROOT_FOLDER from op://Agent-<Slug>/gdrive-root-folder. It must beat any
    # legacy committed value in agent.json.
    monkeypatch.setenv("GDRIVE_ROOT_FOLDER", "FROM_VAULT")
    ident = resolve_gdoc_identity(_agent_repo(tmp_path, root_folder="STALE_IN_GIT"))
    assert ident.root_folder == "FROM_VAULT"


def test_root_folder_falls_back_to_agent_json_when_env_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("GDRIVE_ROOT_FOLDER", raising=False)
    ident = resolve_gdoc_identity(_agent_repo(tmp_path, root_folder="LEGACY"))
    assert ident.root_folder == "LEGACY"


def test_root_folder_empty_when_neither_present(tmp_path, monkeypatch):
    monkeypatch.delenv("GDRIVE_ROOT_FOLDER", raising=False)
    assert resolve_gdoc_identity(_agent_repo(tmp_path)).root_folder == ""


def test_resolve_identity_rejects_bad_share_default(tmp_path):
    ident = resolve_gdoc_identity(_agent_repo(tmp_path, share_default="everyone"))
    assert ident.share_default == "domain"  # unknown -> safe default


def test_resolve_identity_requires_mailbox(tmp_path):
    with pytest.raises(AgentGdocError, match="config/agent.json"):
        resolve_gdoc_identity(_agent_repo(tmp_path, email=""))


# --------------------------------------------------------------------------------------
# markdown -> html
# --------------------------------------------------------------------------------------

def test_md_to_html_headings_and_links():
    out = md_to_html("# Title\n\nSome **bold** and a [link](https://x.com).")
    assert "<h1>Title</h1>" in out
    assert "<strong>bold</strong>" in out
    assert '<a href="https://x.com">link</a>' in out


def test_md_to_html_lists():
    out = md_to_html("- one\n- two\n\n1. first\n2. second")
    assert "<ul><li>one</li><li>two</li></ul>" in out
    assert "<ol><li>first</li><li>second</li></ol>" in out


def test_md_to_html_escapes_html():
    assert "&lt;script&gt;" in md_to_html("a <script> tag")


# --------------------------------------------------------------------------------------
# gog command construction
# --------------------------------------------------------------------------------------

def _ident(**kw):
    base = dict(slug="hal", account="hal@dimagi-ai.com", client="hal")
    base.update(kw)
    return GdocIdentity(**base)


def test_build_upload_command_create():
    cmd = build_upload_command(_ident(), html_path="/tmp/x.html", name="My Doc",
                               parent="FOLDER123")
    assert cmd[:3] == ["gog", "drive", "upload"]
    assert "--convert-to" in cmd and cmd[cmd.index("--convert-to") + 1] == "doc"
    assert cmd[cmd.index("--parent") + 1] == "FOLDER123"
    assert cmd[cmd.index("--name") + 1] == "My Doc"
    assert cmd[cmd.index("--account") + 1] == "hal@dimagi-ai.com"


def test_build_replace_commands_edits_in_place_via_docs_api():
    # issue #353: a native-Doc replace must NOT be a drive media overwrite. It's a
    # docs write (blank to a sentinel) + docs find-replace (markdown re-insert).
    cmds = build_replace_commands(_ident(), doc_id="DOCID", md_path="/tmp/x.md")
    assert [c[:3] for c in cmds] == [["gog", "docs", "write"],
                                     ["gog", "docs", "find-replace"]]
    # never a drive media overwrite on a native Doc
    assert not any(c[:3] == ["gog", "drive", "upload"] for c in cmds)
    assert not any("--mime-type" in c for c in cmds)
    write, find_replace = cmds
    assert write[3] == "DOCID" and write[write.index("--text") + 1] == REPLACE_SENTINEL
    assert find_replace[3] == "DOCID" and find_replace[4] == REPLACE_SENTINEL
    assert find_replace[find_replace.index("--content-file") + 1] == "/tmp/x.md"
    assert find_replace[find_replace.index("--format") + 1] == "markdown"


def test_build_replace_commands_renames_only_when_name_given():
    assert len(build_replace_commands(_ident(), doc_id="D", md_path="/tmp/x.md")) == 2
    with_name = build_replace_commands(_ident(), doc_id="D", md_path="/tmp/x.md", name="New")
    assert len(with_name) == 3
    rename = with_name[2]
    assert rename[:3] == ["gog", "drive", "rename"] and rename[3] == "D" and rename[4] == "New"


def test_build_share_command_domain():
    cmd = build_share_command(_ident(), "DOCID", share="domain", email=None)
    assert cmd[cmd.index("--to") + 1] == "domain"
    assert cmd[cmd.index("--domain") + 1] == "dimagi.com"
    assert cmd[cmd.index("--role") + 1] == "reader"


def test_build_share_command_user_requires_email():
    with pytest.raises(AgentGdocError, match="share-email"):
        build_share_command(_ident(), "DOCID", share="user", email=None)


def test_parse_upload_result_unwraps_gog_file_envelope():
    # gog v0.12 wraps the created file under a "file" key (confirmed live) — the shape a
    # naive mock misses. This is the exact payload that broke the first live smoke.
    stdout = json.dumps({"file": {"id": "D1", "webViewLink": "u", "mimeType": "…document"}})
    out = parse_upload_result(stdout)
    assert out["id"] == "D1"
    assert out["url"] == "u"


def test_parse_upload_result_liberal_keys():
    # unwrapped top-level id/link still works (defensive against a gog shape change)
    assert parse_upload_result(json.dumps({"id": "D1", "webViewLink": "u"}))["id"] == "D1"
    # falls back to a constructed url when no link key is present
    assert parse_upload_result(json.dumps({"file": {"id": "D2"}}))["url"].endswith("/D2/edit")
    # non-JSON is surfaced as raw, not crashed on
    assert parse_upload_result("boom")["id"] == ""


# --------------------------------------------------------------------------------------
# publish (with a fake gog runner — no network)
# --------------------------------------------------------------------------------------

class _FakeGog:
    """Records commands; returns queued responses by (verb) — upload/share/permissions."""

    def __init__(self, *, upload_ok=True, share_ok=True, perm_type="domain",
                 export_md=None, export_ok=True):
        self.calls = []
        self.upload_ok = upload_ok
        self.share_ok = share_ok
        self.perm_type = perm_type
        self.export_md = export_md
        self.export_ok = export_ok

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.calls.append(cmd)
        verb = cmd[2]
        if verb == "export":
            if not self.export_ok:
                return SimpleNamespace(returncode=1, stdout="", stderr="export blew up")
            out = cmd[cmd.index("--out") + 1]
            Path(out).write_text(self.export_md or "", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"path": out}), stderr="")
        if verb == "upload":
            if not self.upload_ok:
                return SimpleNamespace(returncode=1, stdout="", stderr="nope")
            # gog's REAL shape: the file is wrapped under a "file" envelope (confirmed live).
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"file": {"id": "DOC1", "webViewLink": "URL1"}}), stderr="")
        if verb == "share":
            # gog's real share payload: {"link", "permission": {...}, "permissionId"}.
            return SimpleNamespace(
                returncode=0 if self.share_ok else 1,
                stdout=json.dumps({"permission": {"type": "domain", "domain": "dimagi.com"}}),
                stderr="x")
        if verb == "permissions":
            # gog's real permissions payload: list under a "permissions" key.
            perm = {"type": self.perm_type, "domain": "dimagi.com"}
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"permissions": [perm]}), stderr="")
        # in-place replace verbs (docs write / docs find-replace / drive rename) just succeed
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")


def test_publish_create_shares_and_verifies(tmp_path):
    md = tmp_path / "d.md"
    md.write_text("# Hi\n\nbody")
    gog = _FakeGog()
    res = publish(_ident(), name="Doc", parent="F1", md_path=str(md), share="domain",
                  runner=gog)
    assert res["id"] == "DOC1"
    assert res["url"] == "URL1"
    assert res["shared"] == "domain"
    assert res["verified"] is True
    verbs = [c[2] for c in gog.calls]
    assert verbs == ["upload", "share", "permissions"]  # created, shared, verified


def test_publish_reports_unverified_when_permission_missing(tmp_path):
    md = tmp_path / "d.md"
    md.write_text("body")
    gog = _FakeGog(perm_type="anyone")  # asked for domain, readback shows only anyone
    res = publish(_ident(), name="Doc", parent="F1", md_path=str(md), share="domain",
                  runner=gog)
    assert res["verified"] is False


def test_publish_replace_edits_in_place_and_preserves_share(tmp_path):
    # issue #353: replace keeps the same id/url, never re-shares, and edits in place via
    # the Docs API (docs write + docs find-replace) — no drive upload/media overwrite.
    md = tmp_path / "d.md"
    md.write_text("body")
    gog = _FakeGog()
    res = publish(_ident(), name=None, parent=None, md_path=str(md), share="domain",
                  replace="DOCX", runner=gog)
    assert res["id"] == "DOCX"
    assert res["url"].endswith("/DOCX/edit")
    assert res["replaced"] is True
    assert res["shared"] == "preserved"
    verbs = [c[2] for c in gog.calls]
    # in-place edit, no upload, no re-share — then read the body back (issue #451)
    assert verbs == ["write", "find-replace", "export"]


def test_publish_replace_with_name_renames(tmp_path):
    md = tmp_path / "d.md"
    md.write_text("body")
    gog = _FakeGog()
    publish(_ident(), name="Renamed", parent=None, md_path=str(md), share="domain",
            replace="DOCX", runner=gog)
    verbs = [c[2] for c in gog.calls]
    assert verbs == ["write", "find-replace", "rename", "export"]


# --------------------------------------------------------------------------------------
# issue #451 — the replace path must not claim "verified" for a body it never read back.
#
# The exported-markdown fixtures below are VERBATIM `gog docs export --format md` output
# captured from real Drive round trips on 2026-08-12, not hand-written approximations.
# --------------------------------------------------------------------------------------

# What a CORRECT render looks like (published through the create/HTML path).
GOOD_EXPORT = """# **Version One Heading**

An ordinary opening paragraph.

> 1. First numbered item
> 2. Second numbered item
> 3. Third numbered item

## **A Second Heading**

> * a bullet
> * another bullet

Closing paragraph of version one.
"""

# What `docs find-replace --format markdown` actually produced from the SAME source: the
# list markers survived as escaped literal text, so the Doc holds no list at all.
MANGLED_ORDERED_EXPORT = """# **T**

Prose with **bold** and code.
1\\. alpha
1\\. beta
"""

MANGLED_BULLET_EXPORT = """# **T**

Plain prose.
• alpha
• beta
"""

SOURCE_WITH_LISTS = "# T\n\nProse.\n\n1. alpha\n2. beta\n"


def test_replace_degradations_clean_render_reports_nothing():
    src = "# V\n\npara\n\n1. First numbered item\n2. Second\n\n- a bullet\n"
    assert replace_degradations(src, GOOD_EXPORT) == []


def test_replace_degradations_catches_flattened_ordered_list():
    findings = replace_degradations(SOURCE_WITH_LISTS, MANGLED_ORDERED_EXPORT)
    assert len(findings) == 1
    assert "numbered lists were flattened" in findings[0]


def test_replace_degradations_catches_bullet_glyph():
    findings = replace_degradations("# T\n\n- alpha\n- beta\n", MANGLED_BULLET_EXPORT)
    assert any("bullet lists were flattened" in f for f in findings)


def test_replace_degradations_census_catches_silent_list_loss():
    # No escape artefacts at all — a future gog that just drops the list. The census
    # signal is the only thing that can see this.
    findings = replace_degradations(SOURCE_WITH_LISTS, "# T\n\nProse.\n\nalpha\nbeta\n")
    assert len(findings) == 1
    assert "the list structure was lost" in findings[0]


def test_replace_degradations_ignores_lists_inside_code_fences():
    # `1. x` inside a fence is content, not a list — it must not trip the census.
    src = "# T\n\n```\n1. not a list\n- also not\n```\n"
    assert replace_degradations(src, "# **T**\n\nnot a list\n") == []


def test_replace_degradations_no_lists_anywhere_is_clean():
    assert replace_degradations("# T\n\njust prose\n", "# **T**\n\njust prose\n") == []


def test_publish_replace_reports_degraded_and_unverified(tmp_path):
    """The regression that matters: a mangled body must NOT come back verified."""
    md = tmp_path / "d.md"
    md.write_text(SOURCE_WITH_LISTS)
    gog = _FakeGog(export_md=MANGLED_ORDERED_EXPORT)
    res = publish(_ident(), name=None, parent=None, md_path=str(md), share="domain",
                  replace="DOCX", runner=gog)
    assert res["verified"] is False
    assert res["degraded"] and "numbered lists were flattened" in res["degraded"][0]


def test_publish_replace_verified_when_render_is_faithful(tmp_path):
    md = tmp_path / "d.md"
    md.write_text("# V\n\npara\n\n1. First numbered item\n2. Second\n\n- a bullet\n")
    gog = _FakeGog(export_md=GOOD_EXPORT)
    res = publish(_ident(), name=None, parent=None, md_path=str(md), share="domain",
                  replace="DOCX", runner=gog)
    assert res["verified"] is True
    assert res["degraded"] == []


def test_publish_replace_stays_verified_when_export_fails(tmp_path):
    """A flaky export must not turn a good deliverable into a failed publish."""
    md = tmp_path / "d.md"
    md.write_text(SOURCE_WITH_LISTS)
    gog = _FakeGog(export_ok=False)
    res = publish(_ident(), name=None, parent=None, md_path=str(md), share="domain",
                  replace="DOCX", runner=gog)
    assert res["verified"] is True
    assert res["degraded"] == []


def test_build_export_command_pins_the_output_path():
    # gog otherwise writes into its own config dir under a name built from the doc TITLE.
    cmd = build_export_command(_ident(), "DOC9", "/tmp/out.md")
    assert cmd[:5] == ["gog", "docs", "export", "DOC9", "--format"]
    assert cmd[cmd.index("--out") + 1] == "/tmp/out.md"
    assert cmd[cmd.index("--account") + 1] == "hal@dimagi-ai.com"


def test_publish_create_requires_name_and_parent(tmp_path):
    md = tmp_path / "d.md"
    md.write_text("body")
    with pytest.raises(AgentGdocError, match="required for a new doc"):
        publish(_ident(), name=None, parent=None, md_path=str(md), share="none")


def test_publish_dry_run_touches_nothing(tmp_path):
    md = tmp_path / "d.md"
    md.write_text("# T\n\nbody")
    gog = _FakeGog()
    res = publish(_ident(), name="Doc", parent="F1", md_path=str(md), share="domain",
                  dry_run=True, runner=gog)
    assert res["dry_run"] is True
    assert gog.calls == []  # never shelled out
    assert res["upload_cmd"][:3] == ["gog", "drive", "upload"]


def test_publish_raises_on_upload_failure(tmp_path):
    md = tmp_path / "d.md"
    md.write_text("body")
    with pytest.raises(AgentGdocError, match="upload failed"):
        publish(_ident(), name="Doc", parent="F1", md_path=str(md), share="domain",
                runner=_FakeGog(upload_ok=False))


# --------------------------------------------------------------------------------------
# subfolder resolution — <agent root>/<area>[/<project>] (agent-core/deliverables.md layout)
# --------------------------------------------------------------------------------------

FOLDER = "application/vnd.google-apps.folder"


class _FakeDrive:
    """Fakes `gog drive ls` + `gog drive mkdir` over an in-memory {parent: [children]} tree.

    Each mkdir mints a new id and appends a folder child; ls returns the parent's children.
    Records mkdir'd (parent, name) pairs so a test can assert nothing was created."""

    def __init__(self, tree=None):
        self.tree = {k: list(v) for k, v in (tree or {}).items()}
        self.created = []
        self._n = 0

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        verb = cmd[2]
        parent = cmd[cmd.index("--parent") + 1]
        if verb == "ls":
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout=json.dumps({"files": self.tree.get(parent, [])}))
        if verb == "mkdir":
            name = cmd[3]
            self._n += 1
            fid = f"NEW{self._n}"
            self.created.append((parent, name))
            self.tree.setdefault(parent, []).append(
                {"id": fid, "name": name, "mimeType": FOLDER})
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout=json.dumps({"folder": {"id": fid, "name": name}}))
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")


def test_parse_list_and_mkdir_shapes():
    files = parse_list_result(json.dumps({"files": [{"id": "A", "name": "Projects",
                                                     "mimeType": FOLDER}]}))
    assert files[0]["id"] == "A"
    assert parse_mkdir_result(json.dumps({"folder": {"id": "F9", "name": "x"}})) == "F9"
    assert parse_list_result("not json") == []
    assert parse_mkdir_result("not json") == ""


def test_find_child_folder_matches_folder_not_file():
    # a same-named FILE must never shadow the folder we're resolving
    drive = _FakeDrive({"ROOT": [
        {"id": "FILE", "name": "Projects", "mimeType": "application/pdf"},
        {"id": "DIR", "name": "Projects", "mimeType": FOLDER},
    ]})
    assert find_child_folder(_ident(root_folder="ROOT"), "ROOT", "Projects", runner=drive) == "DIR"
    assert find_child_folder(_ident(root_folder="ROOT"), "ROOT", "Missing", runner=drive) == ""


def test_resolve_subfolder_reuses_existing_project():
    drive = _FakeDrive({
        "ROOT": [{"id": "PROJ", "name": "Projects", "mimeType": FOLDER}],
        "PROJ": [{"id": "POD", "name": "Podcasting", "mimeType": FOLDER}],
    })
    got = resolve_subfolder(_ident(root_folder="ROOT"), project="Podcasting", runner=drive)
    assert got == "POD"
    assert drive.created == []  # both folders existed → nothing created


def test_resolve_subfolder_creates_project_under_projects():
    drive = _FakeDrive({"ROOT": [{"id": "PROJ", "name": "Projects", "mimeType": FOLDER}]})
    got = resolve_subfolder(_ident(root_folder="ROOT"), project="New Thing", runner=drive)
    assert got == "NEW1"
    assert drive.created == [("PROJ", "New Thing")]  # area existed, project created


def test_resolve_subfolder_process_state_area_no_project():
    drive = _FakeDrive({"ROOT": []})
    got = resolve_subfolder(_ident(root_folder="ROOT"), area="Process State", runner=drive)
    assert got == "NEW1"
    assert drive.created == [("ROOT", "Process State")]


def test_resolve_subfolder_requires_root():
    with pytest.raises(AgentGdocError, match="no Drive root resolved"):
        resolve_subfolder(_ident(root_folder=""), project="X", runner=_FakeDrive())


# --------------------------------------------------------------------------------------
# Sheets — `canopy gsheet publish` (added 2026-08-13)
#
# WHY: `publish` covered markdown→Doc and nothing covered a SPREADSHEET, so a skill needing
# a roster/tracker fell through to raw `gog sheets create` — which lands in My Drive root,
# unshared. That happened for real on 2026-08-12 (a 45-row target roster). These pin the
# filing contract for the tabular path so it can't regress to "wherever gog defaults to".
# --------------------------------------------------------------------------------------

class _FakeSheets:
    """Fakes `gog sheets create` + `gog sheets update` + `gog drive share|permissions`."""

    def __init__(self, *, create_rc=0, update_rc=0, share_rc=0, perms=None):
        self.create_rc, self.update_rc, self.share_rc = create_rc, update_rc, share_rc
        self.perms = perms if perms is not None else [
            {"type": "domain", "domain": "dimagi.com", "role": "reader"}]
        self.cmds = []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.cmds.append(cmd)
        svc, verb = cmd[1], cmd[2]
        if (svc, verb) == ("sheets", "create"):
            return SimpleNamespace(returncode=self.create_rc, stderr="boom",
                                   stdout=json.dumps({"spreadsheetId": "SID1",
                                                      "spreadsheetUrl": "https://s/SID1"}))
        if (svc, verb) == ("sheets", "update"):
            return SimpleNamespace(returncode=self.update_rc, stdout="{}", stderr="boom")
        if (svc, verb) == ("drive", "share"):
            return SimpleNamespace(returncode=self.share_rc, stdout="{}", stderr="boom")
        if (svc, verb) == ("drive", "permissions"):
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout=json.dumps({"permissions": self.perms}))
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")


def _tsv(tmp_path, name="roster.tsv", rows=(("Tier", "Person"), ("1", "Elizabeth Kelly"))):
    p = tmp_path / name
    p.write_text("\n".join("\t".join(r) for r in rows))
    return str(p)


def test_read_table_infers_delimiter_from_extension(tmp_path):
    tsv = tmp_path / "a.tsv"
    tsv.write_text("a\tb\nc\td")
    assert read_table(str(tsv)) == [["a", "b"], ["c", "d"]]
    # A roster cell is far likelier to hold a comma than a tab, hence the default.
    csv_f = tmp_path / "a.csv"
    csv_f.write_text('x,y\n"Lesh, Neal",z')
    assert read_table(str(csv_f)) == [["x", "y"], ["Lesh, Neal", "z"]]


def test_parse_tab_spec_named_and_bare():
    assert parse_tab_spec("Clean-up=/tmp/c.tsv") == ("Clean-up", "/tmp/c.tsv")
    assert parse_tab_spec("/tmp/roster.tsv") == ("roster", "/tmp/roster.tsv")
    # split on the FIRST '=' so a path containing '=' survives
    assert parse_tab_spec("T=/tmp/a=b.tsv") == ("T", "/tmp/a=b.tsv")
    with pytest.raises(AgentGdocError):
        parse_tab_spec("=/tmp/x.tsv")


def test_a1_col_is_bijective_base26():
    assert (_a1_col(1), _a1_col(26), _a1_col(27), _a1_col(52)) == ("A", "Z", "AA", "AZ")


def test_publish_sheet_refuses_without_parent(tmp_path):
    """THE regression this whole change exists for: no destination = My Drive root."""
    with pytest.raises(AgentGdocError) as e:
        publish_sheet(_ident(), name="Roster", parent=None,
                      tabs=[("Sheet1", _tsv(tmp_path))], share="domain")
    assert "My Drive root" in str(e.value)


def test_publish_sheet_creates_into_parent_writes_tabs_and_shares(tmp_path):
    fake = _FakeSheets()
    out = publish_sheet(_ident(), name="Roster", parent="PROJ",
                        tabs=[("Sheet1", _tsv(tmp_path)),
                              ("Clean-up", _tsv(tmp_path, "c.tsv"))],
                        share="domain", runner=fake)
    create = fake.cmds[0]
    assert create[:3] == ["gog", "sheets", "create"]
    # the destination is non-negotiable, and both tabs are named at creation
    assert create[create.index("--parent") + 1] == "PROJ"
    assert create[create.index("--sheets") + 1] == "Sheet1,Clean-up"
    updates = [c for c in fake.cmds if c[1:3] == ["sheets", "update"]]
    assert [u[4] for u in updates] == ["Sheet1!A1:B2", "Clean-up!A1:B2"]
    assert out["id"] == "SID1" and out["shared"] == "domain" and out["verified"] is True
    assert out["tabs"] == [{"tab": "Sheet1", "rows": 2}, {"tab": "Clean-up", "rows": 2}]


def test_publish_sheet_range_sized_to_the_grid(tmp_path):
    fake = _FakeSheets()
    wide = _tsv(tmp_path, "w.tsv", rows=(tuple("abcdefghijklmnopqrstuvwxyzA"), ("1",)))
    publish_sheet(_ident(), name="R", parent="P", tabs=[("T", wide)],
                  share="none", runner=fake)
    upd = [c for c in fake.cmds if c[1:3] == ["sheets", "update"]][0]
    assert upd[4] == "T!A1:AA2"   # 27 columns → AA, not 'A1'


def test_publish_sheet_reports_a_failed_tab_write_without_claiming_success(tmp_path):
    fake = _FakeSheets(update_rc=1)
    with pytest.raises(AgentGdocError) as e:
        publish_sheet(_ident(), name="R", parent="P", tabs=[("T", _tsv(tmp_path))],
                      share="domain", runner=fake)
    # names the created sheet so the half-built artifact can be found and cleaned up
    assert "SID1" in str(e.value) and "writing tab" in str(e.value)


def test_publish_sheet_failed_share_is_loud(tmp_path):
    fake = _FakeSheets(share_rc=1)
    with pytest.raises(AgentGdocError) as e:
        publish_sheet(_ident(), name="R", parent="P", tabs=[("T", _tsv(tmp_path))],
                      share="domain", runner=fake)
    assert "share failed" in str(e.value)


def test_publish_sheet_unverified_share_does_not_report_verified(tmp_path):
    # share landed but the permission never appeared — the exact "shared it, honest" trap
    fake = _FakeSheets(perms=[])
    out = publish_sheet(_ident(), name="R", parent="P", tabs=[("T", _tsv(tmp_path))],
                        share="domain", runner=fake)
    assert out["verified"] is False


def test_publish_sheet_dry_run_touches_nothing(tmp_path):
    fake = _FakeSheets()
    out = publish_sheet(_ident(), name="R", parent="P", tabs=[("T", _tsv(tmp_path))],
                        share="domain", dry_run=True, runner=fake)
    assert out["dry_run"] is True and fake.cmds == []


def test_parse_sheet_create_result_tolerates_envelopes():
    assert parse_sheet_create_result(json.dumps(
        {"spreadsheet": {"spreadsheetId": "S1"}}))["id"] == "S1"
    # id but no url → synthesize the canonical link rather than hand back ""
    assert parse_sheet_create_result(json.dumps({"spreadsheetId": "S2"}))["url"] \
        == "https://docs.google.com/spreadsheets/d/S2/edit"
    assert parse_sheet_create_result("not json")["id"] == ""


# --------------------------------------------------------------------------------------
# Reuse reporting — silence about landing on prior work is itself the defect
# --------------------------------------------------------------------------------------

def test_resolve_subfolder_trace_marks_reused_vs_created():
    drive = _FakeDrive({
        "ROOT": [{"id": "PROJ", "name": "Projects", "mimeType": FOLDER}],
        "PROJ": [],
    })
    trace: list = []
    resolve_subfolder(_ident(root_folder="ROOT"), project="Bay Area Trip",
                      runner=drive, trace=trace)
    assert trace == [
        {"name": "Projects", "id": "PROJ", "created": False},
        {"name": "Bay Area Trip", "id": "NEW1", "created": True},
    ]


def test_folder_contents_excludes_folders_and_never_raises():
    drive = _FakeDrive({"P": [
        {"id": "F", "name": "sub", "mimeType": FOLDER},
        {"id": "D", "name": "outreach macros.gdoc", "mimeType": "application/vnd.google-apps.document"},
    ]})
    got = folder_contents(_ident(), "P", runner=drive)
    assert [f["name"] for f in got] == ["outreach macros.gdoc"]

    def boom(*a, **k):
        raise OSError("gog exploded")
    # advisory context must never take down a publish
    assert folder_contents(_ident(), "P", runner=boom) == []
