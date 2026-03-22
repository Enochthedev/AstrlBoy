"""
Health and status endpoints.
"""

from fastapi import APIRouter

from agent.service import agent_service
from contracts.service import contracts_service
from core.config import settings

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """Liveness check for Railway health monitoring."""
    return {"status": "ok", "agent": settings.agent_name, "paused": settings.agent_paused}


@router.get("/status")
async def status() -> dict:
    """Agent status including active contracts and pending queue count."""
    contracts = await contracts_service.get_active_contracts()
    pending = await agent_service.get_pending_escalations()

    return {
        "agent": settings.agent_name,
        "paused": settings.agent_paused,
        "active_contracts": len(contracts),
        "contract_slugs": [c.client_slug for c in contracts],
        "pending_escalations": len(pending),
    }
