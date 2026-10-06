"""The huddle's one write outside canopy-web: the clean-outcomes record in the leader's
Drive `Process State/Huddles/<id>.json`."""
import json
import subprocess as sp

import pytest

from orchestrator import huddle_store as hs


class FakeIdentity:
    account, client, root_folder, slug = "ada@dimagi-ai.com", "canopy", "ROOT", "ada"


@pytest.fixture()
def drive(monkeypatch):
    seen = {}
    monkeypatch.setattr("orchestrator.agent_gdoc.resolve_gdoc_identity", lambda r: FakeIdentity())
    monkeypatch.setattr("orchestrator.agent_email.reconcile_client", lambda *a, **k: None)

    def subfolder(identity, **kw):
        seen["subfolder"] = kw
        return "HFOLDER"
    monkeypatch.setattr("orchestrator.agent_gdoc.resolve_subfolder", subfolder)
    return seen


def _runner(calls, ls_rows, uploaded=None, files=None):
    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:3] == ["drive", "ls"]:
            return sp.CompletedProcess(cmd, 0, stdout=json.dumps(ls_rows), stderr="")
        if cmd[1:3] == ["drive", "upload"]:
            if uploaded is not None:
                uploaded.append(json.loads(open(cmd[3], encoding="utf-8").read()))
            return sp.CompletedProcess(cmd, 0, stdout=json.dumps({"id": "F1"}), stderr="")
        if cmd[1:3] == ["drive", "download"]:
            out = cmd[cmd.index("--out") + 1]
            with open(out, "w", encoding="utf-8") as fh:
                json.dump((files or {})[cmd[3]], fh)
            return sp.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        return sp.CompletedProcess(cmd, 0, stdout="{}", stderr="")
    return run


def test_drive_write_creates_in_process_state_huddles(tmp_path, drive):
    calls, uploaded = [], []
    store = hs.DriveHuddleStore(tmp_path, runner=_runner(calls, [], uploaded))
    fid = store.write({"id": "work-fleet-20261006", "team": "fleet"})
    assert fid == "F1"
    assert drive["subfolder"]["area"] == "Process State"
    assert drive["subfolder"]["project"] == "Huddles"
    up = [c for c in calls if c[1:3] == ["drive", "upload"]][0]
    assert up[up.index("--name") + 1] == "work-fleet-20261006.json"
    assert up[up.index("--parent") + 1] == "HFOLDER"
    assert uploaded == [{"id": "work-fleet-20261006", "team": "fleet"}]
    assert all("ada@dimagi-ai.com" in c for c in calls)


def test_drive_write_replaces_an_existing_record(tmp_path, drive):
    calls = []
    store = hs.DriveHuddleStore(
        tmp_path, runner=_runner(calls, [{"id": "F9", "name": "work-fleet-20261006.json"}]))
    assert store.write({"id": "work-fleet-20261006"}) == "F9"
    up = [c for c in calls if c[1:3] == ["drive", "upload"]][0]
    assert up[up.index("--replace") + 1] == "F9" and "--parent" not in up


def test_drive_read_and_recent(tmp_path, drive):
    rows = [{"id": "A", "name": "work-fleet-20261001.json"},
            {"id": "B", "name": "work-fleet-20261006.json"},
            {"id": "C", "name": "work-fleet-20261006-2.json"},
            {"id": "D", "name": "work-other-20261007.json"},
            {"id": "E", "name": "notes.txt"}]
    files = {k: {"id": k} for k in "ABCD"}
    store = hs.DriveHuddleStore(tmp_path, runner=_runner([], rows, files=files))
    assert [r["id"] for r in store.recent("fleet", limit=2)] == ["C", "B"]
    assert [r["id"] for r in store.recent("fleet")] == ["C", "B", "A"]


def test_drive_read_missing_is_none(tmp_path, drive):
    store = hs.DriveHuddleStore(tmp_path, runner=_runner([], []))
    assert store.read("work-fleet-20261006") is None


def test_drive_failure_raises(tmp_path, drive):
    def run(cmd, **kw):
        return sp.CompletedProcess(cmd, 1, stdout="", stderr="boom")
    store = hs.DriveHuddleStore(tmp_path, runner=run)
    with pytest.raises(hs.HuddleStoreError, match="boom"):
        store.write({"id": "work-fleet-20261006"})


def test_local_store_roundtrip_and_recent(tmp_path):
    s = hs.LocalHuddleStore(tmp_path)
    assert s.read("work-fleet-20261001") is None
    for hid in ("work-fleet-20261001", "work-fleet-20261006", "work-fleet-20261006-2",
                "work-x-20261007"):
        s.write({"id": hid, "team": hid.split("-")[1]})
    s.write({"id": "work-fleet-20261001", "team": "fleet", "v": 2})  # replace, not append
    assert s.read("work-fleet-20261001")["v"] == 2
    assert [r["id"] for r in s.recent("fleet")] == [
        "work-fleet-20261006-2", "work-fleet-20261006", "work-fleet-20261001"]
    assert [r["id"] for r in s.recent("fleet", limit=1)] == ["work-fleet-20261006-2"]


def test_record_id_is_validated(tmp_path):
    with pytest.raises(hs.HuddleStoreError):
        hs.LocalHuddleStore(tmp_path).write({"id": "../escape"})
