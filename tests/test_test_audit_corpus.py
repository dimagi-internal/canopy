"""Tests for orchestrator.test_audit.corpus and audit.collect_corpus."""
import shutil
from pathlib import Path

import yaml

from orchestrator.test_audit import build_corpus, write_corpus, collect_corpus

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_suite"


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copytree(FIXTURE, repo / "tests")
    return repo


def test_build_corpus_includes_every_test(tmp_path):
    repo = _make_repo(tmp_path)
    corpus = build_corpus(repo, run_tests=False)
    names = sorted(t["name"] for t in corpus["tests"])
    assert names == sorted([
        "test_add_returns_sum", "test_add_with_negatives",
        "test_always_passes", "test_no_assertion", "test_env_fragile",
        "test_subtraction_works", "test_add_with_mock_of_cut",
    ])
    assert corpus["test_count"] == len(corpus["tests"])
    assert corpus["ran_tests"] is False
    assert "ran_pytest" not in corpus  # the legacy field is not emitted


def test_build_corpus_attaches_source_and_static(tmp_path):
    repo = _make_repo(tmp_path)
    corpus = build_corpus(repo, run_tests=False)
    by_name = {t["name"]: t for t in corpus["tests"]}

    tautology = by_name["test_always_passes"]
    assert "assert True" in tautology["source"]
    assert tautology["static"]["assertion_count"] == 1
    assert tautology["static"]["has_real_assertion"] is False

    real = by_name["test_add_returns_sum"]
    assert real["static"]["has_real_assertion"] is True
    assert "add" in real["static"]["source_funcs_referenced"]


def test_build_corpus_runtime_is_none_when_not_run(tmp_path):
    repo = _make_repo(tmp_path)
    corpus = build_corpus(repo, run_tests=False)
    assert all(t["runtime"] is None for t in corpus["tests"])


def test_write_corpus_emits_valid_yaml(tmp_path):
    repo = _make_repo(tmp_path)
    out = tmp_path / "out"
    path, corpus = write_corpus(repo, out, run_tests=False)
    assert path.exists() and path.name == "corpus.yaml"
    reloaded = yaml.safe_load(path.read_text())
    assert reloaded["test_count"] == corpus["test_count"]
    assert {t["name"] for t in reloaded["tests"]} == {t["name"] for t in corpus["tests"]}


def test_collect_corpus_creates_stamped_dir(tmp_path):
    repo = _make_repo(tmp_path)
    result = collect_corpus(repo, run_tests=False)
    assert result.stamp_dir.exists()
    assert result.stamp_dir.parent.name == "test-audits"
    assert result.stamp_dir.parent.parent.name == ".canopy"
    assert result.corpus_path.exists()
    assert result.test_count == 7
    assert result.ran_tests is False


def test_corpus_includes_architecture_key(tmp_path):
    repo = _make_repo(tmp_path)
    corpus = build_corpus(repo, run_tests=False)
    assert "architecture" in corpus
    arch = corpus["architecture"]
    assert "modules" in arch
    assert "untested_modules" in arch
    assert "mock_density" in arch
    assert "overmocked_files" in arch
    assert "slow_tests" in arch
    # Synthetic suite has no src/ directory, so module inventory is empty.
    assert arch["modules"] == []
