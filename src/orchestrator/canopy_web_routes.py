"""Every (METHOD, path) canopy sends to canopy-web, read statically from the source.

canopy talks to canopy-web from ~60 modules, each building a method and a path
string and handing them to `canopy_web.call` (or a thin wrapper of it). Nothing tied
those strings to the routes canopy-web serves: a route removed there kept "working"
here — every unit test uses a fake transport — until a real call 404'd.

This walks the AST of canopy's own source and lists every call whose first argument
is a literal HTTP method and whose second is a path it can work out statically: a
string, an f-string, a ``+``, a conditional, a module constant (also one imported
from another canopy module), a local variable, ``self.x`` set in the class, or the
return value of a small path helper (``_config_path(ws)`` → ``/api/slack-config/{}``).
Parameters become ``{}``, the query string is dropped and a tenant-scoped
``/api/w/{ws}/…`` path is reduced to the flat route canopy-web reroutes it to.

`tests/test_canopy_web_routes.py` checks the list against the vendored route table of
canopy-web's OpenAPI schema (`tests/fixtures/canopy_web_routes_schema.json`, refreshed
by ``python -m orchestrator.canopy_web_routes refresh-schema --checkout <canopy-web>``).
``python -m orchestrator.canopy_web_routes routes`` prints the list as JSON; canopy-web
vendors that output and fails a PR that removes a route canopy still calls.

A call it cannot work out (a path canopy-web handed back at run time, a presigned
upload URL, a value read from config) is listed under ``unresolved``: a known blind
spot, never a pass.
"""
from __future__ import annotations

import ast
import itertools
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

REPO_ROOT = Path(__file__).resolve().parents[2]
# Where canopy's canopy-web callers live. Tests are excluded: they talk to fakes.
SCAN_ROOTS = ("src/orchestrator", "plugins/canopy", "scripts")

PARAM = "{}"
_UNKNOWN = "\x00"   # a fragment canopy cannot read statically
_MAX_ALTS = 32       # cap on the alternatives one expression may fan out to
_MAX_DEPTH = 12
# Calls canopy-web does not serve, kept on purpose — each with its reason and its way
# out. `routes` prints them, so canopy-web's check skips them rather than failing on a
# route it already removed.
ALLOWED_MISSING = {
    ("GET", "/api/huddle-groups/{}"):
        "dead fallback in checkin_cli.normalize for a work-group run; canopy-web#1800 removed "
        "the /api/huddle-groups aliases, so no run names a `group` any more. Remove it with "
        "tests/test_huddle_group.py.",
    ("GET", "/api/huddle-groups/runs/{}"):
        "dead fallback in checkin_cli.get_run, tried only after /api/checkins/runs/{id} 404s; "
        "removed by canopy-web#1800. Same cleanup.",
    ("POST", "/api/huddle-groups/runs/{}/finished"):
        "dead fallback in checkin_cli.finish_run, tried only when /api/checkins/runs/{id}/"
        "finished is missing; removed by canopy-web#1800. Same cleanup.",
}

_NOT_PATH_BUILDERS = frozenset({"quote", "quote_plus", "urlencode", "str", "int", "get", "dumps"})


def normalise(path: str) -> str:
    """``/api/agents/{}/x`` from a path or template: params ``{}``, no query, flat."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = re.sub(r"\{[^{}]*\}", PARAM, path)
    path = path.replace(_UNKNOWN, PARAM)
    path = re.sub(r"(\{\})+", PARAM, path)
    # An empty segment is a parameter that rendered as "" (`f"/x/{rid}/y"`, rid = "").
    while "//" in path:
        path = path.replace("//", "/{}/")
    return re.sub(r"^/api/w/\{\}/", "/api/", path)


def _finish(s: str) -> Optional[str]:
    """The normalised canopy-web path in a rendered expression, or None."""
    i = s.find("/api/")
    if i < 0:
        return None
    prefix = s[:i]
    # Only a base URL may precede the path: unknown (`{base}`), or a literal host.
    if prefix and not (set(prefix) <= {_UNKNOWN} or prefix.startswith(("http://", "https://"))):
        return None
    return normalise(s[i:])


def _product(parts: list) -> list:
    out = ["".join(p) for p in itertools.product(*parts)]
    return list(dict.fromkeys(out))[:_MAX_ALTS]


class _Module:
    """One parsed file: its top-level names, functions and classes."""

    def __init__(self, path: Path, index: "_Index"):
        self.path = path
        self.index = index
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.assigns: dict = {}
        self.functions: dict = {}
        self.imports: dict = {}   # local name -> (module, attr or None)
        self.parents: dict = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.assigns.setdefault(t.id, []).append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
                self.assigns.setdefault(node.target.id, []).append(node.value)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
        for node in ast.walk(self.tree):  # module- and function-level imports alike
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.imports.setdefault(a.asname, (a.name, None))
                    else:
                        self.imports.setdefault(a.name.split(".")[0], (a.name.split(".")[0], None))
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for a in node.names:
                    self.imports.setdefault(a.asname or a.name, (node.module, a.name))

    # -- scope helpers --------------------------------------------------------

    def enclosing(self, node, kinds):
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, kinds):
                return node
        return None

    def module_named(self, alias: str) -> Optional["_Module"]:
        if alias not in self.imports:
            return None
        mod, attr = self.imports[alias]
        if attr is None:
            return self.index.module(mod, near=self.path)
        return self.index.module(f"{mod}.{attr}", near=self.path)

    def lookup_def(self, name: str):
        """(module, FunctionDef) for a helper ``name`` visible at top level."""
        if name in self.functions:
            return self, self.functions[name]
        if name in self.imports:
            mod, attr = self.imports[name]
            other = self.index.module(mod, near=self.path)
            if other is not None and attr and attr in other.functions:
                return other, other.functions[attr]
        return None

    # -- rendering ------------------------------------------------------------

    def render(self, node, env: dict, depth: int, seen: frozenset) -> list:
        """Every string ``node`` may evaluate to; unreadable fragments are _UNKNOWN."""
        if depth > _MAX_DEPTH:
            return [_UNKNOWN]
        d = depth + 1
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else [_UNKNOWN]
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append([str(v.value)])
                elif isinstance(v, ast.FormattedValue):
                    # Inside an f-string a value is part of the PATH only if it reads
                    # as a path (a base or prefix constant); anything else — an id, a
                    # slug, a literal default — is a parameter.
                    alts = [a for a in self.render(v.value, env, d, seen)
                            if a.startswith(("/", _UNKNOWN, "http")) or a == "" or a.startswith("?")]
                    parts.append(alts or [_UNKNOWN])
            return _product(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return _product([self.render(node.left, env, d, seen),
                             self.render(node.right, env, d, seen)])
        if isinstance(node, ast.IfExp):
            return list(dict.fromkeys(self.render(node.body, env, d, seen)
                                      + self.render(node.orelse, env, d, seen)))[:_MAX_ALTS]
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            out = []
            for v in node.values:
                out += self.render(v, env, d, seen)
            return list(dict.fromkeys(out))[:_MAX_ALTS]
        if isinstance(node, ast.Name):
            return self.render_name(node, env, d, seen)
        if isinstance(node, ast.Attribute):
            return self.render_attr(node, env, d, seen)
        if isinstance(node, ast.Call):
            return self.render_call(node, env, d, seen)
        return [_UNKNOWN]

    def render_name(self, node: ast.Name, env, d, seen) -> list:
        name = node.id
        if name in env:
            return env[name]
        func = self.enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        while func is not None:
            if not isinstance(func, ast.Lambda):
                key = (id(func), name)
                values = [n.value for n in ast.walk(func)
                          if isinstance(n, ast.Assign) and n.value is not None
                          and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
                          and self.enclosing(n, (ast.FunctionDef, ast.AsyncFunctionDef)) is func]
                values += [n.value for n in ast.walk(func)
                           if isinstance(n, ast.AnnAssign) and n.value is not None
                           and isinstance(n.target, ast.Name) and n.target.id == name]
                if values:
                    if key in seen:
                        return [_UNKNOWN]
                    out = []
                    for v in values:
                        out += self.render(v, env, d, seen | {key})
                    return list(dict.fromkeys(out))[:_MAX_ALTS]
                args = func.args
                if name in {a.arg for a in args.args + args.kwonlyargs + args.posonlyargs}:
                    return [_UNKNOWN]
            func = self.enclosing(func, (ast.FunctionDef, ast.AsyncFunctionDef))
        return self.constant(name, d, seen)

    def constant(self, name: str, d: int, seen: frozenset) -> list:
        key = (str(self.path), name)
        if key in seen:
            return [_UNKNOWN]
        seen = seen | {key}
        if name in self.assigns:
            out = []
            for v in self.assigns[name]:
                out += self.render(v, {}, d, seen)
            return list(dict.fromkeys(out))[:_MAX_ALTS]
        if name in self.imports:
            mod, attr = self.imports[name]
            other = self.index.module(mod, near=self.path)
            if other is not None and attr:
                return other.constant(attr, d, seen)
        return [_UNKNOWN]

    def render_attr(self, node: ast.Attribute, env, d, seen) -> list:
        base = node.value
        if isinstance(base, ast.Name) and base.id == "self":
            cls = self.enclosing(node, (ast.ClassDef,))
            if cls is not None:
                key = (id(cls), node.attr)
                if key in seen:
                    return [_UNKNOWN]
                values = [n.value for n in ast.walk(cls) if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                                  and t.value.id == "self" and t.attr == node.attr for t in n.targets)]
                values += [n.value for n in cls.body if isinstance(n, ast.Assign)
                           and any(isinstance(t, ast.Name) and t.id == node.attr for t in n.targets)]
                out = []
                for v in values:
                    out += self.render(v, env, d, seen | {key})
                return list(dict.fromkeys(out))[:_MAX_ALTS] or [_UNKNOWN]
            return [_UNKNOWN]
        if isinstance(base, ast.Name):
            other = self.module_named(base.id)
            if other is not None:
                return other.constant(node.attr, d, seen)
        return [_UNKNOWN]

    def render_call(self, node: ast.Call, env, d, seen) -> list:
        f = node.func
        # "...".format(...), path.replace(...), path.rstrip("/"): the receiver is the path.
        if isinstance(f, ast.Attribute) and f.attr in ("format", "replace", "strip", "rstrip", "lstrip"):
            return self.render(f.value, env, d, seen)
        target = None
        if isinstance(f, ast.Name):
            target = self.lookup_def(f.id)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            other = self.module_named(f.value.id)
            if other is not None and f.attr in other.functions:
                target = (other, other.functions[f.attr])
        if target is not None:
            got = self.inline(target, node, env, d, seen)
            if any(_finish(g) for g in got):
                return got
        # An HTTP call returns a response, and quote()/str()/urlencode() a parameter —
        # never a path, even when one is passed in.
        if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in METHODS:
            return [_UNKNOWN]
        fname = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
        if fname in _NOT_PATH_BUILDERS:
            return [_UNKNOWN]
        # A helper canopy cannot follow: the path is whichever argument is one.
        out = []
        for a in list(node.args) + [k.value for k in node.keywords]:
            out += [s for s in self.render(a, env, d, seen) if "/api/" in s]
        return list(dict.fromkeys(out))[:_MAX_ALTS] or [_UNKNOWN]

    def inline(self, target, call: ast.Call, env, d, seen) -> list:
        mod, fn = target
        key = ("fn", str(mod.path), fn.name)
        if key in seen:
            return [_UNKNOWN]
        params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
        if params and params[0] in ("self", "cls"):
            params = params[1:]
        bound: dict = {}
        for name, arg in zip(params, call.args):
            bound[name] = self.render(arg, env, d, seen)
        for kw in call.keywords:
            if kw.arg:
                bound[kw.arg] = self.render(kw.value, env, d, seen)
        defaults = fn.args.defaults
        for name, default in zip(params[len(params) - len(defaults):], defaults):
            bound.setdefault(name, mod.render(default, {}, d, seen))
        for a in fn.args.kwonlyargs:
            bound.setdefault(a.arg, [_UNKNOWN])
        for name in params:
            bound.setdefault(name, [_UNKNOWN])
        out = []
        for r in ast.walk(fn):
            if isinstance(r, ast.Return) and r.value is not None \
                    and mod.enclosing(r, (ast.FunctionDef, ast.AsyncFunctionDef)) is fn:
                out += mod.render(r.value, bound, d, seen | {key})
        return list(dict.fromkeys(out))[:_MAX_ALTS] or [_UNKNOWN]


class _Index:
    def __init__(self, files: Iterable[Path], root: Path):
        self.root = root
        self._loaded: dict = {}
        self._by_name: dict = {}
        self._by_dir_stem: dict = {}
        for f in files:
            self._by_dir_stem[(f.parent, f.stem)] = f
            try:
                rel = f.relative_to(root / "src")
            except ValueError:
                continue
            parts = list(rel.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            self._by_name[".".join(parts)] = f

    def load(self, path: Path) -> Optional[_Module]:
        if path not in self._loaded:
            try:
                self._loaded[path] = _Module(path, self)
            except SyntaxError:
                self._loaded[path] = None
        return self._loaded[path]

    def module(self, dotted: str, near: Optional[Path] = None) -> Optional[_Module]:
        f = self._by_name.get(dotted)
        if f is None and near is not None:
            # scripts/ddd/*.py import their siblings bare (`import run_store`).
            f = self._by_dir_stem.get((near.parent, dotted.split(".")[-1]))
        return self.load(f) if f else None


def source_files(root: Path = REPO_ROOT) -> list:
    out = []
    for sub in SCAN_ROOTS:
        for f in sorted((root / sub).rglob("*.py")):
            parts = f.relative_to(root).parts
            if "tests" in parts or f.name.startswith("test_") or "node_modules" in parts:
                continue
            out.append(f)
    return out


def extract(root: Path = REPO_ROOT) -> dict:
    """``{"calls": [{"method", "path", "where": [...]}, ...], "unresolved": [...]}``."""
    files = source_files(root)
    index = _Index(files, root)
    calls: dict = {}
    unresolved = []
    for f in files:
        mod = index.load(f)
        if mod is None:
            continue
        rel = f.relative_to(root).as_posix()
        for node in ast.walk(mod.tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            m = node.args[0]
            if not (isinstance(m, ast.Constant) and isinstance(m.value, str) and m.value in METHODS):
                continue
            where = f"{rel}:{node.lineno}"
            rendered = mod.render(node.args[1], {}, 0, frozenset())
            paths = sorted({p for p in (_finish(r) for r in rendered) if p})
            if not paths:
                if any(r.startswith(("/", _UNKNOWN)) for r in rendered):
                    unresolved.append({"method": m.value, "expr": ast.unparse(node.args[1]),
                                       "where": where})
                continue  # a URL on another host, not a canopy-web call
            for p in paths:
                calls.setdefault((m.value, p), []).append(where)
    return {
        "calls": [{"method": k[0], "path": k[1], "where": sorted(set(v))}
                  for k, v in sorted(calls.items(), key=lambda kv: (kv[0][1], kv[0][0]))],
        "unresolved": sorted(unresolved, key=lambda u: u["where"]),
    }


# --- Matching against canopy-web's schema ------------------------------------

def schema_routes(schema: dict) -> set:
    """``{(METHOD, normalised path)}`` an OpenAPI document (or its trimmed route table) serves."""
    out = set()
    for path, ops in (schema.get("paths") or {}).items():
        for method in ops:
            if method.upper() in METHODS:
                out.add((method.upper(), normalise(path)))
    return out


def _segments_match(call: str, route: str) -> bool:
    """Does canopy's path template ``call`` name schema route ``route``?

    Segment by segment: a call-side ``{}`` is a value canopy only knows at run time, so it
    matches any one segment; a schema-side ``{}`` matches only a call-side ``{}`` — a
    LITERAL that lands in a route parameter (``/api/agent-runs/projects/`` read as run
    ``projects``) is a removed route that happens to route somewhere, not a match.
    """
    a, b = call.split("/"), route.split("/")
    if len(a) != len(b):
        return False
    return all(x == y or x == PARAM for x, y in zip(a, b))


def missing(calls: list, routes: set) -> list:
    """The calls whose (method, path) canopy-web does not serve."""
    by_method: dict = {}
    for m, p in routes:
        by_method.setdefault(m, set()).add(p)
    out = []
    for c in calls:
        served = by_method.get(c["method"], set())
        # The trailing slash is part of a route. Only a GET survives a missing one:
        # Django's APPEND_SLASH answers 301 to `/x/` and urllib follows it; any other
        # method is refused (a 301 turns a POST into a GET).
        forms = [c["path"]]
        if c["method"] == "GET" and not c["path"].endswith("/"):
            forms.append(c["path"] + "/")
        if any(f in served or any(_segments_match(f, r) for r in served) for f in forms):
            continue
        out.append(c)
    return out


def trim_schema(schema: dict) -> dict:
    """The part of canopy-web's OpenAPI document this check reads: paths → methods →
    operationId. The full document is ~2 MB of request/response models canopy never
    reads; the route table is what drifts."""
    paths = {}
    for path, ops in sorted((schema.get("paths") or {}).items()):
        kept = {m: {"operationId": (op or {}).get("operationId", "")}
                for m, op in sorted(ops.items()) if m.upper() in METHODS}
        if kept:
            paths[path] = kept
    info = schema.get("info") or {}
    return {"openapi": schema.get("openapi", ""),
            "info": {"title": info.get("title", ""), "version": info.get("version", "")},
            "paths": paths}


SCHEMA_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "canopy_web_routes_schema.json"
DEFAULT_SCHEMA_URL = "https://canopy.dimagi.com/api/openapi.json"

# Builds canopy-web's schema in-process from a checkout — the same call its CI makes to
# regenerate `frontend/src/api/generated.ts`.
_DUMP_SCHEMA = (
    "import django, json, sys; django.setup(); "
    "from apps.api.api import api; "
    "json.dump(api.get_openapi_schema(), sys.stdout, default=str)"
)


def load_schema(*, checkout: Optional[str] = None, url: Optional[str] = None) -> dict:
    """canopy-web's OpenAPI document, from a checkout (built in-process, the code on
    that branch) or a running server (what is deployed)."""
    if checkout:
        import os
        import subprocess

        env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.test"}
        out = subprocess.run(["uv", "run", "python", "-c", _DUMP_SCHEMA], cwd=checkout,
                             env=env, check=True, capture_output=True, text=True).stdout
        return json.loads(out)
    import urllib.request

    with urllib.request.urlopen(url or DEFAULT_SCHEMA_URL, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: Optional[list] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m orchestrator.canopy_web_routes",
                                description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("routes", help="print every (METHOD, path) canopy sends to canopy-web, as JSON "
                                  "(the default; canopy-web vendors this output)")
    r = sub.add_parser("refresh-schema", help="rewrite the vendored canopy-web route table")
    src = r.add_mutually_exclusive_group()
    src.add_argument("--checkout", help="a canopy-web checkout to build the schema from "
                                        "(e.g. a worktree on origin/main)")
    src.add_argument("--url", help=f"a served openapi.json (default {DEFAULT_SCHEMA_URL})")
    args = p.parse_args(argv)
    if args.cmd == "refresh-schema":
        trimmed = trim_schema(load_schema(checkout=args.checkout, url=args.url))
        SCHEMA_FIXTURE.write_text(json.dumps(trimmed, indent=1, sort_keys=True) + "\n",
                                  encoding="utf-8")
        print(f"wrote {SCHEMA_FIXTURE.relative_to(REPO_ROOT)}: {len(trimmed['paths'])} paths",
              file=sys.stderr)
        return 0
    out = extract()
    out["allowed_missing"] = [{"method": m, "path": p, "reason": why}
                              for (m, p), why in sorted(ALLOWED_MISSING.items())]
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
