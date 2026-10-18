#!/usr/bin/env python3
"""Check that the code references in markdown docs still resolve.

Scans every inline backticked token (fenced code blocks are skipped) and every
relative markdown link target in the given files, and resolves the ones that
look like references to the repo:

  path                    apps/x/y.py, docs/a.md, apps/mcp/ — relative to the
                          repo root, then to the doc's own directory; a bare
                          filename (generated.ts) is found anywhere in the repo
  path::symbol            config/views.py::flat_artifact_gone — the file exists
                          and defines the symbol (Class.method checks the last part)
  path/mod.func           apps/agents/access.decide — module file apps/agents/
                          access.py or access/__init__.py defines `func`
  pkg.mod.func            apps.agents.access.decide — same, when the first
                          segment is a top-level directory of the repo
  [text](target)          a relative link target must exist beside the doc or
                          from the repo root

Tokens with placeholders (< > * { } … $), spaces, URLs and absolute paths are
skipped, as are slash tokens not rooted in this repo (routes like agents/new,
migration labels like contacts/0022) unless a repo file ends with them, and bare
filenames found nowhere in the repo (usually runtime files). Prints one MISSING / SYMBOL line per broken reference and a
"checked N, broken M" summary. Exit 1 if anything is broken.

    python3 check_refs.py [--root DIR] FILE.md [FILE.md ...]

Stdlib only.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

EXTENSIONS = {
    "py", "md", "ts", "tsx", "js", "jsx", "mjs", "cjs", "json", "yaml", "yml",
    "toml", "sh", "txt", "html", "css", "cfg", "ini", "lock", "sql", "rb", "go",
    "rs", "java", "kt", "swift", "astro", "vue", "svelte", "env", "tpl", "conf",
}
PLACEHOLDER = re.compile(r"[<>*{}$…]|\.\.\.")
INLINE_CODE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
MD_LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IDENT = re.compile(r"^[A-Za-z_]\w*$")
TRAILING = ".,;:)"


def repo_root(start: Path) -> Path:
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return start


def repo_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, check=True,
        ).stdout
        return out.splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]


def defines(path: Path, symbol: str) -> bool:
    """True if `path` defines `symbol` (strict for Python, a word match otherwise)."""
    name = symbol.split(".")[-1]
    if not IDENT.match(name):
        return True  # not something we can grep for; don't guess
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if path.suffix == ".py":
        pat = rf"^\s*(?:async\s+def|def|class)\s+{re.escape(name)}\b|^\s*{re.escape(name)}\s*(?::[^=\n]*)?="
        return re.search(pat, text, re.M) is not None
    return re.search(rf"\b{re.escape(name)}\b", text) is not None


def module_file(root: Path, module: str) -> Path | None:
    for cand in (root / f"{module}.py", root / module / "__init__.py"):
        if cand.is_file():
            return cand
    return None


class Checker:
    def __init__(self, root: Path):
        self.root = root
        self.files = repo_files(root)
        self.basenames = {Path(f).name for f in self.files}
        self.top_dirs = {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}

    def exists(self, token: str, doc_dir: Path) -> bool:
        rel = token.rstrip("/")
        if (self.root / rel).exists() or (doc_dir / rel).exists():
            return True
        return "/" not in rel and rel in self.basenames

    def resolve_dotted(self, parts: list[str]) -> tuple[str, str] | None:
        """Resolve ['apps','agents','access','decide'] to a module (+ symbol).

        The whole token may itself be a module (apps.tokens.middleware). Else the
        longest prefix that is a module file must define the next part. Returns
        ('ok', ''), ('symbol', module_file) or None when nothing is a module.
        """
        if module_file(self.root, "/".join(parts)) is not None:
            return ("ok", "")
        for cut in range(len(parts) - 1, 0, -1):
            mod = module_file(self.root, "/".join(parts[:cut]))
            if mod is not None:
                if defines(mod, parts[cut]):
                    return ("ok", "")
                return ("symbol", str(mod.relative_to(self.root)))
        return None

    def rooted(self, first: str, doc_dir: Path) -> bool:
        """The token's first segment exists at the repo root or beside the doc."""
        return (self.root / first).exists() or (doc_dir / first).exists()

    def suffix_match(self, token: str) -> bool:
        tail = "/" + token.rstrip("/")
        return any(("/" + f).endswith(tail) for f in self.files)

    def check_link(self, target: str, doc: Path) -> tuple[str, str] | None:
        """A relative markdown link must resolve beside the doc (or from the root)."""
        if PLACEHOLDER.search(target) or target.startswith(("/", "~")):
            return None
        if (doc.parent / target).exists() or (self.root / target).exists():
            return ("ok", "")
        return ("MISSING", target)

    def check(self, token: str, doc: Path) -> tuple[str, str] | None:
        """Return None if not a reference; ('ok', '') or (KIND, detail) otherwise."""
        token = token.strip().rstrip(TRAILING)
        if not token or PLACEHOLDER.search(token) or "://" in token:
            return None
        if token.startswith(("/", "~", "-", "#", "@")):
            return None
        token = re.sub(r":\d+(-\d+)?$", "", token)  # path:123
        if ".md#" in token:
            token = token.split("#", 1)[0]
        path, sep, symbol = token.partition("::")
        # Only plain path characters: rules out MIME types, routes with :params,
        # OIDC subjects, CSS classes, shell, quoted strings.
        if not re.match(r"^[\w./-]+$", path) or (sep and not re.match(r"^[\w.]+$", symbol)):
            return None
        doc_dir = doc.parent
        first = path.split("/", 1)[0]
        last = path.rstrip("/").rsplit("/", 1)[-1]
        ext = last.rsplit(".", 1)[-1] if "." in last.lstrip(".") else ""

        if sep:
            if not self.exists(path, doc_dir):
                return ("MISSING", path)
            target = self.root / path if (self.root / path).exists() else doc_dir / path
            if target.is_file() and not defines(target, symbol):
                return ("SYMBOL", f"{symbol} not defined in {path}")
            return ("ok", "")

        if "/" in path:
            if not self.rooted(first, doc_dir):
                # Not rooted in this repo: a route, a migration label, another
                # repo's file. Count it only if a repo file ends with it.
                if ext in EXTENSIONS and self.suffix_match(path):
                    return ("ok", "")
                return None
            if self.exists(path, doc_dir) or module_file(self.root, path.rstrip("/")):
                return ("ok", "")
            # A loader-relative name (Django templates/contacts/x.html) that a
            # repo file ends with.
            if ext in EXTENSIONS and self.suffix_match(path):
                return ("ok", "")
            # apps/agents/access.decide → module apps/agents/access + symbol decide
            if ext and ext not in EXTENSIONS:
                parts = path.rsplit("/", 1)[0].split("/") + last.split(".")
                res = self.resolve_dotted(parts) if all(IDENT.match(p) for p in last.split(".")) else None
                if res is not None:
                    if res[0] == "symbol":
                        return ("SYMBOL", f"{parts[-1]} not defined in {res[1]}")
                    return ("ok", "")
            return ("MISSING", path)

        if ext in EXTENSIONS:
            # A bare filename counts when it exists somewhere in the repo; one that
            # does not is usually a runtime file (ready.json), so it is skipped.
            return ("ok", "") if self.exists(path, doc_dir) else None

        # apps.agents.access.decide: only when rooted at a top-level repo dir
        parts = path.split(".")
        if len(parts) >= 3 and parts[0] in self.top_dirs and all(IDENT.match(p) for p in parts):
            res = self.resolve_dotted(parts)
            if res is None:
                return ("MISSING", path)
            if res[0] == "symbol":
                return ("SYMBOL", f"{parts[-1]} not defined in {res[1]}")
            return ("ok", "")
        return None


def tokens(text: str):
    """Yield (line_no, token, is_link) for inline code spans and relative link targets."""
    in_fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in INLINE_CODE.finditer(line):
            yield n, m.group(1), False
        for m in MD_LINK.finditer(line):
            target = m.group(1)
            if not target.startswith(("http:", "https:", "mailto:", "#")):
                target = target.split("#", 1)[0]
                if target:
                    yield n, target, True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--root", type=Path, help="repo root (default: git toplevel of the first file)")
    args = ap.parse_args(argv)

    root = (args.root or repo_root(args.files[0].resolve().parent)).resolve()
    checker = Checker(root)
    checked = broken = 0
    for doc in args.files:
        doc = doc.resolve()
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR {doc}: {exc}", file=sys.stderr)
            broken += 1
            continue
        try:
            shown = doc.relative_to(root)
        except ValueError:
            shown = doc
        for line_no, tok, is_link in tokens(text):
            res = checker.check_link(tok, doc) if is_link else checker.check(tok, doc)
            if res is None:
                continue
            checked += 1
            kind, detail = res
            if kind != "ok":
                broken += 1
                print(f"{kind} {shown}:{line_no} `{tok}` — {detail}")
    print(f"checked {checked}, broken {broken}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
