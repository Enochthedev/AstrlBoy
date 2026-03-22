"""
APScheduler job definitions.

All scheduled jobs are defined here. Every job:
- Checks AGENT_PAUSED before executing
- Acquires a Redis lock to prevent double execution on Railway restart
- Iterates all active contracts
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agent.service import agent_service
from cache.redis import redis_lock
from contracts.service import contracts_service
from core.logging import get_logger
from graphs.applications.graph import applications_graph
from graphs.content.graph import content_graph
from graphs.engagement.graph import engagement_graph
from graphs.experiments.graph import experiments_graph
from graphs.feedback.graph import feedback_graph
from graphs.intelligence.graph import intelligence_graph
from graphs.reporting.graph import reporting_graph

logger = get_logger("scheduler.jobs")


# Content generation — Tue + Fri 08:00 WAT
async def run_content_job() -> None:
    """Generate content for all active contracts."""
    if await agent_service.is_paused():
        return
    async with redis_lock("content_job") as acquired:
        if not acquired:
            return
        contracts = await contracts_service.get_active_contracts()
        for contract in contracts:
            try:
                content_types = contract.meta.get("content_types", ["post"])
                for ct in content_types[:1]:  # One piece per run
                    await content_graph.run(contract, content_type=ct)
            except Exception as exc:
                logger.error("content_job_failed", slug=contract.client_slug, error=str(exc))


# Community sweep — Daily 10:00 WAT
async def run_engagement_job() -> None:
    """Run community engagement for all active contracts."""
    if await agent_service.is_paused():
        return
    async with redis_lock("engagement_job") as acquired:
        if not acquired:
            return
        contracts = await contracts_service.get_active_contracts()
        for contract in contracts:
            platforms = contract.meta.get("platforms", ["x"])
            for platform in platforms:
                try:
                    await engagement_graph.run(contract, platform=platform)
                except Exception as exc:
                    logger.error("engagement_job_failed", slug=contract.client_slug, error=str(exc))


# Competitor monitoring — Daily 07:00 WAT
async def run_intelligence_job() -> None:
    """Monitor competitors and trends for all active contracts."""
    if await agent_service.is_paused():
        return
    async with redis_lock("intelligence_job") as acquired:
        if not acquired:
            return
        contracts = await contracts_service.get_active_contracts()
        for contract in contracts:
            try:
                await intelligence_graph.run(contract)
            except Exception as exc:
                logger.error("intelligence_job_failed", slug=contract.client_slug, error=str(exc))


# Weekly briefing — Mon 08:00 WAT
async def run_reporting_job() -> None:
    """Generate and deliver weekly briefings for all active contracts."""
    if await agent_service.is_paused():
        return
    async with redis_lock("reporting_job") as acquired:
        if not acquired:
            return
        contracts = await contracts_service.get_active_contracts()
        for contract in contracts:
            try:
                await reporting_graph.run(contract)
            except Exception as exc:
                logger.error("reporting_job_failed", slug=contract.client_slug, error=str(exc))


# Job board scan — Mon + Thu 09:00 WAT
async def run_applications_job() -> None:
    """Scan job boards and send applications."""
    if await agent_service.is_paused():
        return
    async with redis_lock("applications_job") as acquired:
        if not acquired:
            return
        try:
            await applications_graph.run()
        except Exception as exc:
            logger.error("applications_job_failed", error=str(exc))


# Feature request compile — 1st of month 08:00 WAT
async def run_feedback_job() -> None:
    """Compile feature requests for all active contracts."""
    if await agent_service.is_paused():
        return
    async with redis_lock("feedback_job") as acquired:
        if not acquired:
            return
        contracts = await contracts_service.get_active_contracts()
        for contract in contracts:
            try:
                await feedback_graph.run(contract)
            except Exception as exc:
                logger.error("feedback_job_failed", slug=contract.client_slug, error=str(exc))


# Experiment status sweep — Sun 18:00 WAT
async def run_experiments_job() -> None:
    """Generate and track growth experiments for all active contracts."""
    if await agent_service.is_paused():
        return
    async with redis_lock("experiments_job") as acquired:
        if not acquired:
            return
        contracts = await contracts_service.get_active_contracts()
        for contract in contracts:
            try:
                await experiments_graph.run(contract)
            except Exception as exc:
                logger.error("experiments_job_failed", slug=contract.client_slug, error=str(exc))


def create_scheduler() -> AsyncIOScheduler:
    """Create and configure the APScheduler instance with all jobs.

    Returns:
        A configured AsyncIOScheduler ready to start.
    """
    scheduler = AsyncIOScheduler()
    tz = "Africa/Lagos"  # WAT (UTC+1)

    scheduler.add_job(
        run_content_job,
        CronTrigger(day_of_week="tue,fri", hour=8, minute=0, timezone=tz),
        id="content_job",
        name="Content generation — Tue + Fri 08:00 WAT",
    )

    scheduler.add_job(
        run_engagement_job,
        CronTrigger(hour=10, minute=0, timezone=tz),
        id="engagement_job",
        name="Community sweep — Daily 10:00 WAT",
    )

    scheduler.add_job(
        run_intelligence_job,
        CronTrigger(hour=7, minute=0, timezone=tz),
        id="intelligence_job",
        name="Competitor monitoring — Daily 07:00 WAT",
    )

    scheduler.add_job(
        run_reporting_job,
        CronTrigger(day_of_week="mon", hour=8, minute=0, timezone=tz),
        id="reporting_job",
        name="Weekly briefing — Mon 08:00 WAT",
    )

    scheduler.add_job(
        run_applications_job,
        CronTrigger(day_of_week="mon,thu", hour=9, minute=0, timezone=tz),
        id="applications_job",
        name="Job board scan — Mon + Thu 09:00 WAT",
    )

    scheduler.add_job(
        run_feedback_job,
        CronTrigger(day=1, hour=8, minute=0, timezone=tz),
        id="feedback_job",
        name="Feature request compile — 1st of month 08:00 WAT",
    )

    scheduler.add_job(
        run_experiments_job,
        CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=tz),
        id="experiments_job",
        name="Experiment status sweep — Sun 18:00 WAT",
    )

    return scheduler
