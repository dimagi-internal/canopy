#!/usr/bin/env python3
"""Legacy-compat shim for the canopy PostToolUse capture hook.

The canonical implementation moved to plugins/canopy/hooks/post_tool_use.py,
which the PLUGIN registers itself via plugins/canopy/hooks/hooks.json
(${CLAUDE_PLUGIN_ROOT} — version-locked to the installed cache). This shim
exists only for machines whose ~/.claude/settings.json still carries the old
checkout-path registration; it forwards to the canonical copy so there is one
implementation. The plugin-managed copy defers while a working legacy entry is
present (see _legacy_capture_registered there), so the two never double-log.
`/canopy:setup` removes the legacy entry, after which this file is unused.

Exit 0 always — hook failures must never block Claude Code.
"""

import os
import runpy
import sys

try:
    os.environ["CANOPY_CAPTURE_LEGACY_SHIM"] = "1"
    _target = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "plugins", "canopy", "hooks", "post_tool_use.py",
    )
    runpy.run_path(_target, run_name="__main__")
except Exception:
    pass
sys.exit(0)
