"""Per-repo DDD loop configuration — ``<repo>/.canopy/ddd/config.yaml``.

The same file already carries ``workspace:`` (see :mod:`scripts.ddd.auth`). This
module reads the two blocks the v1/backlog loop adds::

    loop:
      mode: auto              # auto | backlog | polish
      backlog_min_findings: 8 # auto -> backlog when a FULL pass has >= this many open findings
      full_rejudge_every: 3   # backlog: every Nth fix batch is judged in full

    deploy_gate:
      health_url: https://labs.connect.dimagi.com/health/
      sha_field: git_sha      # dotted path into the JSON body
      samples: 5              # every sample must report the expected SHA
      interval_seconds: 5     # between samples
      attempts: 12            # rounds of sampling before reporting not_ready
      retry_seconds: 30       # between rounds

Every key is optional. A missing file, a missing block, or a malformed value
falls back to the defaults below — a config problem must never stop a run, it
only turns the optional behaviour off (the deploy gate reports ``skipped``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MODES = ("auto", "backlog", "polish")


@dataclass(frozen=True)
class LoopConfig:
    mode: str = "auto"
    backlog_min_findings: int = 8
    full_rejudge_every: int = 3


@dataclass(frozen=True)
class DeployGateConfig:
    health_url: str | None = None
    sha_field: str = "git_sha"
    samples: int = 5
    interval_seconds: float = 5.0
    attempts: int = 12
    retry_seconds: float = 30.0

    @property
    def enabled(self) -> bool:
        return bool(self.health_url)


@dataclass(frozen=True)
class DDDConfig:
    loop: LoopConfig = field(default_factory=LoopConfig)
    deploy_gate: DeployGateConfig = field(default_factory=DeployGateConfig)


def _int(raw: Any, default: int, *, minimum: int = 1) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val >= minimum else default


def _float(raw: Any, default: float) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val >= 0 else default


def parse(data: dict | None) -> DDDConfig:
    """Build a config from an already-parsed ``config.yaml`` mapping."""
    data = data if isinstance(data, dict) else {}
    loop_raw = data.get("loop") if isinstance(data.get("loop"), dict) else {}
    mode = str(loop_raw.get("mode") or "auto").strip().lower()
    loop = LoopConfig(
        mode=mode if mode in MODES else "auto",
        backlog_min_findings=_int(loop_raw.get("backlog_min_findings"), 8),
        full_rejudge_every=_int(loop_raw.get("full_rejudge_every"), 3),
    )
    gate_raw = data.get("deploy_gate") if isinstance(data.get("deploy_gate"), dict) else {}
    url = str(gate_raw.get("health_url") or "").strip() or None
    gate = DeployGateConfig(
        health_url=url,
        sha_field=str(gate_raw.get("sha_field") or "git_sha").strip() or "git_sha",
        samples=_int(gate_raw.get("samples"), 5),
        interval_seconds=_float(gate_raw.get("interval_seconds"), 5.0),
        attempts=_int(gate_raw.get("attempts"), 12),
        retry_seconds=_float(gate_raw.get("retry_seconds"), 30.0),
    )
    return DDDConfig(loop=loop, deploy_gate=gate)


def load(ddd_dir: str | Path | None = None) -> DDDConfig:
    """Load ``<ddd_dir>/config.yaml`` (resolved like every other DDD state)."""
    try:
        if ddd_dir is None:
            from scripts.ddd.runstate import _resolve_ddd_dir

            ddd_dir = _resolve_ddd_dir()
        cfg = Path(ddd_dir) / "config.yaml"
        if not cfg.exists():
            return DDDConfig()
        return parse(yaml.safe_load(cfg.read_text()) or {})
    except Exception:
        return DDDConfig()
