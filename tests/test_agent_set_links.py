"""`canopy agent set` must be able to fix a task's links.

Links are the task field most likely to rot — they point at runs, run summaries and
docs, which advance — and until this landed `agent set` had no `--links` option at all.
The only CLI surface that accepted links was `agent add`, which builds a COMPLETE task
dict and pushed it through the old task sync, so using it to correct one stale link blanked
`next_action`, `notes`, `owner`, `assigned`, `status` and `due` unless the caller
re-supplied every one of them. The one field that rots had no safe update path.

Found during an ACE turn on 2026-08-19 (dimagi-internal/canopy#508), correcting a board
card whose run-summary link still pointed at a superseded run.
"""
import pytest

from orchestrator.agent_cli import _appended_links, parse_task_links


class FakeClient:
    """Only the surface `_appended_links` touches."""

    def __init__(self, tasks):
        self._tasks = tasks

    def list_tasks(self):
        return self._tasks


def test_append_keeps_the_links_already_on_the_card():
    """The whole point: adding one link must not drop the others.

    The board's PATCH replaces `links` wholesale, so an append that forgot to read
    first would silently delete every existing link — the exact failure the replace-only
    path already had.
    """
    client = FakeClient([{"ext_id": "T91", "links": [{"label": "Thread", "url": "https://mail/x"}]}])
    out = _appended_links(client, "T91", "Run summary|https://labs/run/2")
    assert [l["url"] for l in out] == ["https://mail/x", "https://labs/run/2"]
    assert out[0]["label"] == "Thread"


def test_append_is_idempotent_on_url_so_reruns_do_not_stack_duplicates():
    """A turn that re-attaches the same artifact is the common case, not an error."""
    client = FakeClient([{"ext_id": "T91", "links": [{"label": "Run", "url": "https://labs/run/2"}]}])
    out = _appended_links(client, "T91", "Run summary|https://labs/run/2")
    assert len(out) == 1
    assert out[0]["label"] == "Run", "first label wins; a re-add must not relabel"


def test_append_onto_a_card_with_no_links_yet():
    client = FakeClient([{"ext_id": "T91"}])
    assert _appended_links(client, "T91", "https://labs/run/2") == [
        {"label": "link", "url": "https://labs/run/2"}
    ]


def test_append_ignores_other_cards_links():
    """Task ids are matched exactly — appending to one card must not inherit another's."""
    client = FakeClient([
        {"ext_id": "T90", "links": [{"label": "Other", "url": "https://other"}]},
        {"ext_id": "T91", "links": [{"label": "Mine", "url": "https://mine"}]},
    ])
    out = _appended_links(client, "T91", "New|https://new")
    assert [l["url"] for l in out] == ["https://mine", "https://new"]


def test_links_and_append_link_are_mutually_exclusive():
    from click.testing import CliRunner

    from orchestrator.agent_cli import agent

    res = CliRunner().invoke(agent, [
        "set", "--slug", "ace", "--task-id", "T1",
        "--links", "a|https://a", "--append-link", "b|https://b",
    ])
    assert res.exit_code != 0
    assert "not both" in res.output


def test_empty_links_string_clears_rather_than_being_ignored():
    """`--links ""` is a real instruction (drop them all); only omitting the flag is a no-op.

    This is why the option defaults to None, not "": patch_task drops None fields, so a
    "" default would have made every unrelated `agent set` call silently wipe the links.
    """
    assert parse_task_links("") == []


class TestAnUnparseableLinkRaisesInsteadOfVanishing:
    """A write that reports success and stores nothing is the worst shape a CLI has.

    `parse_task_links` used to end at `elif part.startswith("http")`, so anything that
    matched neither branch fell off the end and was skipped. `--append-link "label=url"`
    — the wrong separator, and the one people reach for first — therefore wrote no link,
    printed the patched task as JSON, and exited 0. Nothing tells the caller to re-read
    the card, so the link is just absent later with no error anywhere to explain it.
    """

    def test_the_wrong_separator_raises(self):
        with pytest.raises(Exception) as exc:
            parse_task_links("PR #1402=https://github.com/org/repo/pull/1402")
        assert "label|url" in str(exc.value)
        # The message must say the write did not happen — the caller's next move
        # differs entirely between "rejected" and "partially applied".
        assert "Nothing was written" in str(exc.value)

    def test_a_bare_label_with_no_url_raises(self):
        with pytest.raises(Exception):
            parse_task_links("just a label")

    def test_one_bad_part_rejects_the_WHOLE_write(self):
        """No partial application: a card half-updated is harder to reason about than
        one not updated at all, and the caller cannot tell which half landed."""
        with pytest.raises(Exception):
            parse_task_links("Good|https://example.com/1, Bad=https://example.com/2")

    def test_the_valid_forms_still_parse(self):
        """The guard must not narrow what already worked."""
        assert parse_task_links("A|https://x/1, https://x/2") == [
            {"label": "A", "url": "https://x/1"},
            {"label": "link", "url": "https://x/2"},
        ]
        assert parse_task_links("") == []
        assert parse_task_links(None) == []

    def test_appending_a_bad_link_does_not_drop_the_existing_ones(self):
        """The read-modify-write path is where a silent drop would be worst: it rewrites
        `links` wholesale, so failing loudly BEFORE the PATCH is what protects the card."""
        client = FakeClient([{"ext_id": "T91", "links": [{"label": "Thread", "url": "https://mail/x"}]}])
        with pytest.raises(Exception):
            _appended_links(client, "T91", "Bad=https://example.com/2")


class TestCommasInALabelOrUrlAreKept:
    """A comma inside a label or URL is not a link separator.

    The parser used to `split(",")` the whole cell before looking for `|`, so a label with
    a comma was cut in half and its first half rejected as an unparseable link. Nothing was
    written (the guard above held), but there was no way to write the label you meant.
    Seen twice in eva (2026-09-27, 2026-09-30); both calls were retried with the commas
    stripped out of the label.
    """

    def test_a_label_with_a_comma(self):
        assert parse_task_links("Deck source (draft 1, draft 2)|https://d/1") == [
            {"label": "Deck source (draft 1, draft 2)", "url": "https://d/1"},
        ]

    def test_the_brief_repro_parentheses_and_a_comma_then_a_second_link(self):
        assert parse_task_links("A (x, y)|https://a.example, B|https://b.example") == [
            {"label": "A (x, y)", "url": "https://a.example"},
            {"label": "B", "url": "https://b.example"},
        ]

    def test_two_links_whose_labels_both_have_commas(self):
        """The 2026-09-30 eva call, verbatim in shape."""
        cell = ("Slide deck (Dimagi brand, canonical)|https://claude.ai/artifact/C, "
                "Slide deck (neutral, retired 2026-09-30)|https://claude.ai/artifact/D")
        assert parse_task_links(cell) == [
            {"label": "Slide deck (Dimagi brand, canonical)", "url": "https://claude.ai/artifact/C"},
            {"label": "Slide deck (neutral, retired 2026-09-30)", "url": "https://claude.ai/artifact/D"},
        ]

    def test_bare_url_plus_a_labelled_link_with_commas(self):
        assert parse_task_links("https://x/1, Run (a, b)|https://x/2") == [
            {"label": "link", "url": "https://x/1"},
            {"label": "Run (a, b)", "url": "https://x/2"},
        ]

    def test_a_comma_in_a_url_query_string_stays_in_the_url(self):
        assert parse_task_links("Q|https://x?ids=1,2, https://y?ids=3,4") == [
            {"label": "Q", "url": "https://x?ids=1,2"},
            {"label": "link", "url": "https://y?ids=3,4"},
        ]

    def test_when_ambiguous_the_url_wins(self):
        """`b` could be the tail of the first URL or the front of the second label; a
        broken URL is a dead link, so the URL keeps it."""
        assert parse_task_links("a|https://x,b,c|https://y") == [
            {"label": "a", "url": "https://x,b"},
            {"label": "c", "url": "https://y"},
        ]

    def test_the_wrong_separator_still_raises_after_a_good_link(self):
        """The space after the comma marks a separator, so the bad part is not swallowed
        into the previous URL — the #579 guard holds."""
        with pytest.raises(Exception) as exc:
            parse_task_links("Good|https://example.com/1, Bad=https://example.com/2")
        assert "Bad=https://example.com/2" in str(exc.value)

    def test_a_label_with_a_comma_and_no_url_raises_naming_the_whole_label(self):
        with pytest.raises(Exception) as exc:
            parse_task_links("A (x, y), https://b")
        assert "A (x, y)" in str(exc.value)
