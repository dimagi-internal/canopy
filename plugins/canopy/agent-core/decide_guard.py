#!/usr/bin/env python3
"""RETIRED — the fleet's decide-don't-offer rail is now a native prompt hook.

A regex Stop-hook engine cannot hold this rail: the shapes of an offered call ("It's a small
canopy PR if you want it.", "I'd ship all three now … unless you'd rather …", "… would be a
small follow-up.") are open-ended, and a pattern per shape never keeps up.

The rail is now a `{"type": "prompt"}` Stop hook: an LLM judges the closing message
against labelled examples in `agent-core/decide_guard_examples.jsonl`. It is a per-person
opt-in (`canopy decide-guard stamp --user`), never wired by the plugin or an agent repo.

This stub stays so an agent repo whose `hooks/decide_guard.py` loader has not been
removed yet keeps working harmlessly: it reads nothing, prints nothing, exits 0.
"""
import sys

if __name__ == "__main__":
    sys.exit(0)
