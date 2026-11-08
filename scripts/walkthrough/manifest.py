"""Build the canonical render manifest (walkthrough-run-data.json, superset).

The manifest is the one boundary object between the render engine
(``record_video.py``) and its consumers (the deck builder, ``scripts/ddd/upload.py``,
``assemble_run_state``, ``walkthrough-eval``, ``walkthrough-defect-creator``).

Ownership: the engine writes only capture facts (``url``, ``urls_visited``, paths,
``mp4_start_offset``, ``ok``, screenshots). ``ai_evaluation`` is a judgment overlay
the engine leaves null; a separate scoring step merges into it. The engine never scores.

Do not rename the file or remove a key: ``generate_presentation.py`` and the eval
fixtures read it by name (``slides[].screenshot_b64``, ``ai_evaluation``, ``personas``,
``name``, ``narrative``, ``generated_at``). Add keys only. Action telemetry stays in the
separate ``run-report.json``; do not merge the two.

``urls_visited`` is what the page actually showed, including URLs created at run time
(an on-camera audit page) that no ``scene.url`` template contains. Link lists for a
package come from it, never from the spec's raw ``url``.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any


def build_manifest(*, spec: dict, report: Any, snapshots_dir: Path, scenes_run: list[int],
                   scene_filter: str | None, substitution_vars: dict[str, str], generated_at: str,
                   duration_seconds: float = 0.0) -> dict:
    scenes = spec.get("scenes", []) or []
    personas = spec.get("personas", {}) or {}
    slides: list[dict] = []
    for idx, scene in enumerate(scenes, start=1):
        if idx not in scenes_run:
            continue
        png = snapshots_dir / f"scene_{idx}.png"
        timing = report.scene_timing_for(idx) if hasattr(report, "scene_timing_for") else {}
        b64 = base64.b64encode(png.read_bytes()).decode() if png.exists() else None
        page_text = snapshots_dir / f"scene_{idx}_page_text.json"
        slides.append({
            "type": "scene", "scene_index": idx, "scene_total": len(scenes),
            "title": scene.get("title", f"Scene {idx}"),
            "narration": scene.get("narrative") or scene.get("show") or "",
            "persona_key": scene.get("persona") or (next(iter(personas), "") if personas else ""),
            "url": scene.get("url") or "",
            "urls_visited": timing.get("urls_visited", []),
            "screenshot_path": f"{snapshots_dir.name}/{png.name}" if png.exists() else None,
            "page_text_path": f"{snapshots_dir.name}/{page_text.name}" if page_text.exists() else None,
            "screenshot_b64": b64,
            "mp4_start_offset": timing.get("start_seconds"),
            # recorded scenes are ok by construction: a failed must_succeed action aborts the render
            "ok": True,
            "ai_evaluation": None,
        })
    return {
        "name": spec.get("name", ""), "narrative": spec.get("narrative", ""),
        "generated_at": generated_at, "duration_seconds": duration_seconds,
        "base_url": (spec.get("base_url") or "").rstrip("/"),
        "scenes_run": list(scenes_run), "scene_filter": scene_filter,
        "substitution_vars": dict(substitution_vars or {}),
        "personas": {k: {"name": v.get("name", k), "role": v.get("role", ""),
                         "color": v.get("color", "#4F46E5"), "intro": v.get("intro", "")}
                     for k, v in personas.items()},
        "slides": slides,
    }
