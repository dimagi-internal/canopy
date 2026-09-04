"""Architecture boundary test for the canopy plugin's `orchestrator` package.

Same one-way invariant as canopy-web (see src/orchestrator/TIERS.md): FRAMEWORK
modules (the generic, agent-agnostic agent-runtime substrate) must never import
PRODUCT modules (canopy's own self-improvement / DDD / portfolio features).
PRODUCT and the orchestration HUBS may import framework freely.

Unlike canopy-web (separate Django apps) the plugin is one importable package, so
the tiers are per-module. Pure stdlib `ast` — no new dependency.

Failing? Either move the product import out of the framework module, or — if you
added a genuine new orchestration hub — add it to HUBS here and in TIERS.md with a
reason. Don't relabel a module just to pass.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "orchestrator"

# ── Tiers (canonical copy lives in src/orchestrator/TIERS.md; keep in sync) ─────
FRAMEWORK = {
    "agent_cli", "agent_client", "agent_coverage", "agent_doctor", "agent_email", "agent_gdoc", "review_receipt", "agent_factory", "agent_health", "agent_web", "canopy_web",
    "inbox_filters",
    "capture", "transcripts", "scanner", "circuit_breaker", "rate_limiter",
    "scheduler", "paths", "repo_map", "repo_paths", "repo_evidence",
    "skill_budget", "skill_catalog", "skill_runner", "provision", "run_log",
    "version_bump", "version", "doctor", "agent_review", "structure_drift",
    "eval_cli", "eval_rubric", "turn_synthesis", "session_upload", "fleet_align",
    "session_sources", "session_liveness", "work_cursor", "agent_dispatch", "llm_output",
    "project_dispatch", "project_cli", "runner_cli",
}
# Orchestration hubs / composition roots — wire product features into the CLI, the
# improvement pipeline, and the web server. Allowed to import product (like
# canopy-web's `api` app). Kept deliberately small.
HUBS = {"cli", "pipeline", "server"}
PRODUCT = {
    "analyzer", "proposer", "reviewer", "briefing", "observations", "proposals",
    "campaigns", "tracker", "labels", "patterns", "router", "digest", "harvest",
    "shareout", "portfolio_discover", "openclaw_harvest",
    "issue_origin", "verify_findings", "corpus", "test_audit", "prompts",
}


def _imported_orchestrator_modules(path: pathlib.Path) -> set[str]:
    """Submodules of `orchestrator` that this file imports."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "orchestrator":
                out.update(a.name for a in node.names)          # from orchestrator import X
            elif node.module.startswith("orchestrator."):
                out.add(node.module.split(".")[1])              # from orchestrator.X import ...
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("orchestrator."):
                    out.add(alias.name.split(".")[1])           # import orchestrator.X
    return out


def _code_modules() -> set[str]:
    """Every importable top-level module / subpackage of orchestrator (sans __init__)."""
    mods = {p.stem for p in PKG.glob("*.py") if p.stem != "__init__"}
    mods |= {p.name for p in PKG.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    return mods


def test_framework_modules_do_not_import_product() -> None:
    violations: list[str] = []
    for mod in sorted(FRAMEWORK):
        path = PKG / f"{mod}.py"
        if not path.exists():  # framework subpackages are all single modules today
            continue
        leaked = _imported_orchestrator_modules(path) & PRODUCT
        if leaked:
            violations.append(f"{mod}.py imports product module(s) {sorted(leaked)}")
    assert not violations, (
        "FRAMEWORK modules must not import PRODUCT modules (see src/orchestrator/TIERS.md):\n  "
        + "\n  ".join(violations)
    )


def test_every_orchestrator_module_is_tiered() -> None:
    """A new module can't dodge the boundary — it must be tiered explicitly."""
    assert FRAMEWORK.isdisjoint(PRODUCT) and FRAMEWORK.isdisjoint(HUBS) and PRODUCT.isdisjoint(HUBS)
    untiered = _code_modules() - (FRAMEWORK | HUBS | PRODUCT)
    assert not untiered, (
        f"untiered orchestrator module(s) {sorted(untiered)} — add each to "
        "FRAMEWORK / HUBS / PRODUCT here and in src/orchestrator/TIERS.md"
    )
