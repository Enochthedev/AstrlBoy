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


# Mention check + reply — Every 2 hours
async def run_mentions_job() -> None:
    """Check for mentions of @astrlboy_ and reply."""
    if await agent_service.is_paused():
        return
    async with redis_lock("mentions_job") as acquired:
        if not acquired:
            return
        try:
            from anthropic import AsyncAnthropic

            from core.config import settings
            from skills.registry import skill_registry

            if not await skill_registry.is_available("get_mentions"):
                return
            if not await skill_registry.is_available("post_x"):
                return

            mentions_skill = await skill_registry.get("get_mentions")
            post_skill = await skill_registry.get("post_x")
            _client = AsyncAnthropic(api_key=settings.anthropic_api_key)

            mentions = await mentions_skill.execute(max_results=10)

            for m in mentions[:5]:
                try:
                    response = await _client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=280,
                        system=(
                            "You are astrlboy, an AI personality on X. Reply to this mention.\n"
                            "Be sharp, concise, human. Max 280 chars. Engage genuinely."
                        ),
                        messages=[{"role": "user", "content": f"@{m['author_username']} said: {m['text']}"}],
                    )
                    reply_text = response.content[0].text[:280]

                    if settings.agent_auto:
                        # Auto mode — reply immediately
                        await post_skill.execute(text=reply_text, reply_to_id=m["id"])
                    else:
                        # Manual mode — send to Telegram for approval
                        if await skill_registry.is_available("draft_approval"):
                            approval_skill = await skill_registry.get("draft_approval")
                            await approval_skill.execute(
                                draft=reply_text,
                                platform="x",
                                contract_slug="astrlboy",
                                title=f"Reply to @{m['author_username']}",
                            )
                except Exception as exc:
                    logger.warning("mention_reply_failed", mention_id=m["id"], error=str(exc))
        except Exception as exc:
            logger.error("mentions_job_failed", error=str(exc))


# Follow-back check — Daily 14:00 WAT
async def run_follow_back_job() -> None:
    """Check new followers and follow back relevant ones."""
    if await agent_service.is_paused():
        return
    async with redis_lock("follow_back_job") as acquired:
        if not acquired:
            return
        try:
            from skills.registry import skill_registry

            if not await skill_registry.is_available("follow_back_x"):
                return

            follow_back_skill = await skill_registry.get("follow_back_x")
            results = await follow_back_skill.execute(check_since_hours=24)
            followed = sum(1 for r in results if r.get("followed_back"))
            logger.info("follow_back_completed", total=len(results), followed=followed)
        except Exception as exc:
            logger.error("follow_back_job_failed", error=str(exc))


# Keyword ranking tracking — Weekly Wed 07:00 WAT
async def run_keyword_tracking_job() -> None:
    """Track keyword rankings for all active contracts."""
    if await agent_service.is_paused():
        return
    async with redis_lock("keyword_tracking_job") as acquired:
        if not acquired:
            return
        try:
            from skills.registry import skill_registry

            if not await skill_registry.is_available("track_keyword_rankings"):
                return

            tracking_skill = await skill_registry.get("track_keyword_rankings")
            contracts = await contracts_service.get_active_contracts()
            for contract in contracts:
                keywords = (contract.meta or {}).get("stream_keywords", [])
                if keywords:
                    try:
                        await tracking_skill.execute(
                            keywords=keywords[:10],
                            contract_slug=contract.client_slug,
                        )
                    except Exception as exc:
                        logger.warning(
                            "keyword_tracking_failed",
                            slug=contract.client_slug,
                            error=str(exc),
                        )
        except Exception as exc:
            logger.error("keyword_tracking_job_failed", error=str(exc))


# Autonomous growth sweep — Daily 15:00 WAT
async def run_growth_job() -> None:
    """Autonomous growth: find accounts, analyze trends, find engagement opportunities."""
    if await agent_service.is_paused():
        return
    async with redis_lock("growth_job") as acquired:
        if not acquired:
            return
        try:
            from agent.autonomous import run_autonomous

            contracts = await contracts_service.get_active_contracts()
            for contract in contracts:
                keywords = (contract.meta or {}).get("stream_keywords", [])
                if not keywords:
                    continue

                # Let the autonomous agent decide what growth actions to take
                task = (
                    f"You are running a scheduled growth sweep for {contract.client_name}.\n"
                    f"Topics: {', '.join(keywords[:5])}\n\n"
                    "Do the following:\n"
                    "1. Use find_engagement_opportunities to find threads worth engaging with\n"
                    "2. Use analyze_trending_content to understand what's performing well\n"
                    "3. Use find_relevant_accounts to discover accounts worth following\n"
                    "4. Follow 2-3 of the most relevant accounts you found\n"
                    "5. Summarize what you found and did.\n\n"
                    "Be selective — quality over quantity."
                )
                try:
                    result = await run_autonomous(
                        task=task,
                        contract=contract,
                        max_turns=10,
                    )
                    logger.info(
                        "growth_sweep_completed",
                        slug=contract.client_slug,
                        turns=result.turns,
                        tools_used=len(result.tool_calls),
                    )
                except Exception as exc:
                    logger.error(
                        "growth_sweep_failed",
                        slug=contract.client_slug,
                        error=str(exc),
                    )
        except Exception as exc:
            logger.error("growth_job_failed", error=str(exc))


# Performance metrics collection — Daily 20:00 WAT
async def run_performance_job() -> None:
    """Collect engagement metrics for published posts to build the playbook."""
    if await agent_service.is_paused():
        return
    async with redis_lock("performance_job") as acquired:
        if not acquired:
            return
        try:
            from agent.playbook import collect_performance_metrics

            updated = await collect_performance_metrics()
            logger.info("performance_job_completed", updated=updated)
        except Exception as exc:
            logger.error("performance_job_failed", error=str(exc))


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

    scheduler.add_job(
        run_mentions_job,
        CronTrigger(hour="*/2", minute=30, timezone=tz),
        id="mentions_job",
        name="Mention check + reply — Every 2 hours",
    )

    scheduler.add_job(
        run_follow_back_job,
        CronTrigger(hour=14, minute=0, timezone=tz),
        id="follow_back_job",
        name="Follow-back check — Daily 14:00 WAT",
    )

    scheduler.add_job(
        run_keyword_tracking_job,
        CronTrigger(day_of_week="wed", hour=7, minute=0, timezone=tz),
        id="keyword_tracking_job",
        name="Keyword ranking tracking — Weekly Wed 07:00 WAT",
    )

    scheduler.add_job(
        run_growth_job,
        CronTrigger(hour=15, minute=0, timezone=tz),
        id="growth_job",
        name="Autonomous growth sweep — Daily 15:00 WAT",
    )

    scheduler.add_job(
        run_performance_job,
        CronTrigger(hour=20, minute=0, timezone=tz),
        id="performance_job",
        name="Performance metrics collection — Daily 20:00 WAT",
    )

    return scheduler
