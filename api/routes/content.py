"""
Content endpoints — view and trigger content generation.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from contracts.service import contracts_service
from core.exceptions import ContractNotFound
from db.base import async_session_factory
from db.models.content import Content
from graphs.content.graph import content_graph

router = APIRouter(prefix="/content", tags=["content"])


@router.get("")
async def list_content(contract_slug: str | None = None, limit: int = 20) -> list[dict]:
    """List content pieces, optionally filtered by contract."""
    async with async_session_factory() as session:
        query = select(Content).order_by(Content.created_at.desc()).limit(limit)
        if contract_slug:
            contract = await contracts_service.get_contract(contract_slug)
            query = query.where(Content.contract_id == contract.id)
        result = await session.execute(query)
        pieces = result.scalars().all()

    return [
        {
            "id": str(c.id),
            "contract_id": str(c.contract_id),
            "type": c.type,
            "title": c.title,
            "status": c.status,
            "revision_count": c.revision_count,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in pieces
    ]


@router.post("/trigger")
async def trigger_content(contract_slug: str, content_type: str = "post") -> dict:
    """Manually trigger content generation for a contract."""
    try:
        contract = await contracts_service.get_contract(contract_slug)
    except ContractNotFound:
        raise HTTPException(status_code=404, detail=f"Contract '{contract_slug}' not found")

    result = await content_graph.run(contract, content_type=content_type)
    return {"status": result.get("status", "unknown"), "content_id": str(result.get("content_id", ""))}


@router.get("/{content_id}")
async def get_content(content_id: UUID) -> dict:
    """Get a specific content piece."""
    async with async_session_factory() as session:
        result = await session.execute(select(Content).where(Content.id == content_id))
        content = result.scalar_one_or_none()

    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    return {
        "id": str(content.id),
        "contract_id": str(content.contract_id),
        "type": content.type,
        "title": content.title,
        "body": content.body,
        "critique_notes": content.critique_notes,
        "revision_count": content.revision_count,
        "status": content.status,
        "platform": content.platform,
        "created_at": content.created_at.isoformat() if content.created_at else None,
        "published_at": content.published_at.isoformat() if content.published_at else None,
    }
