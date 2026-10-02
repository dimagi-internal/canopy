"""#625 — a render must not silently change frame size mid-loop.

Origin: ACE Phase 7 run spark-fcap-facilitation-2026-09-08-001 rendered
iterations 0-2 at 1440x900; a spec rebuilt after a killed session dropped the
viewport keys and iteration 3 re-rendered at 1280x720 with only a banner line
to show for it.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough._lib.viewport_guard import (  # noqa: E402
    RECORD_NAME,
    check_viewport,
    png_size,
    record_viewport,
)


def _png(path: Path, w: int, h: int) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b""))


def test_png_size_reads_ihdr(tmp_path):
    _png(tmp_path / "a.png", 1440, 900)
    assert png_size(tmp_path / "a.png") == (1440, 900)
    (tmp_path / "b.png").write_text("not a png")
    assert png_size(tmp_path / "b.png") is None


def test_first_render_is_ok(tmp_path):
    assert check_viewport(tmp_path / "missing", 1280, 720).verdict == "ok"
    assert check_viewport(tmp_path, 1280, 720).verdict == "ok"


def test_recorded_mismatch_refuses_and_names_the_fix(tmp_path):
    record_viewport(tmp_path, 1440, 900)
    res = check_viewport(tmp_path, 1280, 720)
    assert res.verdict == "refuse"
    assert "1440x900" in res.message and "1280x720" in res.message
    assert "video_viewport_width: 1440" in res.message
    assert "--allow-viewport-change" in res.message


def test_recorded_match_is_ok(tmp_path):
    record_viewport(tmp_path, 1440, 900)
    assert check_viewport(tmp_path, 1440, 900).verdict == "ok"


def test_allow_change_downgrades_to_warn(tmp_path):
    record_viewport(tmp_path, 1440, 900)
    assert check_viewport(tmp_path, 1280, 720, allow_change=True).verdict == "warn"


def test_height_only_change_is_caught(tmp_path):
    record_viewport(tmp_path, 1280, 1000)
    assert check_viewport(tmp_path, 1280, 720).verdict == "refuse"


def test_legacy_frames_without_record_warn_not_refuse(tmp_path):
    _png(tmp_path / "scene_1.png", 1440, 900)
    _png(tmp_path / "scene_2.png", 1440, 2400)  # full-page: height varies, width doesn't
    res = check_viewport(tmp_path, 1280, 720)
    assert res.verdict == "warn"
    assert "1440px" in res.message


def test_legacy_frames_matching_width_are_ok(tmp_path):
    _png(tmp_path / "scene_1.png", 1280, 3000)
    assert check_viewport(tmp_path, 1280, 720).verdict == "ok"


def test_legacy_inference_skips_overridden_scenes_and_before_frames(tmp_path):
    _png(tmp_path / "scene_1.png", 1280, 720)
    _png(tmp_path / "scene_2.png", 390, 844)  # scene 2 has its own mobile viewport
    _png(tmp_path / "scene_1_before.png", 999, 720)  # not a canonical frame
    assert check_viewport(tmp_path, 1280, 720, overridden_scenes={2}).verdict == "ok"


def test_ambiguous_legacy_widths_do_not_warn(tmp_path):
    _png(tmp_path / "scene_1.png", 1280, 720)
    _png(tmp_path / "scene_2.png", 1440, 900)
    assert check_viewport(tmp_path, 1280, 720).verdict == "ok"


def test_record_is_rewritten_each_render(tmp_path):
    record_viewport(tmp_path, 1440, 900)
    record_viewport(tmp_path, 1280, 720)
    assert (tmp_path / RECORD_NAME).exists()
    assert check_viewport(tmp_path, 1280, 720).verdict == "ok"
