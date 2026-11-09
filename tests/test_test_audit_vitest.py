"""Tests for the vitest adapter and framework auto-detection.

These tests use inline file fixtures + mocked `subprocess.run` so they don't
require a node toolchain. The vitest_adapter logic that depends on real
vitest output (collect/run) is covered by mocking the JSON written to
`--outputFile`.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.test_audit.adapters.vitest_adapter import (
    VitestAdapter,
    _extract_test_body,
    _extract_mock_targets,
    _extract_source_calls,
    _flatten_list_payload,
    _find_matching_close,
    _is_in_string_or_comment,
    _locate_test,
    _scan_tests,
)
from orchestrator.test_audit.framework import (
    FrameworkAdapter, _has_pytest, _has_vitest, detect_framework,
)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_detect_vitest_via_config_file(tmp_path: Path):
    (tmp_path / "vitest.config.ts").write_text("export default {}\n")
    assert _has_vitest(tmp_path) is True
    assert detect_framework(tmp_path).name == "vitest"


def test_detect_vitest_via_package_json_dep(tmp_path: Path):
    (tmp_path / "package.json").write_text(json.dumps({
        "devDependencies": {"vitest": "^1.0.0"}
    }))
    assert _has_vitest(tmp_path) is True
    assert detect_framework(tmp_path).name == "vitest"


def test_detect_pytest_via_pyproject(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert _has_pytest(tmp_path) is True
    assert detect_framework(tmp_path).name == "pytest"


def test_detect_explicit_override_wins(tmp_path: Path):
    # Repo looks like vitest, but caller asks for pytest.
    (tmp_path / "vitest.config.ts").write_text("export default {}\n")
    assert detect_framework(tmp_path, override="pytest").name == "pytest"


def test_detect_falls_back_to_pytest_for_unknown_repo(tmp_path: Path):
    assert detect_framework(tmp_path).name == "pytest"


def test_detect_unknown_framework_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown framework"):
        detect_framework(tmp_path, override="jest")


def test_adapter_satisfies_protocol():
    # Both concrete adapters should satisfy the runtime-checkable Protocol.
    from orchestrator.test_audit.adapters.pytest_adapter import PytestAdapter
    assert isinstance(VitestAdapter(), FrameworkAdapter)
    assert isinstance(PytestAdapter(), FrameworkAdapter)


# ---------------------------------------------------------------------------
# Body extraction (the brace-aware walker)
# ---------------------------------------------------------------------------

def test_find_matching_close_simple():
    src = "foo(bar(baz));"
    assert _find_matching_close(src, 3) == src.index(";") - 1


def test_find_matching_close_skips_strings():
    src = "f('he(llo' + \"wo)rld\")"
    # Open paren after f
    open_idx = src.index("(")
    close = _find_matching_close(src, open_idx)
    assert close is not None and src[close] == ")"
    # The matching close must be the very last char.
    assert close == len(src) - 1


def test_find_matching_close_skips_template_literals():
    src = "g(`a${nested(1)}b${other(2,3)}c`)"
    open_idx = src.index("(")
    close = _find_matching_close(src, open_idx)
    assert close == len(src) - 1


def test_find_matching_close_skips_line_comments():
    src = "h(\n  // ignore )\n  real\n)"
    open_idx = src.index("(")
    close = _find_matching_close(src, open_idx)
    assert close == src.rindex(")")


def test_extract_test_body_finds_test_call():
    src = '''import { it, expect } from 'vitest';
it('does X', () => {
  const x = compute(1, 2);
  expect(x).toBe(3);
});
'''
    body = _extract_test_body(src, line=2, leaf_name="does X")
    assert "expect(x).toBe(3)" in body
    assert "compute(1, 2)" in body


def test_extract_test_body_handles_multiline_test_name():
    src = '''it(
  'a multi-line test name',
  async () => {
    expect(true).toBe(true);
  },
);
'''
    body = _extract_test_body(src, line=1, leaf_name="a multi-line test name")
    assert "expect(true).toBe(true)" in body


# ---------------------------------------------------------------------------
# Static analysis
# ---------------------------------------------------------------------------

def test_extract_mock_targets_named_module():
    body = '''
vi.mock('../src/api');
vi.spyOn(repo, 'find');
vi.fn();
'''
    targets = _extract_mock_targets(body)
    assert "../src/api" in targets
    assert "find" in targets
    # vi.fn() with no args records the helper name itself
    assert "vi.fn" in targets


def test_extract_source_calls_drops_framework_noise():
    body = '''
expect(foo(1)).toBe(2);
describe('x', () => { Bar.do(); });
beforeEach(() => {});
console.log('z');
helper();
'''
    calls = _extract_source_calls(body)
    assert "foo" in calls
    assert "Bar.do" in calls
    assert "helper" in calls
    assert "expect" not in calls
    assert "describe" not in calls
    assert "console.log" not in calls


def test_analyze_counts_assertions_and_marks_real():
    body_file = (
        "import { it, expect, vi } from 'vitest';\n"
        "it('checks math', () => {\n"
        "  const r = sum(1, 2);\n"
        "  expect(r).toBe(3);\n"
        "  expect(r).toBeGreaterThan(0);\n"
        "  vi.mock('../math');\n"
        "});\n"
    )

    class FakeItem:
        nodeid = "x.test.ts::checks math"
        name = "checks math"
        line = 2
        file = None  # filled below
        classname = None

    fp = Path("/tmp/_vitest_analyze_test.ts")
    fp.write_text(body_file)
    FakeItem.file = fp
    try:
        result = VitestAdapter().analyze(FakeItem())
    finally:
        fp.unlink()

    assert result.assertion_count == 2
    assert result.has_real_assertion is True
    assert "../math" in result.mock_targets
    assert "sum" in result.source_funcs_referenced


# ---------------------------------------------------------------------------
# vitest list payload normalization
# ---------------------------------------------------------------------------

def test_flatten_list_top_level_array():
    data = [{"file": "a.test.ts", "name": "x"},
            {"file": "b.test.ts", "name": "y"}]
    assert _flatten_list_payload(data) == data


def test_flatten_list_jest_compatible_shape():
    data = {
        "testResults": [
            {
                "name": "/abs/foo.test.ts",
                "assertionResults": [
                    {"fullName": "outer > inner > does X", "status": "passed"},
                    {"title": "alone", "status": "passed"},
                ],
            }
        ]
    }
    out = _flatten_list_payload(data)
    assert {"file": "/abs/foo.test.ts", "name": "outer > inner > does X"} in out
    assert {"file": "/abs/foo.test.ts", "name": "alone"} in out


def test_flatten_list_task_tree():
    data = {
        "files": [
            {
                "filepath": "/abs/foo.test.ts",
                "type": "suite",
                "name": "",
                "tasks": [
                    {
                        "type": "suite",
                        "name": "outer",
                        "tasks": [
                            {"type": "test", "name": "leaf-a"},
                            {
                                "type": "suite",
                                "name": "inner",
                                "tasks": [
                                    {"type": "test", "name": "leaf-b"},
                                ],
                            },
                        ],
                    }
                ],
            }
        ]
    }
    out = _flatten_list_payload(data)
    names = {(d["file"], d["name"]) for d in out}
    assert ("/abs/foo.test.ts", "outer > leaf-a") in names
    assert ("/abs/foo.test.ts", "outer > inner > leaf-b") in names


# ---------------------------------------------------------------------------
# collect() — both list-output and fallback paths
# ---------------------------------------------------------------------------

def test_collect_via_vitest_list(tmp_path: Path):
    (tmp_path / "vitest.config.ts").write_text("export default {}\n")
    test_file = tmp_path / "math.test.ts"
    test_file.write_text(
        "import { it, expect } from 'vitest';\n"
        "describe('math', () => {\n"
        "  it('adds', () => { expect(1+1).toBe(2); });\n"
        "});\n"
    )

    listed = [{"file": str(test_file), "name": "math > adds"}]

    def fake_run(cmd, **kwargs):
        # Find the --outputFile=... arg and write the listed payload.
        for a in cmd:
            if a.startswith("--outputFile="):
                Path(a.split("=", 1)[1]).write_text(json.dumps(listed))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("orchestrator.test_audit.adapters.vitest_adapter.subprocess.run",
               side_effect=fake_run):
        items = VitestAdapter().collect(tmp_path)

    assert len(items) == 1
    item = items[0]
    assert item.nodeid.endswith("math.test.ts::math::adds")
    assert item.name == "adds"
    assert item.line == 3  # the line where `it('adds', ...)` lives


def test_collect_falls_back_when_vitest_unavailable(tmp_path: Path):
    test_file = tmp_path / "demo.test.ts"
    test_file.write_text(
        "it('alpha', () => {});\n"
        "test('beta', () => {});\n"
    )

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no npx")

    with patch("orchestrator.test_audit.adapters.vitest_adapter.subprocess.run",
               side_effect=fake_run):
        items = VitestAdapter().collect(tmp_path)

    names = {it.name for it in items}
    assert names == {"alpha", "beta"}


def test_collect_fallback_finds_tests_in_test_directory(tmp_path: Path):
    """Regression: fallback scan must walk into `test/`, `tests/`, `__tests__/`.

    `_fallback_scan` must use `_SKIP_TEST_DIRS` (which doesn't skip
    `test`/`tests`/`__tests__`), not `_SKIP_SRC_DIRS` (which does), or it
    silently misses co-located test directories — the standard JS/TS layout.
    """
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "lib").mkdir()
    (tmp_path / "test" / "lib" / "alpha.test.ts").write_text(
        "it('in-test-lib', () => {});\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "beta.test.ts").write_text(
        "it('in-tests', () => {});\n"
    )
    (tmp_path / "__tests__").mkdir()
    (tmp_path / "__tests__" / "gamma.test.ts").write_text(
        "it('in-double-underscore', () => {});\n"
    )

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no npx")

    with patch("orchestrator.test_audit.adapters.vitest_adapter.subprocess.run",
               side_effect=fake_run):
        items = VitestAdapter().collect(tmp_path)

    names = {it.name for it in items}
    assert names == {"in-test-lib", "in-tests", "in-double-underscore"}


# ---------------------------------------------------------------------------
# run() — JSON reporter parsing
# ---------------------------------------------------------------------------

def test_run_parses_jest_compatible_json(tmp_path: Path):
    (tmp_path / "vitest.config.ts").write_text("export default {}\n")
    test_file = tmp_path / "demo.test.ts"
    test_file.write_text("it('x', () => {});\n")

    payload = {
        "testResults": [
            {
                "name": str(test_file),
                "assertionResults": [
                    {
                        "fullName": "x",
                        "status": "passed",
                        "duration": 12.4,
                    },
                    {
                        "fullName": "y",
                        "status": "failed",
                        "duration": 0,
                        "failureMessages": ["expected 1 to be 2"],
                    },
                ],
            }
        ]
    }

    def fake_run(cmd, **kwargs):
        for a in cmd:
            if a.startswith("--outputFile="):
                Path(a.split("=", 1)[1]).write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("orchestrator.test_audit.adapters.vitest_adapter.subprocess.run",
               side_effect=fake_run):
        results = VitestAdapter().run(tmp_path)

    keys = list(results.keys())
    assert any(k.endswith("::x") for k in keys)
    assert any(k.endswith("::y") for k in keys)
    failed = next(v for k, v in results.items() if k.endswith("::y"))
    assert failed.status == "failed"
    assert "expected 1" in (failed.error or "")


# ---------------------------------------------------------------------------
# apply_skip() / apply_delete()
# ---------------------------------------------------------------------------

def test_apply_skip_rewrites_test_call(tmp_path: Path):
    file = tmp_path / "demo.test.ts"
    file.write_text(
        "import { it, expect } from 'vitest';\n"
        "describe('x', () => {\n"
        "  it('does the thing', () => { expect(1).toBe(1); });\n"
        "});\n"
    )
    ok = VitestAdapter().apply_skip(file, "does the thing", "tautology")
    assert ok is True
    new = file.read_text()
    assert "it.skip('does the thing'" in new
    assert "// audit: tautology" in new


def test_apply_skip_handles_only_modifier(tmp_path: Path):
    file = tmp_path / "demo.test.ts"
    file.write_text("it.only('foo', () => {});\n")
    ok = VitestAdapter().apply_skip(file, "foo", "redundant")
    assert ok is True
    new = file.read_text()
    assert "it.skip('foo'" in new


def test_apply_skip_idempotent_on_already_skipped(tmp_path: Path):
    file = tmp_path / "demo.test.ts"
    file.write_text("it.skip('foo', () => {});\n")
    ok = VitestAdapter().apply_skip(file, "foo", "noop")
    assert ok is False
    assert file.read_text() == "it.skip('foo', () => {});\n"


def test_apply_skip_returns_false_when_test_not_found(tmp_path: Path):
    file = tmp_path / "demo.test.ts"
    file.write_text("it('other', () => {});\n")
    ok = VitestAdapter().apply_skip(file, "missing", "x")
    assert ok is False


def test_apply_delete_unsupported(tmp_path: Path):
    adapter = VitestAdapter()
    assert adapter.supports_delete() is False
    file = tmp_path / "demo.test.ts"
    file.write_text("it('x', () => {});\n")
    assert adapter.apply_delete(file, "x") is False


# ---------------------------------------------------------------------------
# module_inventory() — pairing src files with .test files
# ---------------------------------------------------------------------------

def test_module_inventory_pairs_by_name(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "math.ts").write_text(
        "export function sum(a: number, b: number) { return a + b; }\n"
        "export const product = (a: number, b: number) => a * b;\n"
    )
    (tmp_path / "src" / "lonely.ts").write_text(
        "export function noTest() { return 1; }\n"
    )
    (tmp_path / "src" / "math.test.ts").write_text(
        "import { sum } from './math';\n"
        "it('sums', () => { expect(sum(1,2)).toBe(3); });\n"
    )

    inv = VitestAdapter().module_inventory(tmp_path)
    by_name = {m.module_name: m for m in inv}
    assert "math" in by_name and by_name["math"].has_test_file is True
    assert "lonely" in by_name and by_name["lonely"].has_test_file is False
    # 2 named exports in math.ts
    assert by_name["math"].public_func_count == 2


def test_module_inventory_falls_back_to_import_match(tmp_path: Path):
    """A test file that imports the module by relative path should count."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.ts").write_text("export function thing() { }\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "use-thing.test.ts").write_text(
        "import { thing } from '../src/thing';\n"
        "it('x', () => { thing(); });\n"
    )
    inv = VitestAdapter().module_inventory(tmp_path)
    by_name = {m.module_name: m for m in inv}
    assert by_name["thing"].has_test_file is True


# ---------------------------------------------------------------------------
# locate_test() leaf-name handling
# ---------------------------------------------------------------------------

def test_build_corpus_dispatches_to_vitest_adapter(tmp_path: Path):
    """End-to-end: corpus.build_corpus picks the vitest adapter when detected."""
    from orchestrator.test_audit.corpus import build_corpus

    (tmp_path / "vitest.config.ts").write_text("export default {}\n")
    test_file = tmp_path / "demo.test.ts"
    test_file.write_text(
        "import { it, expect } from 'vitest';\n"
        "it('checks', () => { expect(1).toBe(1); });\n"
    )

    listed = [{"file": str(test_file), "name": "checks"}]

    def fake_run(cmd, **kwargs):
        for a in cmd:
            if a.startswith("--outputFile="):
                Path(a.split("=", 1)[1]).write_text(json.dumps(listed))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("orchestrator.test_audit.adapters.vitest_adapter.subprocess.run",
               side_effect=fake_run):
        corpus = build_corpus(tmp_path, run_tests=False)

    assert corpus["framework"] == "vitest"
    assert corpus["test_count"] == 1
    assert corpus["tests"][0]["name"] == "checks"


def test_apply_from_dir_reads_framework_from_corpus(tmp_path: Path):
    """apply_from_dir picks vitest when corpus.yaml says so, even if repo signal is absent."""
    import yaml
    from orchestrator.test_audit.applier import apply_from_dir

    # No vitest.config.ts and no package.json — pure-Python repo signal.
    test_file = tmp_path / "demo.test.ts"
    test_file.write_text("it('x', () => {});\n")

    stamp_dir = tmp_path / ".canopy" / "test-audits" / "20260101-000000"
    stamp_dir.mkdir(parents=True)
    (stamp_dir / "corpus.yaml").write_text(yaml.safe_dump({
        "framework": "vitest",
        "tests": [{"nodeid": "demo.test.ts::x", "file": "demo.test.ts",
                   "name": "x", "line": 1}],
    }))
    (stamp_dir / "verdicts.yaml").write_text(yaml.safe_dump({
        "verdicts": [{"nodeid": "demo.test.ts::x", "score": 1,
                      "verdict": "prune", "reason_code": "tautology",
                      "reason": "no real assert"}],
    }))

    result = apply_from_dir(stamp_dir, repo=tmp_path, dry_run=True)
    # Vitest doesn't support delete -> plan downgrades to skip.
    assert any(c.action == "skip" for c in result.changes)
    assert not any(c.action == "delete" for c in result.changes)


# ---------------------------------------------------------------------------
# Sibling describes with the same leaf name must not collapse
# ---------------------------------------------------------------------------

def test_scan_tests_recovers_describe_path_for_shared_leaf():
    src = (
        "import { describe, it } from 'vitest';\n"
        "describe('outer', () => {\n"
        "  it('shared', () => {});\n"
        "});\n"
        "describe('inner', () => {\n"
        "  it('shared', () => {});\n"
        "});\n"
    )
    found = _scan_tests(src)
    triples = {(stack and stack[0], leaf) for _, stack, leaf in found}
    assert triples == {("outer", "shared"), ("inner", "shared")}


def test_scan_tests_handles_three_level_nesting():
    src = (
        "describe('a', () => {\n"
        "  describe('b', () => {\n"
        "    describe('c', () => {\n"
        "      it('leaf', () => {});\n"
        "    });\n"
        "  });\n"
        "});\n"
    )
    found = _scan_tests(src)
    assert len(found) == 1
    _, stack, leaf = found[0]
    assert stack == ["a", "b", "c"] and leaf == "leaf"


def test_scan_tests_skips_test_calls_inside_strings_and_comments():
    src = (
        "// it('commented out', () => {});\n"
        "/* it('block-commented', () => {}); */\n"
        "const s = \"it('inside string', () => {})\";\n"
        "it('real', () => {});\n"
    )
    found = _scan_tests(src)
    leaves = [t[2] for t in found]
    assert leaves == ["real"]


def test_collect_does_not_collapse_sibling_describes_with_shared_leaf(tmp_path: Path):
    """Regression: vitest list emits flat leaf names but the
    collector must still produce one TestItem per source occurrence.
    """
    (tmp_path / "vitest.config.ts").write_text("export default {}\n")
    test_file = tmp_path / "collide.test.ts"
    test_file.write_text(
        "describe('outer', () => {\n"
        "  it('shared', () => {});\n"
        "});\n"
        "describe('inner', () => {\n"
        "  it('shared', () => {});\n"
        "});\n"
    )

    # Simulate a vitest list reporter that lost describe nesting and
    # collapsed the duplicates to a single entry — the worst case.
    listed = [{"file": str(test_file), "name": "shared"}]

    def fake_run(cmd, **kwargs):
        for a in cmd:
            if a.startswith("--outputFile="):
                Path(a.split("=", 1)[1]).write_text(json.dumps(listed))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("orchestrator.test_audit.adapters.vitest_adapter.subprocess.run",
               side_effect=fake_run):
        items = VitestAdapter().collect(tmp_path)

    nodeids = sorted(it.nodeid for it in items)
    assert nodeids == [
        "collide.test.ts::inner::shared",
        "collide.test.ts::outer::shared",
    ]


def test_collect_backfills_dynamic_test_names_from_list(tmp_path: Path):
    """Tests whose names aren't static literals (e.g. it.each) won't show
    up in the source scan; the collector must still emit them using the
    name vitest reported.
    """
    (tmp_path / "vitest.config.ts").write_text("export default {}\n")
    test_file = tmp_path / "dyn.test.ts"
    test_file.write_text(
        "it.each([1, 2])('case %d', (n) => { expect(n).toBeGreaterThan(0); });\n"
    )

    listed = [
        {"file": str(test_file), "name": "case 1"},
        {"file": str(test_file), "name": "case 2"},
    ]

    def fake_run(cmd, **kwargs):
        for a in cmd:
            if a.startswith("--outputFile="):
                Path(a.split("=", 1)[1]).write_text(json.dumps(listed))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("orchestrator.test_audit.adapters.vitest_adapter.subprocess.run",
               side_effect=fake_run):
        items = VitestAdapter().collect(tmp_path)

    names = sorted(it.name for it in items)
    assert names == ["case 1", "case 2"]


# ---------------------------------------------------------------------------
# Quote-char truncation in fallback scan
# ---------------------------------------------------------------------------

def test_fallback_scan_preserves_inner_quotes(tmp_path: Path):
    """A single-quoted test name containing inner double quotes must not
    be truncated at the first inner quote.
    """
    test_file = tmp_path / "quotes.test.ts"
    test_file.write_text(
        "it('does not retain the pre-Nova \"Current Workaround\" section', "
        "() => {});\n"
    )

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no npx")

    with patch("orchestrator.test_audit.adapters.vitest_adapter.subprocess.run",
               side_effect=fake_run):
        items = VitestAdapter().collect(tmp_path)

    assert len(items) == 1
    assert items[0].name == 'does not retain the pre-Nova "Current Workaround" section'


def test_fallback_scan_handles_backslash_escaped_quote(tmp_path: Path):
    test_file = tmp_path / "escape.test.ts"
    test_file.write_text(
        r'it("name with \"escaped\" quote", () => {});' + "\n"
    )

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no npx")

    with patch("orchestrator.test_audit.adapters.vitest_adapter.subprocess.run",
               side_effect=fake_run):
        items = VitestAdapter().collect(tmp_path)

    assert len(items) == 1
    assert items[0].name == 'name with "escaped" quote'


def test_scan_tests_preserves_inner_quotes():
    src = (
        "it('says \"hi\" loudly', () => {});\n"
    )
    found = _scan_tests(src)
    assert [t[2] for t in found] == ['says "hi" loudly']


def test_is_in_string_or_comment_detects_line_comment():
    src = "abc // hello\nworld"
    assert _is_in_string_or_comment(src, src.index("hello")) is True
    assert _is_in_string_or_comment(src, src.index("world")) is False


# ---------------------------------------------------------------------------
# Explicit source-roots for module inventory
# ---------------------------------------------------------------------------

def test_module_inventory_honors_explicit_source_roots(tmp_path: Path):
    """Repos with non-conventional layouts (mcp/, cli/, etc.) need an
    explicit source-roots list; the adapter must scan exactly those.
    """
    (tmp_path / "mcp").mkdir()
    (tmp_path / "mcp" / "alpha.ts").write_text("export function alpha() {}\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "beta.ts").write_text("export function beta() {}\n")
    # A non-source dir that should be ignored unless asked for.
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "ignored.ts").write_text("export function nope() {}\n")

    inv = VitestAdapter().module_inventory(tmp_path, source_roots=["lib", "mcp"])
    by_name = {m.module_name: m for m in inv}
    assert "alpha" in by_name
    assert "beta" in by_name
    assert "ignored" not in by_name  # scratch wasn't requested


def test_module_inventory_default_roots_unchanged_without_flag(tmp_path: Path):
    """Without an explicit list, only the conventional Node/TS roots are scanned."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("export function a() {}\n")
    (tmp_path / "mcp").mkdir()
    (tmp_path / "mcp" / "b.ts").write_text("export function b() {}\n")

    inv = VitestAdapter().module_inventory(tmp_path)
    names = {m.module_name for m in inv}
    assert "a" in names
    assert "b" not in names  # mcp/ isn't a default candidate


def test_collect_corpus_threads_source_roots_to_adapter(tmp_path: Path):
    """End-to-end: --source-roots threads through audit -> corpus -> adapter."""
    from orchestrator.test_audit import collect_corpus

    (tmp_path / "vitest.config.ts").write_text("export default {}\n")
    (tmp_path / "mcp").mkdir()
    (tmp_path / "mcp" / "thing.ts").write_text("export function thing() {}\n")
    test_file = tmp_path / "demo.test.ts"
    test_file.write_text("it('x', () => {});\n")

    listed = [{"file": str(test_file), "name": "x"}]

    def fake_run(cmd, **kwargs):
        for a in cmd:
            if a.startswith("--outputFile="):
                Path(a.split("=", 1)[1]).write_text(json.dumps(listed))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    import yaml
    with patch("orchestrator.test_audit.adapters.vitest_adapter.subprocess.run",
               side_effect=fake_run):
        result = collect_corpus(tmp_path, run_tests=False, source_roots=["mcp"])

    corpus = yaml.safe_load(result.corpus_path.read_text())
    module_names = {m["module_name"] for m in corpus["architecture"]["modules"]}
    assert "thing" in module_names


def test_locate_test_returns_line_of_it_call():
    src = (
        "import { it } from 'vitest';\n"
        "describe('outer', () => {\n"
        "  describe('inner', () => {\n"
        "    it('the leaf', () => {});\n"
        "  });\n"
        "});\n"
    )
    line, leaf = _locate_test(src, "outer > inner > the leaf")
    assert leaf == "the leaf"
    assert line == 4


# ---------------------------------------------------------------------------
# has_real_assertion — nested expect() args (regression guard: a regex using [^)]*
# silently flags real vitest tests as non-assertive when the expect() argument
# contains inner parens).
# ---------------------------------------------------------------------------

def _analyze_body(tmp_path: Path, body: str):
    fp = tmp_path / "x.test.ts"
    fp.write_text(body)

    class FakeItem:
        nodeid = "x.test.ts::t"
        name = "t"
        line = 2
        file = fp
        classname = None

    return VitestAdapter().analyze(FakeItem())


def test_has_real_assertion_handles_nested_call_in_expect_arg(tmp_path: Path):
    body = (
        "import { it, expect } from 'vitest';\n"
        "it('t', () => {\n"
        "  expect(ROLE_VOCAB.has('summary')).toBe(true);\n"
        "});\n"
    )
    result = _analyze_body(tmp_path, body)
    assert result.assertion_count == 1
    assert result.has_real_assertion is True


def test_has_real_assertion_handles_rejects_to_throw(tmp_path: Path):
    body = (
        "import { it, expect } from 'vitest';\n"
        "it('t', async () => {\n"
        "  await expect(promise).rejects.toThrow('boom');\n"
        "});\n"
    )
    result = _analyze_body(tmp_path, body)
    assert result.has_real_assertion is True


def test_has_real_assertion_handles_resolves_to_equal(tmp_path: Path):
    body = (
        "import { it, expect } from 'vitest';\n"
        "it('t', async () => {\n"
        "  await expect(getValue()).resolves.toEqual({ ok: true });\n"
        "});\n"
    )
    result = _analyze_body(tmp_path, body)
    assert result.has_real_assertion is True


def test_has_real_assertion_false_when_no_matcher(tmp_path: Path):
    body = (
        "import { it, expect } from 'vitest';\n"
        "it('t', () => {\n"
        "  const r = thing();\n"  # no expect at all
        "});\n"
    )
    result = _analyze_body(tmp_path, body)
    assert result.assertion_count == 0
    assert result.has_real_assertion is False


# ---------------------------------------------------------------------------
# _vitest_run_json — modern vitest 4.x `files`/`tasks` tree shape. A runner
# that only parses the legacy testResults shape returns runtime: null for every
# test on a vitest 4.x suite.
# ---------------------------------------------------------------------------

def test_vitest_run_json_parses_files_task_tree(tmp_path: Path):
    from orchestrator.test_audit.adapters.vitest_adapter import _vitest_run_json

    repo = tmp_path / "repo"
    repo.mkdir()
    file_abs = str(repo / "foo.test.ts")
    payload = {
        "files": [
            {
                "filepath": file_abs,
                "name": file_abs,
                "type": "suite",
                "tasks": [
                    {
                        "name": "outer",
                        "type": "suite",
                        "tasks": [
                            {
                                "name": "passes",
                                "type": "test",
                                "result": {"state": "pass", "duration": 12.7},
                            },
                            {
                                "name": "fails",
                                "type": "test",
                                "result": {"state": "fail", "duration": 7,
                                           "errors": [{"message": "boom"}]},
                            },
                            {
                                "name": "is skipped",
                                "type": "test",
                                "result": {"state": "skip", "duration": 0},
                            },
                        ],
                    },
                ],
            }
        ]
    }

    def fake_run(cmd, *args, **kwargs):
        out_path = Path([a for a in cmd if a.startswith("--outputFile=")][0]
                        .split("=", 1)[1])
        out_path.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("orchestrator.test_audit.adapters.vitest_adapter.subprocess.run",
               side_effect=fake_run):
        out = _vitest_run_json(repo)

    assert out is not None
    by_nodeid = {nid: r for nid, r in out.items()}
    nodeids = sorted(by_nodeid)
    assert nodeids == sorted([
        "foo.test.ts::outer::passes",
        "foo.test.ts::outer::fails",
        "foo.test.ts::outer::is skipped",
    ])
    assert by_nodeid["foo.test.ts::outer::passes"].status == "passed"
    assert by_nodeid["foo.test.ts::outer::passes"].duration_ms == 13
    failed = by_nodeid["foo.test.ts::outer::fails"]
    assert failed.status == "failed"
    assert "boom" in (failed.error or "")
    assert by_nodeid["foo.test.ts::outer::is skipped"].status == "skipped"


def test_vitest_run_json_uses_ancestor_titles_for_nodeid(tmp_path: Path):
    """Vitest 4.x emits `fullName` as space-joined ("DESC does X") not `>`-joined.
    Runtime nodeids must come from ancestorTitles+title so they match the static
    collector's `<rel>::<describe>::<name>` shape.
    """
    from orchestrator.test_audit.adapters.vitest_adapter import _vitest_run_json

    repo = tmp_path / "repo"
    repo.mkdir()
    file_abs = str(repo / "foo.test.ts")
    payload = {
        "testResults": [
            {
                "name": file_abs,
                "assertionResults": [
                    {
                        "ancestorTitles": ["PHASE_FOLDERS"],
                        "title": "does X",
                        "fullName": "PHASE_FOLDERS does X",
                        "status": "passed",
                        "duration": 3,
                    },
                    {
                        "ancestorTitles": ["outer", "inner"],
                        "title": "nested test",
                        "fullName": "outer inner nested test",
                        "status": "passed",
                        "duration": 1,
                    },
                ],
            }
        ]
    }

    def fake_run(cmd, *args, **kwargs):
        out_path = Path([a for a in cmd if a.startswith("--outputFile=")][0]
                        .split("=", 1)[1])
        out_path.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("orchestrator.test_audit.adapters.vitest_adapter.subprocess.run",
               side_effect=fake_run):
        out = _vitest_run_json(repo)

    assert "foo.test.ts::PHASE_FOLDERS::does X" in out
    assert "foo.test.ts::outer::inner::nested test" in out


def test_fallback_scan_includes_describe_paths(tmp_path: Path):
    """_fallback_scan must keep describes: `<file>::<leaf>` nodeids would never
    match runtime data's `<file>::<describe>::<leaf>`."""
    from orchestrator.test_audit.adapters.vitest_adapter import _fallback_scan

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.test.ts").write_text(
        "import { describe, it, expect } from 'vitest';\n"
        "describe('PHASE_FOLDERS', () => {\n"
        "  it('does X', () => { expect(1).toBe(1); });\n"
        "  it('does Y', () => { expect(2).toBe(2); });\n"
        "});\n"
        "describe('other', () => {\n"
        "  it('does X', () => { expect(3).toBe(3); });\n"  # same leaf, different describe
        "});\n"
    )
    items = _fallback_scan(repo)
    nodeids = sorted(it.nodeid for it in items)
    assert nodeids == [
        "x.test.ts::PHASE_FOLDERS::does X",
        "x.test.ts::PHASE_FOLDERS::does Y",
        "x.test.ts::other::does X",
    ]


def test_vitest_run_json_legacy_test_results_still_works(tmp_path: Path):
    from orchestrator.test_audit.adapters.vitest_adapter import _vitest_run_json

    repo = tmp_path / "repo"
    repo.mkdir()
    file_abs = str(repo / "bar.test.ts")
    payload = {
        "testResults": [
            {
                "name": file_abs,
                "assertionResults": [
                    {"fullName": "outer > does X", "status": "passed", "duration": 4},
                ],
            }
        ]
    }

    def fake_run(cmd, *args, **kwargs):
        out_path = Path([a for a in cmd if a.startswith("--outputFile=")][0]
                        .split("=", 1)[1])
        out_path.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("orchestrator.test_audit.adapters.vitest_adapter.subprocess.run",
               side_effect=fake_run):
        out = _vitest_run_json(repo)

    assert "bar.test.ts::outer::does X" in out
    assert out["bar.test.ts::outer::does X"].status == "passed"
