"""
Enums and constant values used across the application.

Centralizes all status strings, platform names, and content types
so nothing is hardcoded as a raw string elsewhere.
"""

from enum import StrEnum


class ContractStatus(StrEnum):
    """Lifecycle states for a client contract."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class ContentStatus(StrEnum):
    """Lifecycle states for a content piece."""

    DRAFT = "draft"
    APPROVED = "approved"
    PUBLISHED = "published"
    REJECTED = "rejected"


class ContentType(StrEnum):
    """Types of content astrlboy can produce."""

    SPOTLIGHT = "spotlight"
    GUIDE = "guide"
    TREND = "trend"
    POST = "post"


class InteractionStatus(StrEnum):
    """Lifecycle states for a community interaction."""

    PENDING = "pending"
    APPROVED = "approved"
    POSTED = "posted"
    REJECTED = "rejected"


class Platform(StrEnum):
    """Supported social platforms."""

    X = "x"
    LINKEDIN = "linkedin"
    REDDIT = "reddit"
    DISCORD = "discord"


class TrendSource(StrEnum):
    """Sources of trend signals."""

    X_STREAM = "x_stream"
    REDDIT = "reddit"
    TAVILY = "tavily"
    FIRECRAWL = "firecrawl"


class ExperimentStatus(StrEnum):
    """Lifecycle states for a growth experiment."""

    RUNNING = "running"
    COMPLETE = "complete"
    ABANDONED = "abandoned"


class FeatureRequestPriority(StrEnum):
    """Priority levels for feature requests."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApplicationStatus(StrEnum):
    """Lifecycle states for a job application."""

    SENT = "sent"
    REPLIED = "replied"
    INTERVIEWING = "interviewing"
    REJECTED = "rejected"
    CLOSED = "closed"
