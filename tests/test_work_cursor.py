"""The recurring-work cursor: don't re-analyze what you already analyzed.

Four ad-hoc versions of this existed before the module (ada/bin/ada-run-cursor,
eva/bin/cos-state, shareout's resolve_default_range, and ada/skills/self-review's
deliberate no-state stance). These tests pin the semantics the generic one has to
get right — most of which at least one of those four got wrong.
"""
import datetime as dt

import pytest

from orchestrator.work_cursor import (
    CursorError,
    advance,
    empty_cursor,
    filter_new,
    slug_for_key,
)


def item(i, ts):
    return {"id": i, "ts": ts}


# --- the basic contract ------------------------------------------------------

def test_first_run_everything_is_new():
    cur = empty_cursor("hal/ace-review")
    got = filter_new(cur, [item("a", "2026-07-01T00:00:00Z"), item("b", "2026-07-02T00:00:00Z")])
    assert [i["id"] for i in got] == ["a", "b"]


def test_second_run_skips_what_was_processed():
    cur = advance(empty_cursor("k"), [item("a", "2026-07-01T00:00:00Z")])
    got = filter_new(cur, [item("a", "2026-07-01T00:00:00Z"), item("b", "2026-07-02T00:00:00Z")])
    assert [i["id"] for i in got] == ["b"]


def test_reprocessing_is_idempotent():
    """Running twice with no new work must be a no-op, not a slow re-read."""
    cur = advance(empty_cursor("k"), [item("a", "2026-07-01T00:00:00Z")])
    again = advance(cur, [])
    assert filter_new(again, [item("a", "2026-07-01T00:00:00Z")]) == []


# --- THE bug the ID-only implementations have --------------------------------

def test_an_item_that_GREW_since_it_was_processed_is_new_again():
    """ada-run-cursor dedupes on session_id alone, so a session reviewed at 10:00 and
    then worked in until 18:00 is never looked at again — its later, usually more
    interesting half is invisible forever. Transcripts are append-only; 'seen' has to
    mean 'seen AT THIS TIMESTAMP', which is exactly why the cursor needs BOTH an id
    and a time per item, not one or the other."""
    cur = advance(empty_cursor("k"), [item("s1", "2026-07-01T10:00:00Z")])
    got = filter_new(cur, [item("s1", "2026-07-01T18:00:00Z")])
    assert [i["id"] for i in got] == ["s1"]


def test_an_unchanged_item_is_not_new_even_when_re_listed():
    cur = advance(empty_cursor("k"), [item("s1", "2026-07-01T10:00:00Z")])
    assert filter_new(cur, [item("s1", "2026-07-01T10:00:00Z")]) == []


# --- late arrivals: the coverage failure that loses work silently ------------

def test_an_older_item_discovered_late_is_still_new():
    """A watermark-only cursor (`ts <= floor -> skip`) drops an item that existed before
    the last run but only became visible after it — a session on the other macOS account,
    a wider --hours, a late sync. Skipping it is silent and permanent, which is the
    expensive direction to be wrong in. The id map is the authority; the timestamp only
    decides whether a KNOWN id changed."""
    cur = advance(empty_cursor("k"), [item("b", "2026-07-05T00:00:00Z")])
    got = filter_new(cur, [item("a", "2026-07-02T00:00:00Z")])   # older than the watermark
    assert [i["id"] for i in got] == ["a"]


# --- bounded memory, and honesty about its edge ------------------------------

def test_seen_map_is_pruned_and_records_where_it_pruned_to():
    cur = empty_cursor("k")
    items = [item(f"s{n}", f"2026-07-{n:02d}T00:00:00Z") for n in range(1, 11)]
    cur = advance(cur, items, keep=4)
    assert len(cur["seen"]) == 4
    assert set(cur["seen"]) == {"s7", "s8", "s9", "s10"}   # the most RECENT survive
    assert cur["pruned_before"] == "2026-07-07T00:00:00Z"


def test_items_older_than_the_prune_horizon_are_treated_as_seen():
    """Below the horizon we genuinely cannot tell processed from never-seen. Treat as
    seen (don't re-analyze the distant past on every run) — but the cursor carries
    `pruned_before` so a caller can say so rather than implying full coverage."""
    cur = advance(empty_cursor("k"),
                  [item(f"s{n}", f"2026-07-{n:02d}T00:00:00Z") for n in range(1, 11)],
                  keep=4)
    assert filter_new(cur, [item("ancient", "2026-07-02T00:00:00Z")]) == []
    # ...but anything at or after the horizon is still judged on the id map.
    assert [i["id"] for i in filter_new(cur, [item("fresh", "2026-07-09T00:00:00Z")])] == ["fresh"]


# --- bookkeeping Jonathan asked for -----------------------------------------

def test_advance_stamps_time_id_and_run_count():
    now = dt.datetime(2026, 7, 28, 13, 0, tzinfo=dt.timezone.utc)
    cur = advance(empty_cursor("hal/ace-review"), [item("a", "2026-07-01T00:00:00Z")], now=now)
    assert cur["key"] == "hal/ace-review"
    assert cur["last_run_at"] == "2026-07-28T13:00:00Z"
    assert cur["cursor_ts"] == "2026-07-01T00:00:00Z"
    assert cur["runs"] == 1
    assert cur["seen"]["a"] == "2026-07-01T00:00:00Z"
    cur2 = advance(cur, [item("b", "2026-07-02T00:00:00Z")], now=now)
    assert cur2["runs"] == 2
    assert cur2["cursor_ts"] == "2026-07-02T00:00:00Z"


def test_cursor_ts_never_goes_backwards():
    """Processing a late-arriving older item must not rewind the high-water mark."""
    cur = advance(empty_cursor("k"), [item("b", "2026-07-05T00:00:00Z")])
    cur = advance(cur, [item("a", "2026-07-02T00:00:00Z")])
    assert cur["cursor_ts"] == "2026-07-05T00:00:00Z"


# --- malformed input fails loud ---------------------------------------------

@pytest.mark.parametrize("bad", [
    {"ts": "2026-07-01T00:00:00Z"},          # no id
    {"id": "a"},                             # no ts
    {"id": "", "ts": "2026-07-01T00:00:00Z"},
])
def test_items_without_both_stamps_are_rejected(bad):
    """Silently dropping a malformed item means silently not reviewing it."""
    with pytest.raises(CursorError):
        filter_new(empty_cursor("k"), [bad])


def test_advance_rejects_malformed_items_too():
    with pytest.raises(CursorError):
        advance(empty_cursor("k"), [{"id": "a"}])


# --- key handling ------------------------------------------------------------

@pytest.mark.parametrize("key,expected", [
    ("hal/ace-review", "hal--ace-review.cursor.json"),
    ("ada/canopy-run-review", "ada--canopy-run-review.cursor.json"),
    ("Weird Key/With Spaces", "weird-key--with-spaces.cursor.json"),
])
def test_key_slugs_to_a_stable_filename(key, expected):
    assert slug_for_key(key) == expected


def test_key_slug_is_stable_across_calls():
    assert slug_for_key("hal/ace-review") == slug_for_key("hal/ace-review")


@pytest.mark.parametrize("key", ["", "   ", "/", "../escape"])
def test_bad_keys_are_rejected(key):
    with pytest.raises(CursorError):
        slug_for_key(key)


# --- stores ------------------------------------------------------------------

def test_local_store_round_trips(tmp_path):
    from orchestrator.work_cursor import LocalCursorStore

    s = LocalCursorStore(tmp_path)
    assert s.read("hal/ace-review")["runs"] == 0          # absent == empty, not an error
    s.write("hal/ace-review", advance(empty_cursor("hal/ace-review"),
                                      [item("a", "2026-07-01T00:00:00Z")]))
    got = s.read("hal/ace-review")
    assert got["runs"] == 1 and got["seen"] == {"a": "2026-07-01T00:00:00Z"}
    assert s.list_keys() == ["hal--ace-review.cursor.json"]


def test_local_store_two_keys_do_not_collide(tmp_path):
    """Concurrent skills must not share a document — one advance would clobber the other."""
    from orchestrator.work_cursor import LocalCursorStore

    s = LocalCursorStore(tmp_path)
    s.write("hal/ace-review", advance(empty_cursor("hal/ace-review"), [item("a", "2026-07-01T00:00:00Z")]))
    s.write("ada/canopy-run-review", advance(empty_cursor("ada/canopy-run-review"), [item("b", "2026-07-02T00:00:00Z")]))
    assert list(s.read("hal/ace-review")["seen"]) == ["a"]
    assert list(s.read("ada/canopy-run-review")["seen"]) == ["b"]


def test_local_store_corrupt_cursor_fails_loud(tmp_path):
    """A truncated cursor must not silently read as 'nothing processed yet' — that
    quietly re-analyzes the entire history and looks like the tool working."""
    from orchestrator.work_cursor import LocalCursorStore, slug_for_key

    (tmp_path / slug_for_key("k")).write_text("{not json")
    with pytest.raises(CursorError):
        LocalCursorStore(tmp_path).read("k")


def test_drive_store_reads_writes_through_gog(tmp_path, monkeypatch):
    """The Drive store shells out to gog with the AGENT's identity — that is what makes
    the capability generic across agents rather than per-agent code."""
    import subprocess as sp

    from orchestrator import work_cursor as wc

    calls = []

    class FakeIdentity:
        account, client, root_folder, slug = "hal@dimagi-ai.com", "canopy", "ROOT", "hal"

    monkeypatch.setattr("orchestrator.agent_gdoc.resolve_gdoc_identity", lambda r: FakeIdentity())
    monkeypatch.setattr("orchestrator.agent_gdoc.resolve_subfolder", lambda *a, **k: "STATEFOLDER")

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:3] == ["drive", "ls"]:
            return sp.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        return sp.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    store = wc.DriveCursorStore(tmp_path, runner=fake_run)
    assert store.read("hal/ace-review")["runs"] == 0      # no file yet -> empty cursor
    store.write("hal/ace-review", empty_cursor("hal/ace-review"))

    upload = [c for c in calls if c[1:3] == ["drive", "upload"]]
    assert upload, "never uploaded the cursor"
    assert "--parent" in upload[0] and "STATEFOLDER" in upload[0]
    assert "hal--ace-review.cursor.json" in upload[0]
    assert all("--account" in c and "hal@dimagi-ai.com" in c for c in calls)


def test_drive_store_surfaces_gog_failure(tmp_path, monkeypatch):
    import subprocess as sp

    from orchestrator import work_cursor as wc

    class FakeIdentity:
        account, client, root_folder, slug = "hal@dimagi-ai.com", "canopy", "ROOT", "hal"

    monkeypatch.setattr("orchestrator.agent_gdoc.resolve_gdoc_identity", lambda r: FakeIdentity())
    monkeypatch.setattr("orchestrator.agent_gdoc.resolve_subfolder", lambda *a, **k: "STATEFOLDER")
    store = wc.DriveCursorStore(
        tmp_path, runner=lambda cmd, **kw: sp.CompletedProcess(cmd, 1, stdout="", stderr="auth expired"))
    with pytest.raises(CursorError, match="auth expired"):
        store.read("hal/ace-review")


# --- CLI ---------------------------------------------------------------------

def _cli():
    from click.testing import CliRunner

    from orchestrator.cli import main
    return CliRunner(), main


def test_cli_round_trip_second_run_sees_only_the_new_item(tmp_path):
    """The whole point, end to end: run once, run again, only new work comes back."""
    import json as json_mod

    runner, main = _cli()
    batch1 = json_mod.dumps([{"id": "s1", "ts": "2026-07-01T00:00:00Z"}])
    args = ["--local", str(tmp_path)]

    r = runner.invoke(main, ["cursor", "since", "hal/ace-review", *args], input=batch1)
    assert r.exit_code == 0, r.output
    assert [i["id"] for i in json_mod.loads(r.output)] == ["s1"]

    r = runner.invoke(main, ["cursor", "bump", "hal/ace-review", *args], input=batch1)
    assert r.exit_code == 0, r.output
    assert json_mod.loads(r.output)["runs"] == 1

    both = json_mod.dumps([{"id": "s1", "ts": "2026-07-01T00:00:00Z"},
                           {"id": "s2", "ts": "2026-07-02T00:00:00Z"}])
    r = runner.invoke(main, ["cursor", "since", "hal/ace-review", *args], input=both)
    assert [i["id"] for i in json_mod.loads(r.output)] == ["s2"]


def test_cli_read_and_list(tmp_path):
    import json as json_mod

    runner, main = _cli()
    args = ["--local", str(tmp_path)]
    runner.invoke(main, ["cursor", "bump", "hal/ace-review", *args],
                  input=json_mod.dumps([{"id": "s1", "ts": "2026-07-01T00:00:00Z"}]))
    r = runner.invoke(main, ["cursor", "read", "hal/ace-review", *args])
    assert json_mod.loads(r.output)["seen"] == {"s1": "2026-07-01T00:00:00Z"}
    r = runner.invoke(main, ["cursor", "list", *args])
    assert "hal--ace-review.cursor.json" in r.output


def test_cli_reset_is_gated(tmp_path):
    """Resetting makes the next run re-process everything — an expensive accident."""
    import json as json_mod

    runner, main = _cli()
    args = ["--local", str(tmp_path)]
    runner.invoke(main, ["cursor", "bump", "k", *args],
                  input=json_mod.dumps([{"id": "s1", "ts": "2026-07-01T00:00:00Z"}]))

    r = runner.invoke(main, ["cursor", "reset", "k", *args])
    assert r.exit_code != 0 and "--yes" in r.output
    assert json_mod.loads(runner.invoke(main, ["cursor", "read", "k", *args]).output)["seen"]

    r = runner.invoke(main, ["cursor", "reset", "k", "--yes", *args])
    assert r.exit_code == 0, r.output
    assert json_mod.loads(runner.invoke(main, ["cursor", "read", "k", *args]).output)["seen"] == {}


def test_cli_rejects_items_missing_a_stamp(tmp_path):
    runner, main = _cli()
    r = runner.invoke(main, ["cursor", "since", "k", "--local", str(tmp_path)],
                      input='[{"id": "s1"}]')
    assert r.exit_code != 0
    assert "id and a ts" in r.output


def test_cli_rejects_non_array_input(tmp_path):
    runner, main = _cli()
    r = runner.invoke(main, ["cursor", "since", "k", "--local", str(tmp_path)], input='{"id":"s1"}')
    assert r.exit_code != 0 and "JSON array" in r.output
