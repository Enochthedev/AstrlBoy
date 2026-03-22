"""
Pydantic schemas for the contracts system.

Defines the API request/response shapes and the structure
of the meta JSONB config that holds all client-specific settings.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ContractMeta(BaseModel):
    """Client-specific configuration stored in the contract's meta JSONB column."""

    description: str = ""
    website: str = ""
    tone: str = ""
    content_types: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    subreddits: list[str] = Field(default_factory=list)
    discord_servers: list[str] = Field(default_factory=list)
    stream_keywords: list[str] = Field(default_factory=list)
    briefing_recipients: list[str] = Field(default_factory=list)
    feature_request_endpoint: str = ""
    platforms: list[str] = Field(default_factory=list)
    active_skills: list[str] = Field(default_factory=list)


class ContractCreate(BaseModel):
    """Request body for creating a new contract."""

    client_name: str
    client_slug: str
    client_db_url: str
    meta: ContractMeta = Field(default_factory=ContractMeta)
    ends_at: datetime | None = None


class ContractResponse(BaseModel):
    """Response body for a contract."""

    model_config = {"from_attributes": True}

    id: UUID
    client_name: str
    client_slug: str
    status: str
    meta: dict
    started_at: datetime
    ends_at: datetime | None
    created_at: datetime
