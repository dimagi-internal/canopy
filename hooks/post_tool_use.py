#!/usr/bin/env python3
"""Retired: canopy's PostToolUse capture hook. This file is a no-op.

Canopy no longer writes ~/.claude/canopy/session-log.jsonl — Claude Code's own
transcripts under ~/.claude/projects hold every tool call. The repo-map capture
it also did moved to the plugin's SessionStart hook
(plugins/canopy/hooks/repo_map_capture.py).

The file stays only because some machines' ~/.claude/settings.json still
register it on PostToolUse, and a missing file would make every tool call
error. `/canopy:setup` removes that registration.
"""

import sys

sys.exit(0)
