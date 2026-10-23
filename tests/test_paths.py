"""Tests for orchestrator.paths module."""

from pathlib import Path

import pytest

from orchestrator.paths import CANOPY_DIR, _LEGACY_DIR, ensure_canopy_dir


class TestCanopyDirConstant:
    def test_canopy_dir_is_under_home(self):
        assert CANOPY_DIR.parts[-2:] == (".claude", "canopy")

    def test_canopy_dir_is_absolute(self):
        assert CANOPY_DIR.is_absolute()

    def test_legacy_dir_is_under_home(self):
        assert _LEGACY_DIR.parts[-2:] == (".claude", "orchestrator")


class TestEnsureCanopyDir:
    def test_creates_canopy_dir_when_nothing_exists(self, tmp_path, monkeypatch):
        canopy = tmp_path / ".claude" / "canopy"
        legacy = tmp_path / ".claude" / "orchestrator"
        monkeypatch.setattr("orchestrator.paths.CANOPY_DIR", canopy)
        monkeypatch.setattr("orchestrator.paths._LEGACY_DIR", legacy)
        result = ensure_canopy_dir()
        assert result == canopy
        assert canopy.is_dir()

    def test_migrates_legacy_to_canopy(self, tmp_path, monkeypatch):
        canopy = tmp_path / ".claude" / "canopy"
        legacy = tmp_path / ".claude" / "orchestrator"
        legacy.mkdir(parents=True)
        (legacy / "repo-map.json").write_text('{"test": true}\n')
        monkeypatch.setattr("orchestrator.paths.CANOPY_DIR", canopy)
        monkeypatch.setattr("orchestrator.paths._LEGACY_DIR", legacy)
        result = ensure_canopy_dir()
        assert result == canopy
        assert canopy.is_dir()
        assert not legacy.exists()
        assert (canopy / "repo-map.json").read_text() == '{"test": true}\n'

    def test_uses_canopy_when_both_exist(self, tmp_path, monkeypatch):
        canopy = tmp_path / ".claude" / "canopy"
        legacy = tmp_path / ".claude" / "orchestrator"
        canopy.mkdir(parents=True)
        legacy.mkdir(parents=True)
        (canopy / "marker.txt").write_text("canopy")
        (legacy / "marker.txt").write_text("legacy")
        monkeypatch.setattr("orchestrator.paths.CANOPY_DIR", canopy)
        monkeypatch.setattr("orchestrator.paths._LEGACY_DIR", legacy)
        result = ensure_canopy_dir()
        assert result == canopy
        assert (canopy / "marker.txt").read_text() == "canopy"
        assert legacy.exists()

    def test_returns_existing_canopy_dir(self, tmp_path, monkeypatch):
        canopy = tmp_path / ".claude" / "canopy"
        legacy = tmp_path / ".claude" / "orchestrator"
        canopy.mkdir(parents=True)
        monkeypatch.setattr("orchestrator.paths.CANOPY_DIR", canopy)
        monkeypatch.setattr("orchestrator.paths._LEGACY_DIR", legacy)
        result = ensure_canopy_dir()
        assert result == canopy

    def test_creates_nested_parents(self, tmp_path, monkeypatch):
        canopy = tmp_path / "deep" / ".claude" / "canopy"
        legacy = tmp_path / "deep" / ".claude" / "orchestrator"
        monkeypatch.setattr("orchestrator.paths.CANOPY_DIR", canopy)
        monkeypatch.setattr("orchestrator.paths._LEGACY_DIR", legacy)
        ensure_canopy_dir()
        assert canopy.is_dir()
