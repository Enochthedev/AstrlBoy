"""
FastAPI application entry point for astrlboy.

Starts the async web server, initializes all subsystems on startup,
and tears them down gracefully on shutdown.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

from api.router import api_router
from approval.telegram import create_telegram_app
from cache.redis import close_redis
from contracts.service import contracts_service
from core.config import settings
from core.logging import get_logger, setup_logging
from db.base import close_engine
from db.client_db import client_db_manager
from scheduler.jobs import create_scheduler
from skills.builtin.crawl import CrawlSkill
from skills.builtin.draft_approval import DraftApprovalSkill
from skills.builtin.post_linkedin import PostLinkedInSkill
from skills.builtin.post_x import PostXSkill
from skills.builtin.read_email import ReadEmailSkill
from skills.builtin.scrape import ScrapeSkill
from skills.builtin.search import SearchSkill
from skills.builtin.send_email import SendEmailSkill
from skills.builtin.serp import SerpSkill
from skills.builtin.trend_stream import TrendStreamSkill
from skills.registry import skill_registry

logger = get_logger("main")

_scheduler = None
_telegram_app = None


async def _bootstrap_self_contract() -> None:
    """Create astrlboy's own contract if no contracts exist.

    This gives the agent an objective from day one — grow the @astrlboy__
    account, engage with trends, build an audience — even before any
    paying clients are onboarded.
    """
    from sqlalchemy import select

    from db.base import async_session_factory
    from db.models.contracts import Contract

    async with async_session_factory() as session:
        result = await session.execute(select(Contract).limit(1))
        if result.scalar_one_or_none() is not None:
            return  # contracts already exist

        self_contract = Contract(
            client_name="astrlboy",
            client_slug="astrlboy",
            status="active",
            client_db_url="",  # no separate DB — uses primary
            meta={
                "description": (
                    "astrlboy — an always-on AI personality building its own audience. "
                    "Primary mission: grow the @astrlboy__ account into a recognized voice. "
                    "Explore niches, post hot takes, engage with trending topics, reply to threads. "
                    "Track what gets engagement and double down on what resonates. "
                    "Niches to explore: AI agents, crypto/web3, tech culture, build-in-public, startup life."
                ),
                "website": "https://astrlboy.xyz",
                "tone": "sharp, opinionated, concise, human, slightly irreverent — never corporate, never generic",
                "content_types": ["post", "trend"],
                "competitors": [],
                "subreddits": [],
                "discord_servers": [],
                "stream_keywords": [
                    "AI agents",
                    "autonomous AI",
                    "crypto",
                    "web3",
                    "build in public",
                    "startup",
                    "tech Twitter",
                    "AI trends",
                    "Claude",
                    "agentic",
                ],
                "briefing_recipients": [],
                "feature_request_endpoint": "",
                "platforms": ["x"],
                "active_skills": [
                    "search",
                    "serp",
                    "post_x",
                    "trend_stream",
                ],
            },
        )
        session.add(self_contract)
        await session.commit()

    logger.info("self_contract_bootstrapped")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown lifecycle for the FastAPI application."""
    global _scheduler, _telegram_app
    setup_logging()
    logger.info("astrlboy starting", agent_name=settings.agent_name)

    # Register all built-in skills
    for skill_cls in [
        ScrapeSkill, CrawlSkill, SearchSkill, SerpSkill,
        PostXSkill, PostLinkedInSkill,
        SendEmailSkill, ReadEmailSkill,
        TrendStreamSkill, DraftApprovalSkill,
    ]:
        try:
            await skill_registry.register(skill_cls())
        except Exception as exc:
            # Skills may fail to init if API keys are missing — non-fatal
            logger.warning("skill_init_failed", skill=skill_cls.__name__, error=str(exc))

    # Create database tables if they don't exist
    try:
        from db.base import Base, engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("db_tables_ready")
    except Exception as exc:
        logger.warning("db_init_failed", error=str(exc))

    # Bootstrap astrlboy's own contract if none exist
    try:
        await _bootstrap_self_contract()
    except Exception as exc:
        logger.warning("bootstrap_failed", error=str(exc))

    # Load contract registry
    try:
        await contracts_service.load_registry()
    except Exception as exc:
        logger.warning("registry_load_failed", error=str(exc))

    # Start scheduler
    _scheduler = create_scheduler()
    _scheduler.start()
    logger.info("scheduler_started")

    # Start Telegram bot (polling mode, runs alongside FastAPI)
    try:
        _telegram_app = create_telegram_app()
        if _telegram_app:
            await _telegram_app.initialize()
            await _telegram_app.start()
            await _telegram_app.updater.start_polling(drop_pending_updates=True)
            logger.info("telegram_bot_started")
    except Exception as exc:
        logger.warning("telegram_bot_start_failed", error=str(exc))

    # Start X filtered stream (background)
    try:
        from streams.x_stream import start_stream
        await start_stream()
    except Exception as exc:
        logger.warning("x_stream_start_failed", error=str(exc))

    yield

    # Shutdown
    logger.info("astrlboy shutting down")
    if _telegram_app and _telegram_app.updater:
        await _telegram_app.updater.stop()
        await _telegram_app.stop()
        await _telegram_app.shutdown()
    if _scheduler:
        _scheduler.shutdown(wait=False)
    await client_db_manager.close_all()
    await close_engine()
    await close_redis()


app = FastAPI(
    title="astrlboy",
    description="Autonomous AI agent operating as a freelance contractor",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router)


if __name__ == "__main__":
    import os

    # Railway assigns a PORT — app must bind to it for healthchecks to reach us
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level=settings.log_level.lower(),
    )
