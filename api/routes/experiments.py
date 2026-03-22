"""
Experiment endpoints — view growth experiments.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from db.base import async_session_factory
from db.models.experiments import Experiment

router = APIRouter(prefix="/experiments", tags=["experiments"])


@router.get("")
async def list_experiments(limit: int = 20) -> list[dict]:
    """List all experiments."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Experiment).order_by(Experiment.started_at.desc()).limit(limit)
        )
        experiments = result.scalars().all()

    return [
        {
            "id": str(e.id),
            "contract_id": str(e.contract_id),
            "title": e.title,
            "status": e.status,
            "started_at": e.started_at.isoformat() if e.started_at else None,
        }
        for e in experiments
    ]


@router.get("/{experiment_id}")
async def get_experiment(experiment_id: UUID) -> dict:
    """Get a specific experiment."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Experiment).where(Experiment.id == experiment_id)
        )
        experiment = result.scalar_one_or_none()

    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")

    return {
        "id": str(experiment.id),
        "contract_id": str(experiment.contract_id),
        "title": experiment.title,
        "hypothesis": experiment.hypothesis,
        "execution": experiment.execution,
        "result": experiment.result,
        "learning": experiment.learning,
        "status": experiment.status,
        "started_at": experiment.started_at.isoformat() if experiment.started_at else None,
        "completed_at": experiment.completed_at.isoformat() if experiment.completed_at else None,
    }
