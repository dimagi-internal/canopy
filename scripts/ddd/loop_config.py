"""Per-repo DDD loop configuration — ``<repo>/.canopy/ddd/config.yaml``.

The same file already carries ``workspace:`` (see :mod:`scripts.ddd.auth`). This
module reads the two blocks the v1/backlog loop adds::

    loop:
      mode: auto              # auto | backlog | polish
      backlog_min_findings: 8 # auto -> backlog when a FULL pass has >= this many open findings
      full_rejudge_every: 3   # backlog: every Nth fix batch is judged in full (= a checkpoint)
      judge_tiering: auto     # auto (on in backlog mode) | on | off — between checkpoints
                              # only the concept judge re-runs, on changed scenes

    deploy_gate:
      health_url: https://labs.connect.dimagi.com/health/
      sha_field: git_sha      # dotted path into the JSON body
      samples: 5              # every sample must report the expected SHA
      interval_seconds: 5     # between samples
      attempts: 12            # rounds of sampling before reporting not_ready
      retry_seconds: 30       # between rounds

    inner_loop:               # optional, default OFF — see scripts.ddd.target
      base_url: http://localhost:8000          # a locally served build of the fix branch
      setup: make serve-demo                   # (re)start it; run under the watchdog
      health_url: http://localhost:8000/health/  # optional readiness probe
      ready_timeout_seconds: 120

    timeouts:                 # sub-step watchdog (scripts.ddd.watchdog)
      default_minutes: 45     # any step without its own key
      heartbeat_minutes: 15   # no heartbeat for this long -> timed_out
      fixer_minutes: 45       # <step>_minutes overrides default_minutes per step
      render_minutes: 20

    auth_preflight:           # run before every iteration (scripts.ddd.preflight)
      timeout_seconds: 20     # per command
      commands:               # a string, or {name, run}
        - name: aws-labs
          run: aws sts get-caller-identity --profile labs
        - gh auth status

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
TIERING = ("auto", "on", "off")


@dataclass(frozen=True)
class LoopConfig:
    mode: str = "auto"
    backlog_min_findings: int = 8
    full_rejudge_every: int = 3
    judge_tiering: str = "auto"

    def tiered(self, loop_mode: str | None) -> bool:
        """Concept-only judging between checkpoints? ``auto`` = on in backlog mode."""
        if self.judge_tiering == "on":
            return True
        if self.judge_tiering == "off":
            return False
        return loop_mode == "backlog"


@dataclass(frozen=True)
class InnerLoopConfig:
    base_url: str | None = None
    setup: str | None = None
    health_url: str | None = None
    ready_timeout_seconds: float = 120.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)


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
class TimeoutsConfig:
    default_minutes: float = 45.0
    heartbeat_minutes: float = 15.0
    per_step: dict[str, float] = field(default_factory=dict)

    def for_step(self, step: str) -> float:
        """Total budget (minutes) for ``step`` — ``<step>_minutes`` or the default."""
        base = str(step).split(":", 1)[0]
        return self.per_step.get(step, self.per_step.get(base, self.default_minutes))


@dataclass(frozen=True)
class AuthCommand:
    name: str
    run: str


@dataclass(frozen=True)
class AuthPreflightConfig:
    commands: tuple[AuthCommand, ...] = ()
    timeout_seconds: float = 20.0

    @property
    def enabled(self) -> bool:
        return bool(self.commands)


@dataclass(frozen=True)
class DDDConfig:
    loop: LoopConfig = field(default_factory=LoopConfig)
    deploy_gate: DeployGateConfig = field(default_factory=DeployGateConfig)
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)
    inner_loop: InnerLoopConfig = field(default_factory=InnerLoopConfig)
    auth_preflight: AuthPreflightConfig = field(default_factory=AuthPreflightConfig)


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


def _positive(raw: Any, default: float) -> float:
    val = _float(raw, default)
    return val if val > 0 else default


def _parse_timeouts(raw: Any) -> TimeoutsConfig:
    raw = raw if isinstance(raw, dict) else {}
    per_step: dict[str, float] = {}
    for key, val in raw.items():
        key = str(key)
        if key in ("default_minutes", "heartbeat_minutes") or not key.endswith("_minutes"):
            continue
        minutes = _positive(val, 0.0)
        if minutes > 0:
            per_step[key[: -len("_minutes")]] = minutes
    return TimeoutsConfig(
        default_minutes=_positive(raw.get("default_minutes"), 45.0),
        heartbeat_minutes=_positive(raw.get("heartbeat_minutes"), 15.0),
        per_step=per_step,
    )


def _tiering(raw: Any) -> str:
    if raw is True:
        return "on"
    if raw is False:
        return "off"
    val = str(raw or "auto").strip().lower()
    return val if val in TIERING else "auto"


def _parse_inner(raw: Any) -> InnerLoopConfig:
    raw = raw if isinstance(raw, dict) else {}
    base = str(raw.get("base_url") or "").strip().rstrip("/") or None
    return InnerLoopConfig(
        base_url=base,
        setup=str(raw.get("setup") or "").strip() or None,
        health_url=str(raw.get("health_url") or "").strip() or None,
        ready_timeout_seconds=_positive(raw.get("ready_timeout_seconds"), 120.0),
    )


def _command_name(run: str) -> str:
    return " ".join(run.split()[:3]) or run


def _parse_auth(raw: Any) -> AuthPreflightConfig:
    raw = raw if isinstance(raw, dict) else {}
    cmds: list[AuthCommand] = []
    for item in raw.get("commands") or []:
        if isinstance(item, str) and item.strip():
            cmds.append(AuthCommand(name=_command_name(item.strip()), run=item.strip()))
        elif isinstance(item, dict) and str(item.get("run") or "").strip():
            run = str(item["run"]).strip()
            cmds.append(AuthCommand(name=str(item.get("name") or _command_name(run)).strip(), run=run))
    return AuthPreflightConfig(
        commands=tuple(cmds),
        timeout_seconds=_positive(raw.get("timeout_seconds"), 20.0),
    )


def parse(data: dict | None) -> DDDConfig:
    """Build a config from an already-parsed ``config.yaml`` mapping."""
    data = data if isinstance(data, dict) else {}
    loop_raw = data.get("loop") if isinstance(data.get("loop"), dict) else {}
    mode = str(loop_raw.get("mode") or "auto").strip().lower()
    loop = LoopConfig(
        mode=mode if mode in MODES else "auto",
        backlog_min_findings=_int(loop_raw.get("backlog_min_findings"), 8),
        full_rejudge_every=_int(loop_raw.get("full_rejudge_every"), 3),
        judge_tiering=_tiering(loop_raw.get("judge_tiering")),
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
    return DDDConfig(
        loop=loop,
        deploy_gate=gate,
        timeouts=_parse_timeouts(data.get("timeouts")),
        inner_loop=_parse_inner(data.get("inner_loop")),
        auth_preflight=_parse_auth(data.get("auth_preflight")),
    )


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
