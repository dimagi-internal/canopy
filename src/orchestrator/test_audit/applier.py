"""Apply verdicts: prune (delete) or skip-mark (env-fragile) tests, then open a PR.

`Verdict` lives here since this module is the consumer. The skill writes
`verdicts.yaml` with whatever shape it likes; `apply_from_dir` parses it
into Verdict objects and runs `plan` + `_execute`.

Conservative rules (match spec):
  verdict=prune, reason_code=env-fragile  -> add @pytest.mark.skip(...)
  verdict=prune, score <= 3                -> git rm the test
  verdict=prune, score 4-6                 -> only with aggressive=True

`refactor` and `investigate` verdicts are NEVER applied.
"""
from __future__ import annotations

import ast
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from orchestrator.test_audit.collector import TestItem
from orchestrator.test_audit.framework import FrameworkAdapter, detect_framework

logger = logging.getLogger(__name__)


@dataclass
class Verdict:
    nodeid: str
    score: int
    verdict: str  # keep | refactor | prune | investigate
    reason_code: str
    reason: str = ""


@dataclass
class PlannedChange:
    nodeid: str
    file: Path
    action: str  # "delete" | "skip"
    reason: str


@dataclass
class ApplyResult:
    changes: list[PlannedChange] = field(default_factory=list)
    branch: str | None = None
    pr_url: str | None = None
    patch_path: Path | None = None
    skipped: list[str] = field(default_factory=list)
    error: str | None = None


def plan(items_by_id: dict[str, TestItem], verdicts: dict[str, Verdict],
         aggressive: bool = False, supports_delete: bool = True) -> list[PlannedChange]:
    """Return the list of changes the applier would make.

    `supports_delete=False` (vitest) downgrades delete actions to skip-mark —
    JS/TS deletion needs a real parser to be safe (see SKILL.md), so v1
    leaves deletion to humans and the applier just marks `.skip`.
    """
    out: list[PlannedChange] = []
    for nid, v in verdicts.items():
        if v.verdict != "prune":
            continue
        item = items_by_id.get(nid)
        if item is None:
            continue
        if v.reason_code == "env-fragile":
            out.append(PlannedChange(nodeid=nid, file=item.file, action="skip",
                                     reason=v.reason or v.reason_code))
        elif v.score <= 3 or aggressive:
            action = "delete" if supports_delete else "skip"
            out.append(PlannedChange(nodeid=nid, file=item.file, action=action,
                                     reason=v.reason or v.reason_code))
    return out


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _gh_available() -> bool:
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def _delete_test(file: Path, name: str) -> bool:
    """Remove a test function from `file`. Returns True if changed."""
    src = file.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    target_lines: tuple[int, int] | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            start = node.lineno - 1
            end = (getattr(node, "end_lineno", None) or node.lineno) - 1
            for dec in node.decorator_list:
                start = min(start, dec.lineno - 1)
            target_lines = (start, end)
            break
    if target_lines is None:
        return False
    lines = src.splitlines(keepends=True)
    new = lines[: target_lines[0]] + lines[target_lines[1] + 1 :]
    file.write_text("".join(new), encoding="utf-8")
    return True


def _skip_mark_test(file: Path, name: str, reason: str) -> bool:
    """Add `@pytest.mark.skip(reason=...)` to the test function."""
    src = file.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            target = node
            break
    if target is None:
        return False

    for dec in target.decorator_list:
        text = ast.unparse(dec) if hasattr(ast, "unparse") else ""
        if "pytest.mark.skip" in text:
            return False

    insert_line = target.lineno - 1
    lines = src.splitlines(keepends=True)
    indent = re.match(r"\s*", lines[insert_line]).group(0)
    safe_reason = (reason or "audit: env-fragile").replace('"', "'")[:140]
    decorator = f'{indent}@pytest.mark.skip(reason="audit: {safe_reason}")\n'
    lines.insert(insert_line, decorator)

    new_src = "".join(lines)
    if "import pytest" not in new_src:
        try:
            tree2 = ast.parse(new_src)
            insert_at = 0
            if (tree2.body and isinstance(tree2.body[0], ast.Expr)
                    and isinstance(tree2.body[0].value, ast.Constant)
                    and isinstance(tree2.body[0].value.value, str)):
                insert_at = (tree2.body[0].end_lineno or 1)
            lines2 = new_src.splitlines(keepends=True)
            lines2.insert(insert_at, "import pytest\n")
            new_src = "".join(lines2)
        except SyntaxError:
            pass

    file.write_text(new_src, encoding="utf-8")
    return True


def apply_verdicts(items_by_id: dict[str, TestItem], verdicts: dict[str, Verdict],
                   repo: Path, aggressive: bool = False, dry_run: bool = False,
                   pr_body_path: Path | None = None,
                   branch_prefix: str = "test-audit",
                   adapter: FrameworkAdapter | None = None) -> ApplyResult:
    """Plan + execute changes. Open a PR (or write a patch file if gh is missing).

    `adapter` is the framework backend that does the actual file edits.
    Defaults to auto-detection on `repo` so callers that pre-built
    `items_by_id` (which was already framework-specific) don't need to wire
    it through.
    """
    if adapter is None:
        adapter = detect_framework(repo)
    changes = plan(items_by_id, verdicts, aggressive=aggressive,
                   supports_delete=adapter.supports_delete())
    res = ApplyResult(changes=changes)
    if not changes:
        return res
    if dry_run:
        return res

    by_file: dict[Path, list[PlannedChange]] = {}
    for c in changes:
        by_file.setdefault(c.file, []).append(c)

    for file, file_changes in by_file.items():
        for c in [x for x in file_changes if x.action == "delete"]:
            name = c.nodeid.split("::")[-1]
            if not adapter.apply_delete(file, name):
                res.skipped.append(c.nodeid)
        for c in [x for x in file_changes if x.action == "skip"]:
            name = c.nodeid.split("::")[-1]
            if not adapter.apply_skip(file, name, c.reason):
                res.skipped.append(c.nodeid)

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    branch = f"{branch_prefix}/{stamp}"
    res.branch = branch

    git_status = _git("status", "--porcelain", cwd=repo)
    if not git_status.stdout.strip():
        return res

    cur = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout.strip()
    _git("checkout", "-b", branch, cwd=repo)
    _git("add", "-A", cwd=repo)
    body = (
        f"Test audit: {len([c for c in changes if c.action=='delete'])} deletions, "
        f"{len([c for c in changes if c.action=='skip'])} skip-marks. "
        "See PR body for the full report."
    )
    _git("commit", "-m", body, cwd=repo)

    if _gh_available():
        push = _git("push", "-u", "origin", branch, cwd=repo)
        if push.returncode != 0:
            res.error = f"git push failed: {push.stderr[:200]}"
            return res
        body_arg = ["--body", body]
        if pr_body_path and pr_body_path.exists():
            body_arg = ["--body-file", str(pr_body_path)]
        pr = subprocess.run(
            ["gh", "pr", "create", "--title", f"test-audit: {stamp}", *body_arg],
            cwd=repo, capture_output=True, text=True,
        )
        if pr.returncode == 0:
            url = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else ""
            res.pr_url = url
        else:
            res.error = f"gh pr create failed: {pr.stderr[:200]}"
    else:
        patch = subprocess.run(["git", "format-patch", "-1", "--stdout"],
                               cwd=repo, capture_output=True, text=True)
        patch_path = repo / ".canopy" / "test-audits" / f"{stamp}.patch"
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text(patch.stdout, encoding="utf-8")
        res.patch_path = patch_path

    _git("checkout", cur, cwd=repo)
    return res


# --- High-level: read verdicts.yaml from a stamp dir and act on it ---

def _parse_verdicts_yaml(data: dict | list) -> dict[str, Verdict]:
    """Accept either {'verdicts': [...]} or a top-level list of verdict dicts."""
    out: dict[str, Verdict] = {}
    entries: list[dict] = []
    if isinstance(data, dict):
        entries = list(data.get("verdicts", []))
    elif isinstance(data, list):
        entries = data
    for e in entries:
        if not isinstance(e, dict) or "nodeid" not in e:
            continue
        nid = str(e["nodeid"])
        out[nid] = Verdict(
            nodeid=nid,
            # `score: null` is what SKILL.md tells the judge to write for
            # not-sampled tests on a large suite, so it must parse. int(None)
            # raises, which took down a 6,012-test audit at the apply step.
            score=int(e.get("score") or 0),
            verdict=str(e.get("verdict", "investigate")).lower(),
            reason_code=str(e.get("reason_code", "unknown")),
            reason=str(e.get("reason", "") or ""),
        )
    return out


def apply_from_dir(stamp_dir: Path, repo: Path | None = None,
                   aggressive: bool = False, dry_run: bool = False,
                   framework: str | None = None) -> ApplyResult:
    """Read `<stamp_dir>/verdicts.yaml`, re-collect tests in `repo`, apply.

    If `repo` is omitted, infer from `stamp_dir` assuming it lives at
    `<repo>/.canopy/test-audits/<stamp>/`. If `framework` is omitted, the
    framework is auto-detected from `repo` (or read from corpus.yaml's
    `framework:` field if present, for cases where the repo signal moved
    since `collect` was run).
    """
    verdicts_path = stamp_dir / "verdicts.yaml"
    if not verdicts_path.exists():
        raise FileNotFoundError(f"verdicts.yaml not found in {stamp_dir}")
    if repo is None:
        repo = stamp_dir.parent.parent.parent
    repo = repo.resolve()

    if framework is None:
        corpus_path = stamp_dir / "corpus.yaml"
        if corpus_path.exists():
            try:
                corpus = yaml.safe_load(corpus_path.read_text(encoding="utf-8")) or {}
                framework = corpus.get("framework")
            except yaml.YAMLError:
                framework = None
    adapter = detect_framework(repo, override=framework)

    data = yaml.safe_load(verdicts_path.read_text(encoding="utf-8")) or {}
    verdicts = _parse_verdicts_yaml(data)
    items_by_id = {it.nodeid: it for it in adapter.collect(repo)}

    pr_body_path = _materialize_pr_body(stamp_dir)
    return apply_verdicts(
        items_by_id, verdicts, repo,
        aggressive=aggressive, dry_run=dry_run,
        pr_body_path=pr_body_path,
        adapter=adapter,
    )


def _materialize_pr_body(stamp_dir: Path) -> Path | None:
    """Combine audit-report.md + architecture-review.md (if both exist) into
    a single pr-body.md the applier hands to gh. If only one exists, use it.
    """
    audit = stamp_dir / "audit-report.md"
    arch = stamp_dir / "architecture-review.md"
    if not audit.exists() and not arch.exists():
        return None
    if audit.exists() and not arch.exists():
        return audit
    if arch.exists() and not audit.exists():
        return arch
    combined = stamp_dir / "pr-body.md"
    combined.write_text(
        audit.read_text(encoding="utf-8").rstrip() + "\n\n---\n\n"
        + arch.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return combined
