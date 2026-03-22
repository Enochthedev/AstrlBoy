"""
Job application endpoints — view applications.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from db.base import async_session_factory
from db.models.job_applications import JobApplication

router = APIRouter(prefix="/applications", tags=["applications"])


@router.get("")
async def list_applications(limit: int = 20) -> list[dict]:
    """List all job applications."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(JobApplication).order_by(JobApplication.sent_at.desc()).limit(limit)
        )
        apps = result.scalars().all()

    return [
        {
            "id": str(a.id),
            "role": a.role,
            "company": a.company,
            "status": a.status,
            "sent_at": a.sent_at.isoformat() if a.sent_at else None,
        }
        for a in apps
    ]


@router.get("/{application_id}")
async def get_application(application_id: UUID) -> dict:
    """Get a specific application."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(JobApplication).where(JobApplication.id == application_id)
        )
        app = result.scalar_one_or_none()

    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    return {
        "id": str(app.id),
        "role": app.role,
        "company": app.company,
        "posting_url": app.posting_url,
        "email_sent_to": app.email_sent_to,
        "cover_note": app.cover_note,
        "status": app.status,
        "sent_at": app.sent_at.isoformat() if app.sent_at else None,
        "last_updated": app.last_updated.isoformat() if app.last_updated else None,
    }
