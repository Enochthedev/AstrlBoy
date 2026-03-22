"""
Node functions for the growth experiments graph.

Generates experiment ideas, designs hypotheses, tracks execution,
measures results, and documents learnings.
"""

from uuid import uuid4

from anthropic import AsyncAnthropic

from core.config import settings
from core.logging import get_logger
from db.base import async_session_factory
from db.models.experiments import Experiment
from graphs.experiments.state import ExperimentState
from storage.r2 import r2_client

logger = get_logger("graphs.experiments.nodes")

_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)


async def generate_ideas(state: ExperimentState) -> ExperimentState:
    """Generate growth experiment ideas based on client context."""
    meta = state["contract_meta"]

    response = await _anthropic.messages.create(
        model="claude-sonnet-4-5-20250514",
        max_tokens=1000,
        system=(
            f"You are designing growth experiments for {meta.get('description', 'a client')}.\n"
            "Generate 3 experiment ideas. Each must have:\n"
            "- A clear hypothesis\n"
            "- A measurable outcome\n"
            "- An execution plan that can be run autonomously\n\n"
            "Format: IDEA|HYPOTHESIS|PLAN per line."
        ),
        messages=[{"role": "user", "content": f"Platforms: {meta.get('platforms', [])}"}],
    )

    ideas: list[dict] = []
    for line in response.content[0].text.strip().split("\n"):
        parts = line.split("|")
        if len(parts) >= 3:
            ideas.append({
                "title": parts[0].strip(),
                "hypothesis": parts[1].strip(),
                "plan": parts[2].strip(),
            })

    return {**state, "experiment_ideas": ideas}


async def select_experiment(state: ExperimentState) -> ExperimentState:
    """Select the best experiment idea to run."""
    ideas = state.get("experiment_ideas", [])
    if not ideas:
        return {**state, "status": "no_ideas", "error": "No experiment ideas generated"}

    # Take the first viable idea
    selected = ideas[0]
    return {
        **state,
        "selected_experiment": selected,
        "hypothesis": selected["hypothesis"],
        "execution_plan": selected["plan"],
    }


async def save_experiment(state: ExperimentState) -> ExperimentState:
    """Persist the experiment to the DB and R2."""
    selected = state.get("selected_experiment", {})
    experiment_id = uuid4()

    async with async_session_factory() as session:
        experiment = Experiment(
            id=experiment_id,
            contract_id=state["contract_id"],
            title=selected.get("title", ""),
            hypothesis=state.get("hypothesis", ""),
            execution=state.get("execution_plan", ""),
            status="running",
        )
        session.add(experiment)
        await session.commit()

    try:
        await r2_client.dump(
            contract_slug=state["contract_slug"],
            entity_type="experiments",
            entity_id=experiment_id,
            data={
                "title": selected.get("title", ""),
                "hypothesis": state.get("hypothesis", ""),
                "execution_plan": state.get("execution_plan", ""),
                "model": "claude-sonnet-4-5-20250514",
            },
        )
    except Exception:
        pass

    return {**state, "experiment_id": experiment_id, "status": "running"}
