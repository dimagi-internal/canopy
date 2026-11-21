"""The vendored stored-agent helpers (orchestrator/agent_definition.py).

`manifest_root` must equal canopy-web's `canopy_sdk.agent_definition.manifest_root` byte for
byte: a materialized revision is refused when its root does not match the header canopy-web
sends. The canonical form is pinned here as a literal, not re-derived.
"""
from __future__ import annotations

import hashlib

from orchestrator.agent_definition import (canonical_manifest, check_tree, classify_path,
                                           file_entry, manifest_root)

A = b"# helper\n"
B = b"#!/bin/sh\n"


def test_canonical_serialization_is_pinned():
    tree = {
        "bin/helper-email": {"sha256": hashlib.sha256(B).hexdigest(), "size": len(B),
                             "executable": True},
        "CLAUDE.md": {"sha256": hashlib.sha256(A).hexdigest(), "size": len(A),
                      "executable": False},
    }
    expected = (
        '[{"executable":false,"path":"CLAUDE.md","sha256":"'
        + hashlib.sha256(A).hexdigest() + '","size":9},'
        '{"executable":true,"path":"bin/helper-email","sha256":"'
        + hashlib.sha256(B).hexdigest() + '","size":10}]'
    ).encode()
    assert canonical_manifest(tree) == expected
    assert manifest_root(tree) == hashlib.sha256(expected).hexdigest()
    # Entries in any order, as a list, give the same root as the tree form.
    entries = [file_entry("bin/helper-email", B, True), file_entry("CLAUDE.md", A)]
    assert manifest_root(entries) == manifest_root(tree)
    # The SDK must produce this exact root for these two files (shared test vector).
    assert manifest_root(tree) == "b90fd0b7d22806c3d6402f4996bb0b1bcd2a1978b40769aefca00be68b1b97d4"


def test_root_is_sorted_by_path_and_empty_tree():
    assert canonical_manifest({}) == b"[]"
    assert manifest_root([]) == hashlib.sha256(b"[]").hexdigest()


def test_classify():
    assert classify_path("CLAUDE.md", "helper") == "editable"
    assert classify_path("skills/turn/SKILL.md", "helper") == "editable"
    assert classify_path("skills/turn/refs/x.yaml", "helper") == "editable"
    assert classify_path("tests/scenarios/a.json", "helper") == "editable"
    assert classify_path("docs/a/b.md", "helper") == "editable"
    assert classify_path("config/agent.json", "helper") == "editable"
    for p in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
              ".claude/settings.json", "bin/helper-email", ".gitignore"):
        assert classify_path(p, "helper", p.startswith("bin/")) == "generated"
    for p in ("tests/test_x.py", "skills/turn/run.py", "bin/_env.py", "hooks/hooks.json",
              ".github/workflows/t.yml", "../x.md", "/abs.md", "a\\b.md", ".git/config",
              "docs/x.txt", "skills/SKILL.md"):
        assert classify_path(p, "helper").startswith("refused"), p
    assert classify_path("persona.md", "helper", executable=True).startswith("refused")


def test_check_tree():
    ok = {"CLAUDE.md": {"content": "x"}, "skills/a/SKILL.md": {"content": b"y"},
          "docs/old.md": {"content": None}}
    assert check_tree(ok, "helper") == []
    bad = check_tree({
        "persona.md": {"content": "token: ghp_" + "a" * 40},
        "README.md": {"content": b"\xff\xfe"},
        "CLAUDE.md": {"content": "x" * (256 * 1024 + 1)},
        ".gitignore": {"content": ".env"},
        "skills/a/SKILL.md": {"content": "x", "symlink": True},
    }, "helper")
    text = "\n".join(bad)
    assert "persona.md: looks like it holds a secret (GitHub token)" in text
    assert "README.md: not UTF-8" in text
    assert "CLAUDE.md: 262145 bytes" in text
    assert ".gitignore: written by canopy-web" in text
    assert "symlinks are refused" in text
    assert check_tree({".gitignore": {"content": ".env"}}, "helper", allow_generated=True) == []
    many = {f"docs/{i}.md": {"content": "x"} for i in range(201)}
    assert any("201 files" in p for p in check_tree(many, "helper"))
