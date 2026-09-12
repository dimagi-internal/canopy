# canopy-agent-factory

Stamp out a new Claude Code agent from the canopy operating model — a persona, the `turn`
orchestrator, reads-free / writes-gated guarding via a config-driven PreToolUse hook, and a
canopy-web-ready layout. Extracted from the `canopy` distribution's
`src/orchestrator/agent_factory.py` so a second consumer (canopy-web, a Django app) can
scaffold an agent repo without inheriting canopy's video-tooling dependency footprint
(Pillow, NumPy).

Zero runtime dependencies. Versions independently of `canopy`.

## Public API

```python
from canopy_agent_factory import AgentSpec, create_agent, normalize_slug, AgentFactoryError
from canopy_agent_factory import templates, gating_config  # canonical stamp-table baseline
```

- `AgentSpec` — dataclass describing the agent to scaffold.
- `create_agent(spec, target_dir, *, force=False) -> list[Path]` — writes the templated
  files into `target_dir` (caller-supplied; this function does no `git init` and no other
  I/O beyond writing the scaffold) and returns the paths it wrote.
- `normalize_slug(raw) -> str` — validate + normalize an agent slug.
- `AgentFactoryError` — raised for bad input or an unsafe target directory.
- `templates() -> Mapping[str, str]` — the canonical stamp table (repo-relative path →
  rendered-template source), read-only. Ground truth for "what a factory-stamped agent
  looks like"; used by canopy's fleet-alignment lens to diff real agent repos against the
  baseline.
- `gating_config() -> str` — the canonical `config/gating.json` body a stamped agent ships
  with.

## Development

```bash
uv run --with pytest pytest -q
```

Runs standalone — no orchestrator import, no Django.
