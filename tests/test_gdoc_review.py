"""Pins `canopy gdoc check` (gdoc_review.py) and the converter fixes that shipped with it.

The review and the converter fixes came out of eva's skills/gdoc-review, which caught each
of these on a real deliverable. Every case below is a defect that reached, or nearly
reached, a reader.
"""
from __future__ import annotations

from orchestrator import gdoc_review
from orchestrator.agent_gdoc import md_to_html, replace_degradations, unwrap_markdown

FILLER = "x" * 300


def _html(body: str) -> str:
    return f"<html><head><style>.c{{}}</style></head><body>{body}<p>{FILLER}</p></body></html>"


# ---- review_html -------------------------------------------------------------------------

def test_clean_doc_passes():
    r = gdoc_review.review_html(_html('<p><span style="font-style:normal">hi</span></p>'))
    assert r["passed"] and not r["fails"]


def test_empty_doc_fails_even_though_nothing_else_is_wrong():
    """An emptied doc trips no defect detector, so it used to score a clean pass."""
    r = gdoc_review.review_html("<html><body><p>tiny</p></body></html>")
    assert not r["passed"] and r["fails"][0].startswith("EMPTY DOC")


def test_replace_sentinel_left_behind_fails():
    r = gdoc_review.review_html(_html(f"<p>{gdoc_review.BODY_SENTINEL}</p>"))
    assert any("sentinel" in f for f in r["fails"])


def test_italic_bleed_is_a_ratio_not_a_count():
    """A brief that quotes its sources has many italic runs and is fine (2026-07-26:
    34 quotes failed a `count > 1` rule); a body that is mostly italic is the bleed."""
    quotes = '<span style="font-style:italic">q</span>' * 10
    normal = '<span style="font-style:normal">n</span>' * 40
    assert gdoc_review.review_html(_html(quotes + normal))["passed"]
    bleed = '<span style="font-style:italic">q</span>' * 40
    r = gdoc_review.review_html(_html(bleed + normal[:200]))
    assert any(f.startswith("ITALIC BLEED") for f in r["fails"])


def test_leaked_markdown_in_visible_text_fails():
    r = gdoc_review.review_html(_html("<p>a **bold** leak</p>"))
    assert any("MARKDOWN LEAK" in f for f in r["fails"])


def test_paired_single_asterisks_fail_but_a_lone_one_does_not():
    assert not gdoc_review.review_html(_html("<p>an *italic* leak</p>"))["passed"]
    assert gdoc_review.review_html(_html("<p>footnote* only</p>"))["passed"]


def test_expected_links_and_blocks():
    doc = _html('<p><a href="https://a.org">a</a></p><table></table>')
    assert gdoc_review.review_html(doc, expect_links=1, expect_blocks=1)["passed"]
    r = gdoc_review.review_html(doc, expect_links=2, expect_blocks=2)
    assert len(r["fails"]) == 2


def test_font_and_size_only_warn_and_only_when_asked():
    doc = _html('<p style="font-family:Times;font-size:9pt">t</p>')
    assert not gdoc_review.review_html(doc)["warns"]
    r = gdoc_review.review_html(doc, font="Arial", body_size="11")
    assert r["passed"] and len(r["warns"]) == 2


def test_style_defaults_come_from_agent_json(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agent.json").write_text(
        '{"gdoc_style": {"font": "Arial", "body_size": 11}}')
    assert gdoc_review.style_defaults(tmp_path) == {"font": "Arial", "body_size": "11"}
    assert gdoc_review.style_defaults(None) == {}


def test_a_pass_writes_the_receipt_and_a_fail_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_AGENT_HOME", str(tmp_path))

    def fake_gog(body):
        def run(cmd, **kw):
            out = cmd[cmd.index("--out") + 1]
            open(out, "w").write(body)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return run

    ok = gdoc_review.review_doc(slug="a", account="a@x", client="a", doc_id="DOC1",
                                runner=fake_gog(_html("<p>fine</p>")))
    assert ok["passed"] and (tmp_path / "gdoc-gate" / "passed" / "DOC1").exists()
    bad = gdoc_review.review_doc(slug="a", account="a@x", client="a", doc_id="DOC2",
                                 runner=fake_gog("<p>x</p>"))
    assert not bad["passed"] and not (tmp_path / "gdoc-gate" / "passed" / "DOC2").exists()


def test_an_export_failure_is_not_reviewed_and_writes_no_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_AGENT_HOME", str(tmp_path))

    def boom(cmd, **kw):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "403"})()

    r = gdoc_review.review_doc(slug="a", account="a@x", client="a", doc_id="D", runner=boom)
    assert r["passed"] is None and "403" in r["error"]
    assert not (tmp_path / "gdoc-gate" / "passed").exists()


# ---- converter fixes ---------------------------------------------------------------------

def test_pipe_tables_render_as_tables():
    """Measured 2026-10-04: a GFM table published fresh shipped as `| a | b | |---|---|`."""
    h = md_to_html("| Col 1 | Col 2 |\n|---|:---:|\n| **a** | b |\n\nafter")
    assert "<table>" in h and "<th>Col 1</th>" in h and "<td><strong>a</strong></td>" in h
    assert "|" not in h.replace("<table>", "")
    assert "<p>after</p>" in h


def test_a_lone_pipe_line_is_still_a_paragraph():
    assert "<table>" not in md_to_html("| not a table |\njust text")


def test_unwrap_joins_soft_wrapped_bold_for_the_replace_path():
    """Measured 2026-10-04: on `--replace`, `**bold that wraps\\nacross a line**` shipped
    with both `**` visible and the list item split in two."""
    src = ("Para **bold that wraps\nacross the line** end.\n\n"
           "- Item **wraps\n  here** too.\n- Next item\n")
    out = unwrap_markdown(src)
    assert "Para **bold that wraps across the line** end." in out
    assert "- Item **wraps here** too." in out
    assert "- Next item" in out


def test_unwrap_leaves_blocks_alone():
    src = "# Head\ntext\n\n```\na\nb\n```\n| a | b |\n|---|---|\n> q1\n> q2\n"
    out = unwrap_markdown(src)
    assert "```\na\nb\n```" in out
    assert "| a | b |\n|---|---|" in out
    assert "> q1\n> q2" in out
    assert "# Head\ntext" in out


def test_render_check_catches_leaked_bold_and_pipes():
    src = "a **b** c\n\n| x | y |\n|---|---|\n"
    assert replace_degradations(src, "a **b** c\n\n| x | y |\n") == []
    found = replace_degradations(src, "a \\*\\*b\\*\\* c\n\n\\| x \\| y \\|\n")
    assert len(found) == 2


def test_render_check_ignores_escapes_the_source_already_had():
    src = "literal \\*\\* stars\n"
    assert replace_degradations(src, "literal \\*\\* stars\n") == []
