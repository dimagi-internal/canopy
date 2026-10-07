"""Fixer-diff gate (canopy#786) — the positives are lines DDD fixers actually shipped
to connect-labs on the Spark and supply runs (2026-10); the negatives are ordinary
product code the gate must leave alone."""
from __future__ import annotations

import json
import subprocess

from scripts.ddd import fix_gate, judge_gate, loop_config
from scripts.ddd.fix_gate import GateConfig, gate


def _diff(path: str, added: list[str], *, removed: list[str] = (), context: list[str] = (), new: bool = False) -> str:
    head = [f"diff --git a/{path} b/{path}"]
    if new:
        head += ["new file mode 100644", "--- /dev/null"]
    else:
        head += [f"--- a/{path}"]
    head += [f"+++ b/{path}", f"@@ -1,{len(context) + len(removed)} +1,{len(context) + len(added)} @@"]
    body = [f" {c}" for c in context] + [f"-{r}" for r in removed] + [f"+{a}" for a in added]
    return "\n".join(head + body) + "\n"


def _checks(result: dict) -> list[str]:
    return [f["check"] for f in result["findings"]]


# --- the issue's own examples ---------------------------------------------------


def test_sophie_docstring_is_a_persona_and_a_fix_narration():
    diff = _diff(
        "connect_labs/supply_chain/history/timeline.py",
        [
            "def _fold_bookkeeping(entries) -> list[Entry]:",
            '    """Bookkeeping lines that say the same thing on one day read as one line counting the records.',
            "",
            "    Six invitations sent on 19 Sep by Sophie were six lines differing only in the",
            '    supplier ("Outreach · Kanem Foods Ltd · recorded: sent 19 Sep"); they read as',
            '    one: "Outreach · 6 suppliers · recorded: sent 19 Sep".',
            '    """',
            "    out = []",
        ],
    )
    r = gate(diff, terms=["Sophie", "Okafor"])
    assert r["status"] == "fail"
    assert "persona_name" in _checks(r)
    narr = [f for f in r["findings"] if f["check"] == "fix_comment"]
    assert narr and "before/after story" in narr[0]["why"]
    # the docstring is a comment, not user-visible prose
    assert "user_prose" not in _checks(r)


def test_terms_gaps_frozenset_is_a_rule_keyed_on_wording():
    diff = _diff(
        "connect_labs/supply_chain/moves.py",
        ['_TERMS_GAPS = frozenset({"tender duty terms", "duty terms"})'],
    )
    r = gate(diff)
    assert _checks(r) == ["literal_rule"]
    assert "set of phrases" in r["findings"][0]["why"]


def test_keyword_regex_guessing_meaning_from_wording():
    for line in (
        "const REPEAT = /repeat|duplicat|identical|same/i;",
        "if (/repeat|duplicat|identical|same/i.test(flag.label)) {",
        "REPEAT = re.compile(r'repeat|duplicat|identical|same', re.I)",
    ):
        ext = "py" if "re.compile" in line else "js"
        r = gate(_diff(f"connect_labs/workflow/templates/render.{ext}", [line]))
        assert _checks(r) == ["literal_rule"], line
        assert "keyword regex" in r["findings"][0]["why"]


def test_branch_on_a_free_text_literal():
    for path, line in (
        ("app/rules.py", "    if gap == 'tender duty terms':"),
        ("app/rules.py", "    if label.startswith('Short of target'):"),
        ("app/rules.py", "    if 'duty terms' in gap_name:"),
        ("app/rules.js", "  if (oneCov.indexOf('all ') === 0) {"),
    ):
        assert _checks(gate(_diff(path, [line]))) == ["literal_rule"], line


def test_spark_explanatory_copy_is_user_prose():
    diff = _diff(
        "connect_labs/workflow/templates/indicator_report_render.js",
        [
            "              'Short of target, not yet off target' +",
            '          right="Click a column name for its definition · ↕ sorts"',
        ],
        removed=['          right="Click a column name for its definition · the arrow sorts"'],
    )
    r = gate(diff)
    assert _checks(r) == ["user_prose", "user_prose"]


def test_comment_that_tells_the_story_of_a_fix():
    diff = _diff(
        "connect_labs/supply_chain/moves.py",
        [
            "# A quote's missing fact is NOT a move (rules above), so its chip never borrows the moves'",
            '# "to do": a To do count of 0 beside "duty terms · to do" read as a contradiction. Theirs',
            "# keep the waiting words.",
        ],
    )
    assert _checks(gate(diff)) == ["fix_comment"]


def test_comment_naming_the_loop():
    diff = _diff(
        "connect_labs/supply_chain/procurement/views.py",
        ["        # Every draft's why is a labelled row (DDD 003 batch 2; sheets b5), never a sentence."],
    )
    r = gate(diff)
    assert _checks(r) == ["fix_comment"] and "names the loop" in r["findings"][0]["why"]


def test_long_template_comment_explaining_the_ui():
    diff = _diff(
        "connect_labs/templates/supply_chain/procurement/tender_detail.html",
        [
            "        {% comment %}Who set the terms and when, and the waiver's evidence: each its own field",
            "        beside the terms, not run on after them.{% endcomment %}",
            '        <span class="chip">{{ terms }}</span>',
        ],
    )
    r = gate(diff)
    assert _checks(r) == ["fix_comment"]
    assert "template comment" in r["findings"][0]["why"]


def test_a_hunk_that_starts_inside_a_template_comment():
    # The opener is above the hunk; the closer below the edited line tells the
    # gate the added line is comment text, not a sentence on the page.
    diff = _diff(
        "connect_labs/templates/supply_chain/_moves.html",
        ["fact that table does not carry, still shows under it). A move to a drafted email opens"],
        context=["count, one .move-item per move with its rule label and the link that makes it."],
    ).rstrip("\n") + "\n the draft panel at it.\n {% endcomment %}\n"
    assert "user_prose" not in _checks(gate(diff))


def test_demo_named_test_files():
    for name in ("test_sophie_batch5.py", "test_unanswered_round_1004_b4.py", "test_sheets_b3_compare.py",
                 "test_unanswered_round_ddd003_batch2.py", "test_unanswered_round_v11_duty_terms.py"):
        r = gate(_diff(f"connect_labs/supply_chain/tests/{name}", ["def test_x():", "    assert True"], new=True),
                 terms=["Sophie"])
        assert _checks(r) == ["demo_test"], name


# --- what the gate must leave alone ---------------------------------------------


def test_ordinary_product_code_passes():
    diff = _diff(
        "connect_labs/supply_chain/procurement/status.py",
        [
            "STATUS_LABELS = {'draft': 'Draft', 'open': 'Open', 'closed': 'Closed'}",
            "    if tender.duty_terms == 'buyer_waiver':",
            "        return Chip(label='Waiting', tone='amber')",
            "    logger.warning('tender %s has no supplier we could reach in time for the close', tender.pk)",
            "    raise ValueError('a document cannot hold for a period that ends before it starts')",
            "    # Sum the landed cost per supplier.",
            '    cls = "px-4 py-2 text-sm text-gray-700 border-t border-gray-100 space-y-1"',
        ],
    )
    r = gate(diff)
    assert r["status"] == "pass", r["findings"]


def test_template_labels_and_script_comments_pass():
    diff = _diff(
        "connect_labs/templates/supply_chain/order_detail.html",
        [
            '<td class="px-4 py-2">{% if x %}<span>included in the price</span>{% else %}Not yet approved{% endif %}</td>',
            "  // Fonts reflow the page after this runs, so land on the target again once they have.",
        ],
    )
    assert gate(diff)["status"] == "pass"


def test_tests_seeds_docs_and_existing_strings_are_out_of_scope():
    parts = [
        _diff("scripts/walkthroughs/supply-sophie/seed.py", ["NAME = 'Sophie Okafor'"]),
        _diff("connect_labs/supply_chain/management/commands/seed_demo.py", ["help = 'Seed the demo for Sophie'"]),
        _diff("docs/walkthroughs/supply-sophie.why_brief.yaml", ["problem: Sophie cannot see which quotes stand"]),
        _diff("connect_labs/supply_chain/tests/test_moves.py", ["    assert word == 'ours to settle the duty terms now'"]),
        # Re-indenting a line does not make its sentence new.
        _diff(
            "connect_labs/workflow/templates/render.js",
            ["      title: 'No report has enough cases to score this yet.',"],
            removed=["    title: 'No report has enough cases to score this yet.',"],
        ),
    ]
    r = gate("".join(parts), terms=["Sophie"])
    assert r["status"] == "pass", r["findings"]


def test_allow_waives_in_config_not_in_the_product():
    diff = _diff("app/views.py", ["    help_text = 'Upload the file or link to where it already lives online'"])
    assert gate(diff, cfg=GateConfig())["status"] == "fail"
    r = gate(diff, cfg=GateConfig(allow=("Upload the file",)))
    assert r["status"] == "pass" and len(r["waived"]) == 1


def test_persona_terms_come_from_the_spec(tmp_path):
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "personas:\n  pm:\n    name: Sophie Okafor\n    role: procurement\n    color: '#000'\n    intro: x\n"
        "  mo:\n    name: Dr. Amina Bello\n    role: m\n    color: '#111'\n    intro: y\n"
    )
    assert fix_gate.persona_terms(spec) == ["Sophie", "Okafor", "Amina", "Bello"]


def test_cli_against_a_real_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "moves.py").write_text("X = 1\n")
    run("add", ".")
    run("commit", "-qm", "base")
    run("checkout", "-qb", "fix")
    (repo / "moves.py").write_text('X = 1\n_TERMS_GAPS = frozenset({"tender duty terms", "duty terms"})\n')
    run("commit", "-qam", "fix")
    out = tmp_path / "out"
    out.mkdir()
    rc = fix_gate._main([str(repo), "--base", "main", "--head", "fix", "--run-dir", str(out)])
    assert rc == 1
    data = json.loads((out / fix_gate.OUT_FILE).read_text())
    assert data["status"] == "fail" and data["counts"]["literal_rule"] == 1


# --- wiring ----------------------------------------------------------------------


def test_judge_gate_refuses_a_flagged_batch():
    d = judge_gate.decide({"status": "ready"}, {"fix_gate": "fail"})
    assert d["judge"] is False and d["action"] == "rework_fix"
    assert judge_gate.decide({"status": "ready"}, {"fix_gate": "pass"})["judge"] is True


def test_stamp_records_the_verdict_on_state():
    from scripts.ddd.schemas.models import RunState

    s = RunState(run_id="r", narrative_slug="n")
    fix_gate.stamp(s, gate(_diff("a.py", ['S = {"a b", "c d"}'])), head="abc")
    assert s.fix_gate["status"] == "fail" and s.fix_gate["head"] == "abc"


def test_config_block_parses():
    cfg = loop_config.parse(
        {"fix_gate": {"prose_words": 9, "demo_terms": "Spark", "allow": ["ok", "(bad"], "skip_paths": ["^mcp/"]}}
    ).fix_gate
    assert cfg.prose_words == 9 and cfg.demo_terms == ("Spark",)
    assert cfg.allow == ("ok",) and cfg.skip_paths == ("^mcp/",)
    assert loop_config.parse({}).fix_gate.prose_words == 7
