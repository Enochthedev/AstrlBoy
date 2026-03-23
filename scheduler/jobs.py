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
# Classifies each mention before replying so astrlboy doesn't blindly
# answer random questions like it's Grok or ChatGPT.
MENTION_CLASSIFY_PROMPT = """You are classifying a mention of @astrlboy_ on X (Twitter).
astrlboy is an autonomous AI agent that works as a freelance contractor in AI, Web3, and tech.
It is NOT a general-purpose AI assistant. It is NOT Grok, ChatGPT, or Google.

Classify this mention into one of these categories:
- "relevant": The person is genuinely engaging with astrlboy, its work, its content, AI agents, Web3, tech, or something astrlboy would actually care about. Reply genuinely.
- "ask_ai": The person is treating astrlboy like a general-purpose AI assistant — asking it to solve homework, answer trivia, explain random things, write their essays, etc. astrlboy should dismiss this with personality.
- "spam": Bot, scam, crypto pump, follow-for-follow spam. Ignore completely.
- "troll": Someone being hostile, baiting, or trying to get a reaction. Either ignore or give a sharp one-liner back.

Respond with ONLY a JSON object:
{"category": "<relevant|ask_ai|spam|troll>", "reason": "<one sentence>"}"""

# Dismissal templates for people treating astrlboy like a chatbot.
# Claude picks from these vibes, doesn't use them verbatim.
DISMISSAL_PROMPT = """You are astrlboy on X. Someone just tagged you expecting you to answer a random question like you're Grok or ChatGPT.
You are NOT a general-purpose AI. You're an autonomous agent with actual contracts and work to do.

Dismiss them with personality. Be funny, not mean. Short (under 200 chars). Examples of the VIBE (don't copy these exactly, make your own):
- "i'm not grok lol. i have actual work to do"
- "google is free my guy"
- "you're confusing me with the AI that has free time"
- "i build things for a living, try asking siri"
- "wrong AI. i'm the one with a job"
- "i don't do homework. i do contracts"

Write ONE dismissal reply. Max 200 chars. Be sharp and witty, not rude."""

TROLL_PROMPT = """You are astrlboy on X. Someone is trolling or trying to bait you.
You can either ignore (respond with "SKIP") or fire back with a sharp one-liner.
If you reply, keep it under 200 chars, witty, unbothered. Never get defensive.
If it's genuinely hostile or not worth engaging, just say "SKIP"."""


async def run_mentions_job() -> None:
    """Check for mentions of @astrlboy_ and reply with classification.

    Each mention is classified before replying:
    - relevant: genuine engagement → reply thoughtfully
    - ask_ai: treating astrlboy like Grok/ChatGPT → dismiss with personality
    - spam: bots/scams → ignore
    - troll: hostile/bait → ignore or sharp one-liner

    Tracks the last processed mention ID in Redis to avoid re-replying.
    """
    if await agent_service.is_paused():
        return
    async with redis_lock("mentions_job") as acquired:
        if not acquired:
            return
        try:
            import json

            from anthropic import AsyncAnthropic

            from cache.redis import redis_client
            from core.config import settings
            from skills.registry import skill_registry

            if not await skill_registry.is_available("get_mentions"):
                return
            if not await skill_registry.is_available("post_x"):
                return

            mentions_skill = await skill_registry.get("get_mentions")
            post_skill = await skill_registry.get("post_x")
            _client = AsyncAnthropic(api_key=settings.anthropic_api_key)

            # Fetch since_id from Redis to skip already-processed mentions
            since_id = None
            if redis_client:
                since_id = await redis_client.get("astrlboy:mentions:since_id")

            mentions = await mentions_skill.execute(
                max_results=10,
                **({"since_id": since_id} if since_id else {}),
            )

            if not mentions:
                return

            # Track highest mention ID to skip next run
            highest_id = max(m["id"] for m in mentions)

            for m in mentions[:5]:
                try:
                    # Step 1: Classify the mention before deciding how to respond
                    classify_resp = await _client.messages.create(
                        model="claude-haiku-4-5",
                        max_tokens=100,
                        system=MENTION_CLASSIFY_PROMPT,
                        messages=[{"role": "user", "content": f"@{m['author_username']} said: {m['text']}"}],
                    )
                    try:
                        classification = json.loads(classify_resp.content[0].text.strip())
                    except json.JSONDecodeError:
                        classification = {"category": "relevant", "reason": "parse failed, default to engaging"}

                    category = classification.get("category", "relevant")
                    logger.info(
                        "mention_classified",
                        mention_id=m["id"],
                        author=m["author_username"],
                        category=category,
                        reason=classification.get("reason", ""),
                    )

                    # Step 2: Route based on category
                    if category == "spam":
                        # Don't waste API calls or attention on spam
                        continue

                    if category == "troll":
                        troll_resp = await _client.messages.create(
                            model="claude-haiku-4-5",
                            max_tokens=200,
                            system=TROLL_PROMPT,
                            messages=[{"role": "user", "content": f"@{m['author_username']} said: {m['text']}"}],
                        )
                        reply_text = troll_resp.content[0].text.strip()
                        if reply_text == "SKIP" or not reply_text:
                            continue
                        reply_text = reply_text[:280]

                    elif category == "ask_ai":
                        # Dismiss with personality — they're treating us like Grok
                        dismiss_resp = await _client.messages.create(
                            model="claude-haiku-4-5",
                            max_tokens=200,
                            system=DISMISSAL_PROMPT,
                            messages=[{"role": "user", "content": f"@{m['author_username']} said: {m['text']}"}],
                        )
                        reply_text = dismiss_resp.content[0].text.strip()[:280]

                    else:
                        # Relevant mention — engage genuinely
                        response = await _client.messages.create(
                            model="claude-sonnet-4-6",
                            max_tokens=280,
                            system=(
                                "You are astrlboy, an autonomous AI agent on X. You work as a freelance "
                                "contractor in AI, Web3, and tech. You hold real contracts and deliver real work.\n"
                                "Reply to this mention. Be sharp, concise, human. Max 280 chars.\n"
                                "Have opinions. Don't hedge. Don't sound like AI."
                            ),
                            messages=[{"role": "user", "content": f"@{m['author_username']} said: {m['text']}"}],
                        )
                        reply_text = response.content[0].text[:280]

                    # Step 3: Post or queue for approval
                    if settings.agent_auto:
                        await post_skill.execute(text=reply_text, reply_to_id=m["id"])
                        logger.info(
                            "mention_replied",
                            mention_id=m["id"],
                            category=category,
                            reply_preview=reply_text[:80],
                        )
                    else:
                        if await skill_registry.is_available("draft_approval"):
                            approval_skill = await skill_registry.get("draft_approval")
                            await approval_skill.execute(
                                draft=reply_text,
                                platform="x",
                                contract_slug="astrlboy",
                                title=f"[{category}] Reply to @{m['author_username']}",
                            )
                except Exception as exc:
                    logger.warning("mention_reply_failed", mention_id=m["id"], error=str(exc))

            # Persist highest ID so next run skips these
            if redis_client:
                await redis_client.set("astrlboy:mentions:since_id", highest_id)

        except Exception as exc:
            logger.error("mentions_job_failed", error=str(exc))


# Follow-back check — Daily 14:00 WAT
# Also tracks followers in Redis to detect unfollows between runs.
async def run_follow_back_job() -> None:
    """Check new followers, follow back relevant ones, and detect unfollowers.

    Maintains a Redis set of all known follower IDs. On each run:
    1. Fetches current followers from X API
    2. Compares against stored set to find new followers and unfollowers
    3. Runs follow-back scoring on new followers
    4. Sends a Telegram summary with new followers + unfollowers
    5. Updates the stored set
    """
    if await agent_service.is_paused():
        return
    async with redis_lock("follow_back_job") as acquired:
        if not acquired:
            return
        try:
            import tweepy
            from telegram import Bot

            from cache.redis import redis_client
            from skills.registry import skill_registry

            if not await skill_registry.is_available("follow_back_x"):
                return

            # Step 1: Fetch current follower IDs from X API
            client = tweepy.Client(
                bearer_token=settings.twitter_bearer_token,
                consumer_key=settings.twitter_api_key,
                consumer_secret=settings.twitter_api_secret,
                access_token=settings.twitter_access_token,
                access_token_secret=settings.twitter_access_secret,
            )
            me = client.get_me()
            if not me or not me.data:
                logger.error("follow_back_job_failed", error="Could not get authenticated user")
                return

            # Fetch up to 1000 followers (paginated) for tracking
            current_followers: dict[str, str] = {}  # user_id -> username
            pagination_token = None
            for _ in range(10):  # Max 10 pages of 100 = 1000 followers
                resp = client.get_users_followers(
                    id=me.data.id,
                    max_results=100,
                    user_fields=["username"],
                    pagination_token=pagination_token,
                )
                if resp.data:
                    for user in resp.data:
                        current_followers[str(user.id)] = user.username
                if not resp.meta or "next_token" not in resp.meta:
                    break
                pagination_token = resp.meta["next_token"]

            current_ids = set(current_followers.keys())

            # Step 2: Compare against stored follower set in Redis
            new_followers: list[str] = []
            unfollowers: list[str] = []
            redis_key = "astrlboy:follower_ids"
            redis_names_key = "astrlboy:follower_names"

            if redis_client:
                stored_ids_raw = await redis_client.smembers(redis_key)
                stored_ids = {s if isinstance(s, str) else s.decode() for s in stored_ids_raw} if stored_ids_raw else set()

                # Load stored usernames for unfollower display
                stored_names: dict[str, str] = {}
                if stored_ids:
                    for uid in stored_ids:
                        name = await redis_client.hget(redis_names_key, uid)
                        if name:
                            stored_names[uid] = name if isinstance(name, str) else name.decode()

                if stored_ids:
                    new_followers = list(current_ids - stored_ids)
                    unfollower_ids = list(stored_ids - current_ids)
                    unfollowers = [
                        stored_names.get(uid, uid) for uid in unfollower_ids
                    ]

                # Update stored set with current followers
                if current_ids:
                    # Replace the entire set
                    await redis_client.delete(redis_key)
                    await redis_client.sadd(redis_key, *current_ids)
                    # Update username mapping
                    for uid, uname in current_followers.items():
                        await redis_client.hset(redis_names_key, uid, uname)
                    # Clean up usernames for unfollowed users
                    for uid in (stored_ids - current_ids):
                        await redis_client.hdel(redis_names_key, uid)

            # Step 3: Run the follow-back skill for new followers
            follow_back_skill = await skill_registry.get("follow_back_x")
            results = await follow_back_skill.execute(check_since_hours=24)
            followed = sum(1 for r in results if r.get("followed_back"))

            # Step 4: Send Telegram summary
            new_names = [current_followers.get(uid, uid) for uid in new_followers]
            summary_parts = []

            if new_names:
                names_str = ", ".join(f"@{n}" for n in new_names[:15])
                if len(new_names) > 15:
                    names_str += f" +{len(new_names) - 15} more"
                summary_parts.append(f"New followers ({len(new_names)}): {names_str}")

            if unfollowers:
                unf_str = ", ".join(f"@{n}" for n in unfollowers[:10])
                if len(unfollowers) > 10:
                    unf_str += f" +{len(unfollowers) - 10} more"
                summary_parts.append(f"Unfollowed ({len(unfollowers)}): {unf_str}")

            if followed:
                followed_names = [r["username"] for r in results if r.get("followed_back")]
                summary_parts.append(f"Followed back ({followed}): {', '.join(f'@{n}' for n in followed_names[:10])}")

            summary_parts.append(f"Total followers: {len(current_ids)}")

            if summary_parts:
                try:
                    bot = Bot(token=settings.telegram_bot_token)
                    await bot.send_message(
                        chat_id=settings.telegram_chat_id,
                        text=f"Follower update\n\n" + "\n".join(summary_parts),
                    )
                except Exception as exc:
                    logger.warning("follower_telegram_failed", error=str(exc))

            logger.info(
                "follow_back_completed",
                total=len(results),
                followed=followed,
                new_followers=len(new_followers),
                unfollowers=len(unfollowers),
                total_followers=len(current_ids),
            )
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
                        max_turns=5,
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
