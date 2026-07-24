"""Enforce the bundled-runtime resolution idiom in plugin markdown.

The canopy plugin's Python runtime ships INSIDE the installed plugin cache
(<installPath>/runtime/, synced per .claude-plugin/runtime.json), resolved by
the single shared resolver scripts/canopy-runtime.sh. Skills must never reach
for a dev checkout or the marketplace clone directly — that is the drift class
(stale global CLI, checkout-only scripts, three competing resolution idioms)
this architecture removed. See CLAUDE.md § Plugin Runtime Bundle.

Every hit of a banned pattern outside the explicit allowlist fails this test.
If you are adding a genuinely new exception, add it to ALLOWED with a comment
saying WHY it cannot go through the resolver.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).parent.parent
PLUGIN = REPO / "plugins" / "canopy"

# Negative lookahead so canopy-web / canopy-findings-shots don't match.
BANNED = [
    # Direct dev-checkout references to the canopy repo.
    re.compile(r"emdash-projects/canopy(?![\w-])"),
    re.compile(r"emdash/repositories/canopy(?![\w-])"),
    # Direct marketplace-clone references (the clone is the UPDATE CHANNEL,
    # not a runtime root — only the updaters/bootstrap may touch it).
    re.compile(r"marketplaces/canopy(?![\w-])"),
    # The runtime CLI must be invoked through the resolved project, so it is
    # version-locked to the plugin ("uv run --project "$CANOPY_ROOT" canopy").
    re.compile(r"uv run canopy(?![\w-])"),
]

# path (relative to plugins/canopy) -> why it may reference these locations.
ALLOWED = {
    # The update skill IS the update channel: it pulls the marketplace clone,
    # rsyncs the cache, and deploys the convenience CLI from that clone.
    "skills/update/SKILL.md",
    # Bootstrap fallback: a cache that predates the runtime bundle has no
    # runtime/scripts/canopy-setup.sh yet; the marketplace clone always does.
    "commands/setup.md",
    # video-engine is the ONE documented dev-checkout exception — Remotion's
    # node_modules is too heavy to bundle per plugin version, so local render
    # resolves a dev checkout where /canopy:setup ran `npm ci`.
    "skills/ddd-ace-render/SKILL.md",
    "skills/ddd-timing-eval/SKILL.md",
    # Diagnostics: doctor inspects the update channel and dev checkouts to
    # report on them — reading their state is its job, not a runtime dep.
    "skills/canopy-doctor/SKILL.md",
}


def _md_files():
    return sorted(PLUGIN.rglob("*.md"))


def test_no_checkout_or_marketplace_refs_in_plugin_markdown():
    violations: list[str] = []
    for path in _md_files():
        rel = str(path.relative_to(PLUGIN))
        if rel in ALLOWED:
            continue
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern in BANNED:
                if pattern.search(line):
                    violations.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    assert not violations, (
        "Plugin markdown must resolve the canopy runtime via "
        "scripts/canopy-runtime.sh (see CLAUDE.md § Plugin Runtime Bundle), "
        "not reference checkouts/marketplace clones directly:\n"
        + "\n".join(violations)
    )


def test_allowlist_entries_exist():
    """A stale allowlist silently widens the ban's blind spots."""
    for rel in ALLOWED:
        assert (PLUGIN / rel).is_file(), f"ALLOWED entry no longer exists: {rel}"


def test_resolver_script_ships_in_plugin():
    resolver = PLUGIN / "scripts" / "canopy-runtime.sh"
    assert resolver.is_file()
    text = resolver.read_text()
    # The ladder's production tier must be the in-cache bundle.
    assert "runtime" in text and "marketplaces/canopy" in text


def test_runtime_manifest_paths_exist():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "runtime.json").read_text())
    assert manifest.get("dest") == "runtime"
    for rel in manifest["paths"]:
        assert (REPO / rel).exists(), f"runtime.json path missing in repo: {rel}"
    # The runtime bundle must carry what skills execute.
    assert "src" in manifest["paths"]
    assert "scripts" in manifest["paths"]
    assert "pyproject.toml" in manifest["paths"]
