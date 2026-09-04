"""`canopy eval …` — the eval runner: weighted-score a rubric and record the
verdict to a run step (consumes canopy-web's verdict endpoint via AgentClient).
The LLM that produces per-dimension scores is a separate seam; this wires the
score → verdict path so any agent self-grades against the run lifecycle."""
import json
import click

from orchestrator.agent_client import AgentClient, CanopyError
from orchestrator.eval_rubric import score_rubric


@click.group(name="eval")
def eval_group():
    """Score rubrics and record run-step verdicts (the eval runner)."""


@eval_group.command("score")
@click.option("--rubric-json", required=True, type=click.Path(exists=True),
              help='JSON list: [{"name","score","weight"?}, ...]')
@click.option("--pass-at", default=70.0, type=float)
@click.option("--warn-at", default=50.0, type=float)
def eval_score(rubric_json, pass_at, warn_at):
    """Weighted-score a rubric JSON (no network)."""
    try:
        dims = json.load(open(rubric_json, encoding="utf-8"))
        click.echo(json.dumps(score_rubric(dims, pass_at=pass_at, warn_at=warn_at)))
    except ValueError as e:
        raise click.ClickException(str(e))


@eval_group.command("record")
@click.option("--slug", required=True)
@click.option("--run-id", required=True)
@click.option("--step", required=True)
@click.option("--kind", type=click.Choice(["judge", "qa"]), default="judge")
@click.option("--rubric-json", type=click.Path(exists=True),
              help="judge: weighted-score these dimensions into the verdict")
@click.option("--score", type=float, default=None)
@click.option("--passed/--no-passed", default=None)
@click.option("--rationale", default="")
def eval_record(slug, run_id, step, kind, rubric_json, score, passed, rationale):
    """Record a verdict on a run step — judge from a rubric, or qa/explicit."""
    criteria: dict = {}
    try:
        if rubric_json:
            scored = score_rubric(json.load(open(rubric_json, encoding="utf-8")))
            kind, score = "judge", scored["overall_score"]
            criteria = {"dimensions": scored["dimensions"], "verdict": scored["verdict"]}
        out = AgentClient({"slug": slug}).record_verdict(
            run_id, step, kind=kind, score=score, passed=passed,
            criteria=criteria, rationale=rationale)
        click.echo(json.dumps(out))
    except (ValueError, CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
