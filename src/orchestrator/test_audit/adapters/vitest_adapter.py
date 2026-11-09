"""Vitest adapter — discovers, analyzes, runs, and skip-marks vitest tests.

Conventions vs the pytest adapter:
- nodeid is `<rel-file>::<describe1>::<describe2>::<test_name>`. Spaces and
  punctuation in the test name are preserved (vitest names are arbitrary
  strings).
- Static analysis is regex + brace-counting, not AST. JS/TS doesn't have a
  Python stdlib parser; the LLM judge tolerates some noise in the static
  signal because it reads the source body itself.
- `apply_delete` is intentionally unsupported: removing `it(...)` blocks
  needs a real parser to be safe (regex literals + JSX + nested template
  strings break brace counting). The applier downgrades delete→skip via
  `supports_delete=False`. Audit-report.md still lists deletion candidates
  for human follow-up in the same PR.
- Discovery + run shell out to `npx vitest`. The target repo always has
  vitest installed by definition, so `npx --no-install` would also work.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from orchestrator.test_audit.architecture import ModuleInfo
from orchestrator.test_audit.collector import TestItem
from orchestrator.test_audit.parser import StaticAnalysis
from orchestrator.test_audit.runner import TestResult


# Files vitest discovers by default.
_TEST_FILE_RE = re.compile(r".*\.(test|spec)\.(ts|tsx|js|jsx|mjs|cjs|cts|mts)$")
# Source files we'd pair with tests.
_SRC_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs")
# Dirs we never scan when discovering source modules.
_SKIP_SRC_DIRS = {"node_modules", "dist", "build", "coverage", ".next", ".nuxt",
                  ".vite", ".svelte-kit", "__tests__", "test", "tests", "__snapshots__"}
# Dirs we never scan when discovering test files. Looser than _SKIP_SRC_DIRS
# because tests legitimately live under `tests/` and `__tests__/`.
_SKIP_TEST_DIRS = {"node_modules", "dist", "build", "coverage", ".next", ".nuxt",
                   ".vite", ".svelte-kit", "__snapshots__"}
# Back-compat alias for any external callers.
_SKIP_DIRS = _SKIP_SRC_DIRS
# Identifiers that look like framework noise rather than CUT calls.
_FRAMEWORK_NAMES = {
    "describe", "it", "test", "expect", "beforeAll", "beforeEach",
    "afterAll", "afterEach", "vi", "vitest", "suite", "bench",
    "jest", "console", "Promise", "Array", "Object", "JSON", "Math",
    "String", "Number", "Boolean", "Date", "Set", "Map",
}
# Vi/jest mock-like helpers; presence of these implies a mock target.
_MOCK_HELPERS = ("vi.mock", "vi.fn", "vi.spyOn", "vi.doMock",
                 "jest.mock", "jest.fn", "jest.spyOn")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class VitestAdapter:
    name = "vitest"

    # ------------------------------------------------------------------
    # Public Protocol surface
    # ------------------------------------------------------------------

    def collect(self, repo: Path) -> list[TestItem]:
        """Discover tests via `npx vitest list --reporter=json`, then walk
        the source files to recover full describe paths per test.

        Source-walking is the primary signal: the vitest list reporter has
        historically emitted only leaf names for some shapes, which would
        otherwise collapse multiple `it('shared', ...)` calls under sibling
        describes into a single nodeid. We use the list output
        for file discovery and to backfill dynamic-name tests (e.g.
        `it.each([...])`) that can't be recovered from a static scan.

        Falls back to a regex-only filesystem scan if vitest can't be
        invoked — keeps the corpus usable in CI without a node_modules.
        """
        repo = repo.resolve()
        listed = _vitest_list_json(repo)
        if listed is None:
            return _fallback_scan(repo)

        # Group listed entries by file. We only need the file set + the names
        # vitest reported, to detect dynamic-name tests not present in source.
        listed_by_file: dict[Path, set[str]] = {}
        for entry in listed:
            filepath = entry.get("file") or entry.get("filepath")
            full_name = entry.get("name") or entry.get("fullName") or ""
            if not filepath or not full_name:
                continue
            listed_by_file.setdefault(Path(filepath), set()).add(full_name)

        items: list[TestItem] = []
        for file, listed_names in listed_by_file.items():
            if not file.exists():
                continue
            try:
                src = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            try:
                rel = str(file.relative_to(repo))
            except ValueError:
                rel = str(file)

            # Source-based: find every static `it/test/fit('lit', ...)` call
            # along with its describe nesting. Each occurrence becomes one
            # TestItem, so sibling describes with the same leaf name produce
            # distinct nodeids.
            scanned = _scan_tests(src)
            scanned_full = set()
            scanned_leaves = set()
            for line, stack, leaf in scanned:
                full = " > ".join(stack + [leaf]) if stack else leaf
                scanned_full.add(full)
                scanned_leaves.add(leaf)
                nodeid = f"{rel}::{'::'.join(stack + [leaf])}"
                items.append(TestItem(
                    nodeid=nodeid,
                    file=file,
                    name=leaf,
                    line=line,
                    classname=None,
                ))

            # Backfill: anything vitest reported that the source scan didn't
            # cover (typical case: it.each / templated names that aren't a
            # static literal). Use the listed name verbatim.
            for listed_name in listed_names:
                if listed_name in scanned_full:
                    continue
                leaf = listed_name.split(" > ")[-1].strip()
                if leaf in scanned_leaves and " > " not in listed_name:
                    # vitest gave us a flat leaf and the source already
                    # produced a (possibly-nested) test with that leaf —
                    # don't duplicate.
                    continue
                line, leaf_loc = _locate_test(src, listed_name)
                nodeid = f"{rel}::{listed_name.replace(' > ', '::')}"
                items.append(TestItem(
                    nodeid=nodeid,
                    file=file,
                    name=leaf_loc,
                    line=line,
                    classname=None,
                ))
        return items

    def analyze(self, item: TestItem) -> StaticAnalysis:
        """Static facts for a single vitest test: assertions, mocks, helpers."""
        try:
            src_full = item.file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return StaticAnalysis(nodeid=item.nodeid, name=item.name, body_source="",
                                  assertion_count=0, line_count=0)

        body = _extract_test_body(src_full, item.line, item.name)
        if not body:
            return StaticAnalysis(nodeid=item.nodeid, name=item.name, body_source="",
                                  assertion_count=0, line_count=0)

        assertion_count = len(re.findall(r"\bexpect\s*\(", body))
        # A "real" assertion = an `expect(` somewhere AND a matcher chained
        # somewhere. The old single-regex form used `[^)]*` for the expect
        # argument, which silently fails on nested calls like
        # `expect(obj.has('x')).toBe(true)` (~40% of typical vitest suites).
        # We accept the looser two-part check: in practice the LLM judge reads
        # the body and catches the rare false positive.
        has_expect = bool(re.search(r"\bexpect\s*\(", body))
        has_matcher = bool(
            re.search(r"\)\s*(?:\.\s*(?:not|resolves|rejects)\s*)*\.\s*\w+\s*\(", body)
        )
        has_real_assertion = (
            (has_expect and has_matcher)
            or bool(re.search(r"\bassert(?:Equal|Strict)?\s*\(", body))
        )

        mock_targets = sorted(set(_extract_mock_targets(body)))
        source_funcs = sorted(set(_extract_source_calls(body)))

        return StaticAnalysis(
            nodeid=item.nodeid,
            name=item.name,
            body_source=body,
            assertion_count=assertion_count,
            mock_targets=mock_targets,
            fixtures_used=[],  # vitest has no fixture system analogous to pytest
            source_funcs_referenced=source_funcs,
            has_real_assertion=has_real_assertion,
            line_count=body.count("\n") + 1 if body else 0,
        )

    def run(self, repo: Path, reruns: int = 0) -> dict[str, TestResult]:
        """Shell out to `vitest run --reporter=json` and parse the result."""
        runs: list[dict[str, TestResult]] = []
        for _ in range(1 + reruns):
            results = _vitest_run_json(repo)
            if results is not None:
                runs.append(results)

        if not runs:
            return {}
        if len(runs) == 1:
            return runs[0]

        # Combine: majority status wins; flake_count = runs that disagreed.
        nodeids = set().union(*(r.keys() for r in runs))
        out: dict[str, TestResult] = {}
        for nid in nodeids:
            statuses = [r[nid].status for r in runs if nid in r]
            if not statuses:
                continue
            majority, _ = Counter(statuses).most_common(1)[0]
            flake = sum(1 for s in statuses if s != majority)
            rep = next(r[nid] for r in runs if nid in r and r[nid].status == majority)
            rep.flake_count = flake
            out[nid] = rep
        return out

    def module_inventory(self, repo: Path,
                         source_roots: list[str] | None = None) -> list[ModuleInfo]:
        """Pair each src/* TS/JS module with its test file, by name and by import.

        `source_roots` (when given) is a list of repo-relative directories
        to scan for source modules. Caller-supplied roots win — they're
        the right answer for repos with non-conventional layouts (`mcp/`,
        `cli/`, etc., or multi-root layouts like ACE's `lib/` + `mcp/`).
        Without an explicit list, fall back to the conventional Node/TS
        layout (`src/`, `lib/`, `app/`, `source/`).
        """
        repo = repo.resolve()
        roots: list[Path] = []
        if source_roots:
            for r in source_roots:
                d = (repo / r).resolve()
                if d.exists() and d.is_dir():
                    roots.append(d)
        else:
            for candidate in ("src", "lib", "app", "source"):
                d = repo / candidate
                if d.exists() and d.is_dir():
                    roots.append(d)
        if not roots:
            # No usable root — scan repo root, but conservatively.
            roots = [repo]

        # Collect test files anywhere under repo (vitest scans broadly,
        # including conventional `tests/` and `__tests__/` directories).
        test_files = list(_walk_files(repo, _SKIP_TEST_DIRS))
        test_files = [p for p in test_files if _TEST_FILE_RE.match(p.name)]
        test_by_stem: dict[str, Path] = {}
        for tp in test_files:
            stem = re.sub(r"\.(test|spec)$", "", tp.stem)
            test_by_stem.setdefault(stem, tp)

        test_imports = _scan_test_imports(test_files)

        inv: list[ModuleInfo] = []
        seen: set[Path] = set()
        for root in roots:
            for src in sorted(_walk_files(root, _SKIP_SRC_DIRS)):
                if src in seen:
                    continue
                seen.add(src)
                if src.suffix not in _SRC_EXTS:
                    continue
                if _TEST_FILE_RE.match(src.name):
                    continue
                if src.name.startswith("."):
                    continue
                # Skip declaration files and barrel files — they hold no testable behavior.
                if src.name.endswith(".d.ts"):
                    continue
                try:
                    text = src.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                tp = test_by_stem.get(src.stem)
                imported = _module_referenced_in_imports(src, test_imports, repo)
                inv.append(ModuleInfo(
                    module_name=src.stem,
                    src_path=str(src.relative_to(repo)),
                    src_lines=text.count("\n") + 1,
                    public_func_count=_count_public_exports(text),
                    has_test_file=tp is not None or imported,
                    test_file_path=str(tp.relative_to(repo)) if tp else None,
                ))
        return inv

    def apply_delete(self, file: Path, name: str) -> bool:
        """Vitest deletion is intentionally not supported in v1.

        See module docstring + SKILL.md for rationale. Returns False so the
        applier records the nodeid as `skipped` (won't happen in practice
        because plan() downgrades delete→skip when supports_delete=False).
        """
        return False

    def apply_skip(self, file: Path, name: str, reason: str) -> bool:
        """Mark a vitest test as `.skip(...)` and prepend an audit comment."""
        try:
            src = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        # Match the test call by name. Vitest accepts string, template, or
        # function-name variants; we only handle the string/template form
        # here (the common case). If the name is a template literal with
        # interpolation, this falls through and returns False — agent's
        # job to flag.
        name_re = re.escape(name)
        # Allow surrounding quotes of any kind; allow it/test with optional
        # chained modifier (`.only`, `.concurrent`, `.skip`).
        pattern = re.compile(
            rf"(?P<head>\b(?:it|test|fit)(?:\s*\.\s*\w+)?\s*\(\s*)"
            rf"(?P<quote>['\"`]){name_re}(?P=quote)"
        )
        match = pattern.search(src)
        if not match:
            return False
        # Already skipped?
        if ".skip" in match.group("head"):
            return False
        # Find the start-of-line for the matched call.
        line_start = src.rfind("\n", 0, match.start()) + 1
        line_end = src.find("\n", match.end())
        if line_end == -1:
            line_end = len(src)
        line = src[line_start:line_end]
        indent_match = re.match(r"\s*", line)
        indent = indent_match.group(0) if indent_match else ""

        # Rewrite head: `it(` / `test(` / `it.only(` → `it.skip(` / `test.skip(`.
        head = match.group("head")
        new_head = re.sub(r"\b(it|test|fit)(\s*\.\s*\w+)?",
                          lambda m: f"{m.group(1)}.skip", head, count=1)
        new_src = src[:match.start()] + new_head + src[match.start() + len(head):]

        safe_reason = (reason or "audit").replace("*/", "*\\/").splitlines()
        safe_reason = " ".join(safe_reason)[:140]
        comment = f"{indent}// audit: {safe_reason}\n"
        # Insert comment at line_start of the (already-rewritten) source.
        new_src = new_src[:line_start] + comment + new_src[line_start:]

        file.write_text(new_src, encoding="utf-8")
        return True

    def supports_delete(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Helpers — discovery / runtime via npx
# ---------------------------------------------------------------------------

def _vitest_list_json(repo: Path) -> list[dict] | None:
    """Run `npx vitest list --reporter=json` and parse the array.

    Returns None if vitest can't be invoked. Defensive against multiple
    output shapes seen across vitest versions.
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        cmd = ["npx", "--yes", "vitest", "list",
               "--reporter=json", f"--outputFile={out_path}"]
        try:
            subprocess.run(cmd, cwd=repo, capture_output=True,
                           text=True, timeout=120)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if not out_path.exists() or out_path.stat().st_size == 0:
            return None
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return _flatten_list_payload(data)
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass


def _flatten_list_payload(data) -> list[dict]:
    """Normalize the variable shapes vitest's JSON list reporter has emitted.

    Possible shapes:
      - top-level list of {file, name} dicts (newer vitest)
      - {"tests": [...]}                       (some 1.x revs)
      - {"testResults": [...]}                 (jest-compatible reporter)
      - {"files": [{"filepath", "tasks": ...}]} (internal task tree)
    """
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("tests"), list):
            return [d for d in data["tests"] if isinstance(d, dict)]
        if isinstance(data.get("testResults"), list):
            out: list[dict] = []
            for tr in data["testResults"]:
                f = tr.get("name") or tr.get("file")
                for ar in tr.get("assertionResults") or []:
                    out.append({
                        "file": f,
                        "name": ar.get("fullName") or ar.get("title", ""),
                    })
            return out
        if isinstance(data.get("files"), list):
            out: list[dict] = []
            for f in data["files"]:
                _walk_task_tree(f, [], f.get("filepath") or f.get("name", ""), out)
            return out
    return []


def _walk_task_tree(node: dict, path: list[str], filepath: str, out: list[dict]) -> None:
    name = node.get("name", "")
    type_ = node.get("type")
    if type_ == "test":
        full = " > ".join(path + [name]) if path else name
        out.append({"file": filepath, "name": full})
    else:
        next_path = path + [name] if name and type_ == "suite" else path
        for child in node.get("tasks") or []:
            _walk_task_tree(child, next_path, filepath, out)


def _vitest_run_json(repo: Path) -> dict[str, TestResult] | None:
    """Run vitest and return TestResult per nodeid, or None if vitest is missing."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        cmd = ["npx", "--yes", "vitest", "run",
               "--reporter=json", f"--outputFile={out_path}"]
        try:
            subprocess.run(cmd, cwd=repo, capture_output=True,
                           text=True, timeout=600)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if not out_path.exists() or out_path.stat().st_size == 0:
            return None
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass

    out: dict[str, TestResult] = {}
    # Jest-compatible shape — emitted by both vitest 3.x AND vitest 4.x.
    # Nodeid construction must match what the static collector produces in
    # `collect()` (via `_scan_tests`): `<rel>::<describe1>::<describe2>::<name>`.
    # Prefer `ancestorTitles` + `title` over `fullName` — vitest 4.x's fullName
    # is space-joined ("PHASE_FOLDERS does X"), not `>`-joined, so the old
    # `replace(' > ', '::')` produced one collapsed segment and never matched
    # the static side.
    for tr in data.get("testResults") or []:
        file_abs = tr.get("name") or ""
        try:
            rel = str(Path(file_abs).resolve().relative_to(repo.resolve()))
        except (ValueError, OSError):
            rel = file_abs
        for ar in tr.get("assertionResults") or []:
            ancestors = ar.get("ancestorTitles") or []
            title = ar.get("title") or ""
            if title:
                segments = [*ancestors, title]
            else:
                # Fall back to fullName for shapes that don't split. Vitest 3.x
                # used " > "; vitest 4.x uses plain space; we try both.
                full_name = ar.get("fullName") or ""
                if not full_name:
                    continue
                segments = full_name.split(" > ") if " > " in full_name else [full_name]
            nodeid = f"{rel}::{'::'.join(segments)}"
            status = ar.get("status", "unknown")
            if status == "pending":
                status = "skipped"
            duration_ms = int(round(float(ar.get("duration") or 0)))
            err = None
            fails = ar.get("failureMessages") or []
            if fails:
                err = "; ".join(str(f)[:200] for f in fails)[:500]
            out[nodeid] = TestResult(
                nodeid=nodeid, status=status,
                duration_ms=duration_ms, error=err,
            )
    if out:
        return out
    # Modern vitest 4.x shape — `files: [{filepath, tasks: [...]}]` task tree.
    for f in data.get("files") or []:
        filepath = f.get("filepath") or f.get("name", "")
        try:
            rel = str(Path(filepath).resolve().relative_to(repo.resolve()))
        except (ValueError, OSError):
            rel = filepath
        # Recurse into the file's children directly — the file node itself
        # is not a describe block (its `name` is the absolute filepath and
        # would otherwise leak into nodeids).
        for child in f.get("tasks") or []:
            _collect_task_results(child, [], rel, out)
    return out


def _collect_task_results(node: dict, path: list[str], rel: str,
                          out: dict[str, TestResult]) -> None:
    """Walk a vitest 4.x task tree node and emit TestResults for `type=test` leaves."""
    name = node.get("name", "")
    type_ = node.get("type")
    if type_ == "test":
        full = "::".join(path + [name]) if path else name
        nodeid = f"{rel}::{full}"
        result = node.get("result") or {}
        state = result.get("state") or node.get("mode") or "unknown"
        # vitest task states: "pass", "fail", "skip", "todo", "run".
        status_map = {"pass": "passed", "fail": "failed", "skip": "skipped",
                      "todo": "skipped", "run": "unknown"}
        status = status_map.get(state, state)
        duration_ms = int(round(float(result.get("duration") or 0)))
        err = None
        errors = result.get("errors") or []
        if errors:
            err = "; ".join(str(e.get("message") or e)[:200] for e in errors)[:500]
        out[nodeid] = TestResult(
            nodeid=nodeid, status=status,
            duration_ms=duration_ms, error=err,
        )
        return
    next_path = path + [name] if name and type_ == "suite" else path
    for child in node.get("tasks") or []:
        _collect_task_results(child, next_path, rel, out)


# ---------------------------------------------------------------------------
# Helpers — fallback discovery + body extraction
# ---------------------------------------------------------------------------

def _walk_files(root: Path, skip_dirs: set[str]):
    """Recursively yield files, pruning skip_dirs at any depth."""
    if not root.exists():
        return
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in skip_dirs or entry.name.startswith("."):
                    continue
                stack.append(entry)
            elif entry.is_file():
                yield entry


def _fallback_scan(repo: Path) -> list[TestItem]:
    """Regex scan when vitest isn't runnable. Misses dynamic-name tests.

    Uses `_SKIP_TEST_DIRS` (not `_SKIP_SRC_DIRS`) so conventional `test/`,
    `tests/`, and `__tests__/` directories are walked — that's where most
    JS/TS suites actually live. Without this distinction the fallback
    silently misses every co-located test directory (caught on first
    real-world run against ACE; canopy 0.2.84 was the broken version).
    """
    items: list[TestItem] = []
    test_files = [p for p in _walk_files(repo, _SKIP_TEST_DIRS) if _TEST_FILE_RE.match(p.name)]
    for file in sorted(test_files):
        try:
            src = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = str(file.relative_to(repo))
        except ValueError:
            rel = str(file)
        # Use the same describe-aware scanner as the primary `collect()` path
        # so fallback nodeids match the format runtime data uses (and so
        # sibling describes with the same leaf name don't collide).
        for line, stack, leaf in _scan_tests(src):
            items.append(TestItem(
                nodeid=f"{rel}::{'::'.join(stack + [leaf])}",
                file=file,
                name=leaf,
                line=line,
            ))
    return items


# Quote-aware match for `describe|it|test|fit('name', ...)` calls. Captures
# the opening quote in group `q` and the literal body (with backslash-escapes
# preserved) in group `name`. Allows a chained modifier (`.skip`, `.only`,
# `.concurrent`, `.each`, etc.) on the call. Note: `each` is matched but
# templated args between the modifier and the parens (`it.each([...])`'s
# second call) won't show up because there's no string literal after `(`.
_TEST_CALL_RE = re.compile(
    r"\b(?P<kind>describe|it|test|fit)(?:\s*\.\s*\w+)*\s*\(\s*"
    r"(?P<q>['\"`])(?P<name>(?:\\.|(?!(?P=q)).)*)(?P=q)"
)


def _decode_quoted(raw: str) -> str:
    """Decode JS-style backslash escapes in a captured quoted-literal body.

    We only undo the escapes the regex preserved (\\<anything>) — primarily
    \\", \\', \\`, \\\\, \\n, \\t. Anything more exotic stays as-is. The aim
    is to round-trip the test name vitest itself would produce.
    """
    def _sub(m: re.Match) -> str:
        c = m.group(1)
        return {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(c, c)
    return re.sub(r"\\(.)", _sub, raw)


def _scan_tests(src: str) -> list[tuple[int, list[str], str]]:
    """Walk a TS/JS source and return (line, describe_stack, leaf) per test.

    Skips `_TEST_CALL_RE` matches that fall inside strings, template
    literals, or comments — those would otherwise produce phantom tests
    when source code talks about test names in docstrings.

    Computes the describe stack for each `it/test/fit` call by finding
    the body brace `{ ... }` of every `describe(...)` and recording which
    describes lexically enclose each test call.
    """
    # First pass: enumerate all candidate calls.
    calls: list[tuple[str, str, int, int]] = []  # (kind, name, match_start, match_end)
    for m in _TEST_CALL_RE.finditer(src):
        if _is_in_string_or_comment(src, m.start()):
            continue
        calls.append((
            m.group("kind"),
            _decode_quoted(m.group("name")),
            m.start(),
            m.end(),
        ))

    # Second pass: for each describe, find its body { } so we know the
    # lexical range it owns. The body brace is the first `{` after the
    # call's open paren that has a matching `}` before the call's close
    # paren.
    describes: list[tuple[str, int, int]] = []  # (name, body_open, body_close)
    for kind, name, start, end in calls:
        if kind != "describe":
            continue
        paren_open = src.find("(", start)
        if paren_open == -1:
            continue
        paren_close = _find_matching_close(src, paren_open)
        if paren_close is None:
            continue
        body_open = _find_first_body_brace(src, end, paren_close)
        if body_open is None:
            continue
        body_close = _find_matching_brace(src, body_open)
        if body_close is None or body_close > paren_close:
            continue
        describes.append((name, body_open, body_close))

    # Third pass: emit each test call with the describe stack that encloses it.
    out: list[tuple[int, list[str], str]] = []
    for kind, name, start, _end in calls:
        if kind == "describe":
            continue
        stack = sorted(
            ((open_, close_, dname) for dname, open_, close_ in describes
             if open_ < start < close_),
            key=lambda t: t[0],
        )
        line = src.count("\n", 0, start) + 1
        out.append((line, [t[2] for t in stack], name))
    return out


def _find_first_body_brace(src: str, search_from: int, search_to: int) -> int | None:
    """Find the first `{` between offsets that's not inside a string/comment.

    Used to locate the body brace of a `describe('name', () => { ... })`
    or `describe('name', function () { ... })` call.
    """
    i = search_from
    n = min(len(src), search_to)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            nl = src.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            close = src.find("*/", i + 2)
            i = n if close == -1 else close + 2
            continue
        if c in "'\"":
            i = _skip_string(src, i, c)
            continue
        if c == "`":
            i = _skip_template(src, i)
            continue
        if c == "{":
            return i
        i += 1
    return None


def _is_in_string_or_comment(src: str, offset: int) -> bool:
    """True iff `offset` falls inside a string, template literal, or comment."""
    i = 0
    n = len(src)
    while i < offset:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            nl = src.find("\n", i)
            end = n if nl == -1 else nl + 1
            if offset < end:
                return True
            i = end
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            close = src.find("*/", i + 2)
            end = n if close == -1 else close + 2
            if offset < end:
                return True
            i = end
            continue
        if c in "'\"":
            j = _skip_string(src, i, c)
            if offset < j:
                return True
            i = j
            continue
        if c == "`":
            j = _skip_template(src, i)
            if offset < j:
                return True
            i = j
            continue
        i += 1
    return False


def _locate_test(src: str, full_name: str) -> tuple[int, str]:
    """Return (line, leaf_name) for a test inside `src` given its full name.

    `full_name` looks like 'outer > inner > does X'. We try, in order:
      1. exact match of the leaf name on a line containing it/test/fit
      2. fallback: first occurrence of the leaf name anywhere
    Line is 1-based; defaults to 1 if not found.
    """
    leaf = full_name.split(" > ")[-1].strip()
    if not leaf:
        return 1, full_name
    needle = re.compile(
        rf"\b(?:it|test|fit)(?:\s*\.\s*\w+)?\s*\(\s*['\"`]{re.escape(leaf)}['\"`]"
    )
    m = needle.search(src)
    if m:
        return src.count("\n", 0, m.start()) + 1, leaf
    idx = src.find(leaf)
    if idx >= 0:
        return src.count("\n", 0, idx) + 1, leaf
    return 1, leaf


def _extract_test_body(src: str, line: int, leaf_name: str) -> str:
    """Return the lexical body of an `it(...)`/`test(...)` call.

    Strategy: starting at `line` (1-based), find the opening paren of the
    nearest `it/test(` call. Skip past the name string. Then walk forward
    counting parens, brackets, and braces, ignoring those inside strings,
    template literals, and line/block comments. Stop when paren depth
    returns to zero.
    """
    line0 = max(0, line - 1)
    lines = src.splitlines(keepends=True)
    # Index into src corresponding to line0.
    if line0 >= len(lines):
        return ""
    start_offset = sum(len(l) for l in lines[:line0])
    # Search forward for the call within ~5 lines.
    window = src[start_offset:start_offset + 4000]
    m = re.search(
        rf"\b(?:it|test|fit)(?:\s*\.\s*\w+)?\s*(\()\s*['\"`]{re.escape(leaf_name)}['\"`]",
        window,
    )
    if not m:
        # Looser fallback — any it/test call within window. Capture group 1
        # is the open paren so callers don't need to re-find it.
        m = re.search(r"\b(?:it|test|fit)(?:\s*\.\s*\w+)?\s*(\()", window)
    if not m:
        return ""
    open_paren = start_offset + m.start(1)
    end = _find_matching_close(src, open_paren)
    if end is None:
        return ""
    return src[open_paren:end + 1]


def _find_matching_close(src: str, open_idx: int) -> int | None:
    """Walk `src` from `open_idx` (a `(`), return index of matching `)`.

    Aware of:
      - single-quoted, double-quoted, template-literal strings (skip braces inside)
      - // line comments and /* block */ comments
      - escapes inside strings
      - nested template-literal `${...}` expressions
    """
    if open_idx >= len(src) or src[open_idx] != "(":
        return None
    i = open_idx
    depth = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            nl = src.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            close = src.find("*/", i + 2)
            i = n if close == -1 else close + 2
            continue
        if c == "'" or c == '"':
            i = _skip_string(src, i, c)
            continue
        if c == "`":
            i = _skip_template(src, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _skip_string(src: str, i: int, quote: str) -> int:
    """Advance past a normal quoted string starting at `i` (the opening quote)."""
    n = len(src)
    i += 1
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        i += 1
    return n


def _skip_template(src: str, i: int) -> int:
    """Advance past a backtick template literal, including ${...} expressions."""
    n = len(src)
    i += 1  # past the opening `
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return i + 1
        if c == "$" and i + 1 < n and src[i + 1] == "{":
            # nested expression — walk it as if it's a brace block.
            j = _find_matching_brace(src, i + 1)
            i = j + 1 if j is not None else n
            continue
        i += 1
    return n


def _find_matching_brace(src: str, open_idx: int) -> int | None:
    """Walk from `{` to its matching `}`, respecting strings/templates/comments."""
    if open_idx >= len(src) or src[open_idx] != "{":
        return None
    i = open_idx
    depth = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            nl = src.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            close = src.find("*/", i + 2)
            i = n if close == -1 else close + 2
            continue
        if c == "'" or c == '"':
            i = _skip_string(src, i, c)
            continue
        if c == "`":
            i = _skip_template(src, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


# ---------------------------------------------------------------------------
# Helpers — body analysis
# ---------------------------------------------------------------------------

def _extract_mock_targets(body: str) -> list[str]:
    """Find vi.mock("pkg/mod") / vi.fn() / vi.spyOn(obj, "m") targets.

    Per-helper extraction since the meaningful argument differs:
      vi.mock("path")         -> "path"             (first string arg)
      jest.mock("path")       -> "path"
      vi.doMock("path")       -> "path"
      vi.spyOn(obj, "method") -> "method"           (second arg, after object)
      vi.fn() / jest.fn()     -> helper name        (no string arg to capture)
    """
    out: list[str] = []
    # First-arg-is-target helpers.
    for helper in ("vi.mock", "vi.doMock", "jest.mock"):
        for m in re.finditer(
            rf"{re.escape(helper)}\s*\(\s*(?P<q>['\"`])(?P<v>[^'\"`]*)(?P=q)",
            body,
        ):
            v = m.group("v").strip()
            if v:
                out.append(v)
    # spyOn — capture the method name (2nd arg).
    for m in re.finditer(
        r"\b(?:vi|jest)\s*\.\s*spyOn\s*\(\s*[^,]+,\s*(?P<q>['\"`])(?P<v>[^'\"`]*)(?P=q)",
        body,
    ):
        v = m.group("v").strip()
        if v:
            out.append(v)
    # Anonymous mocks — record the helper name itself.
    for helper in ("vi.fn", "jest.fn"):
        if re.search(rf"{re.escape(helper)}\s*\(", body):
            out.append(helper)
    return out


def _extract_source_calls(body: str) -> list[str]:
    """Identifiers called as functions that aren't framework noise.

    Heuristic: find `<ident>(` and `<obj>.<method>(` calls; drop framework
    names. Used for the same purpose as the pytest `source_funcs_referenced`
    list — surface what the test actually exercises.
    """
    out: list[str] = []
    for m in re.finditer(r"\b([A-Za-z_$][\w$]*)(?:\s*\.\s*([A-Za-z_$][\w$]*))?\s*\(", body):
        name = m.group(1)
        attr = m.group(2)
        head = name.split(".")[0]
        if head in _FRAMEWORK_NAMES:
            continue
        if attr:
            out.append(f"{name}.{attr}")
        else:
            out.append(name)
    return out


# ---------------------------------------------------------------------------
# Helpers — module inventory
# ---------------------------------------------------------------------------

_EXPORT_RE = re.compile(
    r"\bexport\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function\s+([A-Za-z_$][\w$]*)|"
    r"const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?\(|"
    r"class\s+([A-Za-z_$][\w$]*))"
)


def _count_public_exports(text: str) -> int:
    """Approximate "public surface" as the number of named exports.

    Misses re-exports (`export { x } from`) and `export default <expr>` of
    anonymous values; that's a known approximation. The architecture review
    pass uses this as a relative signal, not an absolute count.
    """
    return len(_EXPORT_RE.findall(text))


_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:[\w*${},\s]+from\s+)?['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)
_REQUIRE_RE = re.compile(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]")


def _scan_test_imports(test_files: list[Path]) -> set[str]:
    """Collect every import specifier referenced by any test file."""
    out: set[str] = set()
    for tp in test_files:
        try:
            text = tp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out.update(_IMPORT_RE.findall(text))
        out.update(_REQUIRE_RE.findall(text))
    return out


def _module_referenced_in_imports(src: Path, imports: set[str], repo: Path) -> bool:
    """True if any test-file import specifier resolves to this src module.

    Matches conservatively on path-suffix and module stem. Avoids resolving
    tsconfig path aliases — too repo-specific.
    """
    rel = src.relative_to(repo) if src.is_relative_to(repo) else src
    rel_str = str(rel)
    stem = src.stem
    parent = str(rel.parent)
    for imp in imports:
        # "./foo", "../bar/baz" — match by suffix
        if imp.startswith("."):
            tail = imp.lstrip(".").lstrip("/")
            if rel_str.endswith(tail) or rel_str.endswith(tail + src.suffix):
                return True
            if tail == stem or tail.endswith("/" + stem):
                return True
        else:
            # bare specifier — match if last segment is the module stem AND
            # the src lives under a folder that matches the import root.
            if imp.split("/")[-1] == stem and parent.replace("\\", "/").endswith(
                "/".join(imp.split("/")[:-1])
            ):
                return True
    return False
