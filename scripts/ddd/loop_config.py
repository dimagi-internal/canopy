"""Per-repo DDD loop configuration — ``<repo>/.canopy/ddd/config.yaml``.

The same file already carries ``workspace:`` (see :mod:`scripts.ddd.auth`). This
module reads the two blocks the v1/backlog loop adds::

    loop:
      objective: demo         # demo (default) | product | auto — WHAT the loop optimizes
                              # (scripts.ddd.objective). product: the demo is a
                              # probe and the product getting better is the goal;
                              # demo: the walkthrough itself is the deliverable.
                              # auto -> product when the first FULL pass has
                              # >= backlog_min_findings open findings, else demo;
                              # chosen once per run and then sticky. Default demo:
                              # ACE's /ace:demo runs ship the video.
      mode: auto              # auto | backlog | polish — HOW it judges (economics)
      backlog_min_findings: 8 # auto -> backlog when a FULL pass has >= this many open findings
      full_rejudge_every: 3   # backlog: every Nth fix batch is judged in full (= a checkpoint)
      judge_tiering: auto     # auto (on in backlog mode) | on | off — between checkpoints
                              # only the concept judge re-runs, on changed scenes
      inner_loop_hint_minutes: 15  # no inner_loop + a batch waited longer than this on
                              # CI + deploy -> judge_gate prints a one-line
                              # recommendation to configure one (0 = never)
      floor_first: true       # between checkpoints, the judge holding the gating
                              # floor re-judges its capping scene(s) every pass, and
                              # the next batch fixes the floor's findings first
                              # (scripts.ddd.floor, canopy#780)
      narrative_mode: auto    # auto | build | polish — how the narrative is reviewed
                              # (scripts.ddd.narrative_guard, canopy#789). build: a
                              # living spec the loop may evolve, every revision
                              # guarded autonomously, nothing waits on a human;
                              # polish: the first version blocks on the human gate.
                              # auto -> polish for the demo objective, else build.
      fixed_surfaces:         # regexes naming what this loop may NOT edit; a floor
        - registry            # finding whose fix names one is out of edit scope,
        - seed data           # and a floor made only of those stops the run
                              # (stop_out_of_scope) instead of iterating to a stall

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

    # ...or, deliberately without one (the reason is recorded in run_state and
    # printed by every assemble):
    inner_loop: off
    inner_loop_off_reason: the product has no locally servable build yet
    # (equivalently: inner_loop: {off: true, reason: "..."})

    timeouts:                # sub-step watchdog (scripts.ddd.watchdog)
      default_minutes: 45     # any step without its own key
      heartbeat_minutes: 15   # no heartbeat for this long -> timed_out
      fixer_minutes: 45       # <step>_minutes overrides default_minutes per step
      render_minutes: 20

    product:                  # the product objective (scripts.ddd.objective) — all optional
      floor: 3                # every PRODUCT dimension's weakest cell must reach this
      presentation_floor: 2   # a presentation cell below this still blocks (broken, not unpolished)
      block_severities: [high, medium]  # product findings at these severities block
      polish_pass: true       # one batch of the deferred polish findings before stop_done
      lint:                   # scripts.ddd.product_lint — deterministic, every render
        max_words_per_screen: 800
        max_long_lines_per_screen: 3   # a "long line" = >= long_line_words words of UI text
        long_line_words: 20
        max_new_long_lines: 0          # explanatory lines a run may ADD over its baseline
        glossary: {round: tender}      # banned term -> the product's word for it
        fonts: [Inter]                 # allowed first font family of rendered text

    auth_preflight:           # run before every iteration (scripts.ddd.preflight)
      timeout_seconds: 20     # per command
      commands:               # a string, or {name, run}
        - name: aws-labs
          run: aws sts get-caller-identity --profile labs
        - gh auth status

Every key is optional. A missing file, a missing block, or a malformed value
falls back to the defaults below — a config problem must never stop a run, it
only turns the optional behaviour off (the deploy gate reports ``skipped``).

One deliberate exception (0.2.554, :func:`scripts.ddd.target.inner_loop_policy`):
a repo whose ``deploy_gate`` is configured — every fix batch merges, waits on CI
and deploys before a frame can be judged — must also say how it renders BETWEEN
checkpoints. Without ``inner_loop:`` (or ``inner_loop: off`` plus a reason)
``assemble`` refuses to continue a backlog (v1-product) loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MODES = ("auto", "backlog", "polish")
OBJECTIVES = ("auto", "product", "demo")
TIERING = ("auto", "on", "off")
NARRATIVE_MODES = ("auto", "build", "polish")


@dataclass(frozen=True)
class LoopConfig:
    objective: str = "demo"
    mode: str = "auto"
    backlog_min_findings: int = 8
    full_rejudge_every: int = 3
    judge_tiering: str = "auto"
    inner_loop_hint_minutes: float = 15.0
    floor_first: bool = True
    fixed_surfaces: tuple[str, ...] = ()
    narrative_mode: str = "auto"

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
    # ``inner_loop: off`` — declared deliberately, rather than just missing.
    off: bool = False
    off_reason: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url) and not self.off


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
class LintConfig:
    max_words_per_screen: int = 800
    max_long_lines_per_screen: int = 3
    long_line_words: int = 20
    max_new_long_lines: int = 0
    glossary: dict[str, str] = field(default_factory=dict)
    fonts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductConfig:
    floor: float = 3.0
    presentation_floor: float = 2.0
    block_severities: tuple[str, ...] = ("high", "medium")
    polish_pass: bool = True
    lint: LintConfig = field(default_factory=LintConfig)


@dataclass(frozen=True)
class DDDConfig:
    loop: LoopConfig = field(default_factory=LoopConfig)
    deploy_gate: DeployGateConfig = field(default_factory=DeployGateConfig)
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)
    inner_loop: InnerLoopConfig = field(default_factory=InnerLoopConfig)
    auth_preflight: AuthPreflightConfig = field(default_factory=AuthPreflightConfig)
    product: ProductConfig = field(default_factory=ProductConfig)


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


def _patterns(raw: Any) -> tuple[str, ...]:
    """A list of regexes; an invalid one is dropped (a config typo must not stop a run)."""
    import re

    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for item in raw or []:
        pat = str(item or "").strip()
        if not pat:
            continue
        try:
            re.compile(pat)
        except re.error:
            continue
        out.append(pat)
    return tuple(out)


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


_OFF_WORDS = ("off", "false", "no", "disabled", "none")


def _parse_inner(raw: Any, off_reason: Any = None) -> InnerLoopConfig:
    """``inner_loop:`` — a mapping, or ``off`` (YAML reads a bare ``off`` as False).

    ``off`` takes its reason from ``inner_loop_off_reason:``; the mapping form
    ``{off: true, reason: ...}`` carries its own.
    """
    reason = str(off_reason or "").strip() or None
    if raw is False or (isinstance(raw, str) and raw.strip().lower() in _OFF_WORDS):
        return InnerLoopConfig(off=True, off_reason=reason)
    raw = raw if isinstance(raw, dict) else {}
    if raw.get("off") is True or str(raw.get("off") or "").strip().lower() in ("true", "yes", "on"):
        return InnerLoopConfig(off=True, off_reason=str(raw.get("reason") or "").strip() or reason)
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


def _parse_product(raw: Any) -> ProductConfig:
    raw = raw if isinstance(raw, dict) else {}
    lint_raw = raw.get("lint") if isinstance(raw.get("lint"), dict) else {}
    glossary_raw = lint_raw.get("glossary")
    glossary = (
        {str(k).strip().lower(): str(v).strip() for k, v in glossary_raw.items() if str(k).strip()}
        if isinstance(glossary_raw, dict)
        else {}
    )
    fonts_raw = lint_raw.get("fonts")
    if isinstance(fonts_raw, str):
        fonts_raw = [fonts_raw]
    fonts = tuple(str(f).strip() for f in fonts_raw or [] if str(f).strip())
    sev_raw = raw.get("block_severities")
    sev = (
        tuple(str(s).strip().lower() for s in sev_raw if str(s).strip())
        if isinstance(sev_raw, list)
        else ("high", "medium")
    )
    lint = LintConfig(
        max_words_per_screen=_int(lint_raw.get("max_words_per_screen"), 800),
        max_long_lines_per_screen=_int(lint_raw.get("max_long_lines_per_screen"), 3, minimum=0),
        long_line_words=_int(lint_raw.get("long_line_words"), 20),
        max_new_long_lines=_int(lint_raw.get("max_new_long_lines"), 0, minimum=0),
        glossary=glossary,
        fonts=fonts,
    )
    return ProductConfig(
        floor=_positive(raw.get("floor"), 3.0),
        presentation_floor=_float(raw.get("presentation_floor"), 2.0),
        block_severities=sev,
        polish_pass=raw.get("polish_pass") is not False,
        lint=lint,
    )


def parse(data: dict | None) -> DDDConfig:
    """Build a config from an already-parsed ``config.yaml`` mapping."""
    data = data if isinstance(data, dict) else {}
    loop_raw = data.get("loop") if isinstance(data.get("loop"), dict) else {}
    mode = str(loop_raw.get("mode") or "auto").strip().lower()
    objective = str(loop_raw.get("objective") or "demo").strip().lower()
    narrative_mode = str(loop_raw.get("narrative_mode") or "auto").strip().lower()
    loop = LoopConfig(
        objective=objective if objective in OBJECTIVES else "demo",
        mode=mode if mode in MODES else "auto",
        backlog_min_findings=_int(loop_raw.get("backlog_min_findings"), 8),
        full_rejudge_every=_int(loop_raw.get("full_rejudge_every"), 3),
        judge_tiering=_tiering(loop_raw.get("judge_tiering")),
        inner_loop_hint_minutes=max(_float(loop_raw.get("inner_loop_hint_minutes"), 15.0), 0.0),
        floor_first=loop_raw.get("floor_first") is not False,
        fixed_surfaces=_patterns(loop_raw.get("fixed_surfaces")),
        narrative_mode=narrative_mode if narrative_mode in NARRATIVE_MODES else "auto",
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
        inner_loop=_parse_inner(data.get("inner_loop"), data.get("inner_loop_off_reason")),
        auth_preflight=_parse_auth(data.get("auth_preflight")),
        product=_parse_product(data.get("product")),
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
