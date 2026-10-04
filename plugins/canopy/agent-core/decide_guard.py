#!/usr/bin/env python3
"""RETIRED — the fleet's decide-don't-offer rail is now a native prompt hook.

This file used to be a regex Stop-hook engine that blocked once per session when an
agent's closing message handed back a call it was authorized to make ("Want me to ship
the PR?"). It was whack-a-mole: on 2026-10-04 a single ada session got past it three
times with shapes nobody had written a pattern for ("It's a small canopy PR if you want
it.", "I'd ship all three now … unless you'd rather …", "… would be a small follow-up.").

The rail now lives in the canopy plugin's `hooks/hooks.json` as a `{"type": "prompt"}`
Stop hook: an LLM judges the closing message against labelled examples in
`agent-core/decide_guard_examples.jsonl` (rendered by `canopy decide-guard render`). It
fires in every session with canopy installed, so agent repos no longer wire their own.

This stub stays so an agent repo whose `hooks/decide_guard.py` loader has not been
removed yet keeps working harmlessly: it reads nothing, prints nothing, exits 0.
"""
import sys

if __name__ == "__main__":
    sys.exit(0)
