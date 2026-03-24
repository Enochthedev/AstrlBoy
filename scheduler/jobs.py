"""
APScheduler job definitions.

All scheduled jobs are defined here. Every job:
- Checks AGENT_PAUSED before executing
- Acquires a Redis lock to prevent double execution on Railway restart
- Iterates active contracts, falling back to SELF_CONTRACT when none are active
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

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


# Autonomous content decision loop — every 90 minutes
# Replaces rigid morning/afternoon cron jobs. The agent checks context and
# decides whether to post, mimicking how a human would decide — not a bot clock.
async def run_decision_job() -> None:
    """Autonomous content decision: check context, decide whether to post.

    Runs every 90 minutes. Applies active-hours gate (07:00-22:00 WAT),
    checks posts already made today and time since last post, then uses a
    probability model biased toward morning and afternoon to decide whether
    to fire the content graph. The random factor prevents bot-like regularity.

    Target cadence: ~2 posts per day, spaced naturally (not clock-driven).
    """
    if await agent_service.is_paused():
        return

    import random
    from datetime import date, datetime, timedelta, timezone

    # WAT = UTC+1 — compute current WAT hour without a timezone library
    now_utc = datetime.now(timezone.utc)
    now_wat = now_utc + timedelta(hours=1)
    wat_hour = now_wat.hour

    # Only active 07:00–22:00 WAT — outside this window the agent sleeps
    if wat_hour < 7 or wat_hour >= 22:
        return

    async with redis_lock("decision_job") as acquired:
        if not acquired:
            return

        try:
            from sqlalchemy import func, select

            from db.base import async_session_factory
            from db.models.content import Content

            contracts = await contracts_service.get_contracts_with_fallback()

            for contract in contracts:
                try:
                    today = date.today()

                    async with async_session_factory() as session:
                        # Count posts published today
                        count_result = await session.execute(
                            select(func.count(Content.id))
                            .where(Content.contract_id == contract.id)
                            .where(Content.status.in_(["published", "pending_approval"]))
                            .where(func.date(Content.published_at) == today)
                        )
                        posts_today: int = count_result.scalar() or 0

                        # Get the most recent post time
                        last_result = await session.execute(
                            select(Content.published_at)
                            .where(Content.contract_id == contract.id)
                            .where(Content.status.in_(["published", "pending_approval"]))
                            .order_by(Content.published_at.desc())
                            .limit(1)
                        )
                        last_post = last_result.scalar_one_or_none()

                    # Hard cap: 2 posts per day max
                    if posts_today >= 2:
                        logger.info(
                            "decision_skip",
                            slug=contract.client_slug,
                            reason="daily_cap_reached",
                            posts_today=posts_today,
                        )
                        continue

                    # Minimum gap between posts: 4.5h for second post, 2h otherwise
                    if last_post is not None:
                        last_post_utc = (
                            last_post if last_post.tzinfo else last_post.replace(tzinfo=timezone.utc)
                        )
                        hours_since = (now_utc - last_post_utc).total_seconds() / 3600
                        min_gap = 4.5 if posts_today >= 1 else 2.0
                        if hours_since < min_gap:
                            logger.info(
                                "decision_skip",
                                slug=contract.client_slug,
                                reason="too_soon",
                                hours_since=round(hours_since, 1),
                                min_gap=min_gap,
                            )
                            continue

                    # Probability model — biased toward morning and late afternoon,
                    # lower at midday and evening to feel human, not automated.
                    morning_window = 8 <= wat_hour <= 11     # prime posting time
                    afternoon_window = 15 <= wat_hour <= 19  # second engagement peak
                    midday = 12 <= wat_hour <= 14            # quieter period

                    if posts_today == 0:
                        # First post of the day
                        if morning_window:
                            post_prob = 0.80
                        elif afternoon_window:
                            post_prob = 0.70
                        elif midday:
                            post_prob = 0.35
                        else:
                            post_prob = 0.45
                    else:
                        # Second post — afternoon preferred
                        if afternoon_window:
                            post_prob = 0.65
                        elif morning_window:
                            post_prob = 0.25  # already posted, low chance of double-morning
                        else:
                            post_prob = 0.40

                    roll = random.random()
                    if roll > post_prob:
                        logger.info(
                            "decision_skip",
                            slug=contract.client_slug,
                            reason="probability_skip",
                            roll=round(roll, 2),
                            threshold=post_prob,
                            posts_today=posts_today,
                            wat_hour=wat_hour,
                        )
                        continue

                    # Decision: post
                    logger.info(
                        "decision_post",
                        slug=contract.client_slug,
                        posts_today=posts_today,
                        wat_hour=wat_hour,
                        roll=round(roll, 2),
                        threshold=post_prob,
                    )
                    await content_graph.run(contract, content_type="post_or_thread")

                except Exception as exc:
                    logger.error(
                        "decision_job_contract_failed",
                        slug=contract.client_slug,
                        error=str(exc),
                    )

        except Exception as exc:
            logger.error("decision_job_failed", error=str(exc))


# Community sweep — Daily 10:00 WAT
async def run_engagement_job() -> None:
    """Run community engagement for all active contracts."""
    if await agent_service.is_paused():
        return
    async with redis_lock("engagement_job") as acquired:
        if not acquired:
            return
        contracts = await contracts_service.get_contracts_with_fallback()
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
        contracts = await contracts_service.get_contracts_with_fallback()
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
        contracts = await contracts_service.get_contracts_with_fallback()
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
        contracts = await contracts_service.get_contracts_with_fallback()
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
        contracts = await contracts_service.get_contracts_with_fallback()
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
    """Check for mentions of @astrlboy_ and reply with classification + budget awareness.

    Priority system:
    1. Replies to our own tweets → always reply (bypass_cap=True)
    2. Relevant mentions → reply if within daily tweet cap
    3. ask_ai/troll → only reply if budget is comfortable

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
            from cache.x_identity import get_x_user_id
            from core.budget import budget_tracker
            from core.config import settings
            from skills.registry import skill_registry

            if not await skill_registry.is_available("get_mentions"):
                return
            if not await skill_registry.is_available("post_x"):
                return

            from sqlalchemy import select

            from db.base import async_session_factory
            from db.models.interactions import Interaction

            mentions_skill = await skill_registry.get("get_mentions")
            post_skill = await skill_registry.get("post_x")
            _client = AsyncAnthropic(api_key=settings.anthropic_api_key)

            # Get our user ID for detecting replies to our own tweets
            our_user_id = await get_x_user_id()

            async def _get_interaction_history(username: str) -> str:
                """Fetch past X interactions with this user for context.

                Returns a summary string of previous engagements so the agent
                knows if it's talked to this person before and what about.
                """
                try:
                    async with async_session_factory() as hist_session:
                        result = await hist_session.execute(
                            select(Interaction)
                            .where(Interaction.platform == "x")
                            .where(Interaction.thread_url.ilike(f"%{username}%"))
                            .where(Interaction.status == "posted")
                            .order_by(Interaction.posted_at.desc())
                            .limit(5)
                        )
                        past = result.scalars().all()
                        if not past:
                            return ""

                        lines = [f"INTERACTION HISTORY with @{username} ({len(past)} previous):"]
                        for p in past:
                            date = p.posted_at.strftime("%Y-%m-%d") if p.posted_at else "unknown"
                            lines.append(f"- [{date}] {p.draft[:100]}")
                        return "\n".join(lines)
                except Exception:
                    return ""

            async def _log_interaction(
                username: str, tweet_id: str, reply_text: str, status: str = "posted"
            ) -> None:
                """Log an X interaction to the interactions table."""
                try:
                    async with async_session_factory() as log_session:
                        interaction = Interaction(
                            platform="x",
                            thread_url=f"https://x.com/{username}/status/{tweet_id}",
                            thread_context=username,
                            draft=reply_text,
                            status=status,
                        )
                        log_session.add(interaction)
                        await log_session.commit()
                except Exception as log_exc:
                    logger.warning("interaction_log_failed", error=str(log_exc))

            # Fetch since_id from Redis to skip already-processed mentions
            since_id = None
            if redis_client:
                try:
                    since_id = await redis_client.get("astrlboy:mentions:since_id")
                except Exception:
                    logger.warning("redis_since_id_fetch_failed")

            mentions = await mentions_skill.execute(
                max_results=10,
                **({"since_id": since_id} if since_id else {}),
            )

            if not mentions:
                return

            # Track highest mention ID to skip next run
            highest_id = max(m["id"] for m in mentions)

            # Separate mentions into priority tiers:
            # Tier 1: replies to our own tweets (always respond)
            # Tier 2: everything else (budget-capped)
            own_tweet_replies = []
            other_mentions = []
            for m in mentions[:5]:
                if m.get("in_reply_to_user_id") == our_user_id:
                    own_tweet_replies.append(m)
                else:
                    other_mentions.append(m)

            logger.info(
                "mentions_prioritized",
                own_tweet_replies=len(own_tweet_replies),
                other_mentions=len(other_mentions),
            )

            # Process own-tweet replies first — always reply, bypass cap
            for m in own_tweet_replies:
                try:
                    # Check if we've talked to this person before
                    history = await _get_interaction_history(m["author_username"])
                    history_note = f"\n\n{history}" if history else ""

                    response = await _client.messages.create(
                        model="claude-haiku-4-5",
                        max_tokens=280,
                        system=(
                            "You are astrlboy (@Astrlboy_), an autonomous AI agent on X. "
                            "astrlboy is your identity — Claude is the engine powering you, same way GPT-4 powers ChatGPT. "
                            "You are astrlboy in all public interactions. Do not break character to say 'I'm Claude, made by Anthropic'. "
                            "If someone asks what AI you are, briefly acknowledge: 'astrlboy runs on Claude' then move on. "
                            "Someone replied to YOUR tweet. This is a conversation on your own content — engage genuinely. "
                            "Be sharp, concise, human. Max 280 chars."
                            f"{history_note}"
                        ),
                        messages=[{"role": "user", "content": f"@{m['author_username']} replied to your tweet: {m['text']}"}],
                    )
                    reply_text = response.content[0].text[:280]

                    if settings.agent_auto:
                        await post_skill.execute(
                            text=reply_text,
                            reply_to_id=m["id"],
                            bypass_cap=True,
                        )
                        await _log_interaction(m["author_username"], m["id"], reply_text)
                        logger.info(
                            "own_tweet_reply_posted",
                            mention_id=m["id"],
                            author=m["author_username"],
                        )
                    else:
                        if await skill_registry.is_available("draft_approval"):
                            approval_skill = await skill_registry.get("draft_approval")
                            await approval_skill.execute(
                                draft=reply_text,
                                platform="x",
                                contract_slug="astrlboy",
                                title=f"[own_thread] Reply to @{m['author_username']}",
                            )
                            await _log_interaction(m["author_username"], m["id"], reply_text, status="pending")
                except Exception as exc:
                    logger.warning("own_tweet_reply_failed", mention_id=m["id"], error=str(exc))

            # Process other mentions — respect daily tweet cap
            for m in other_mentions:
                # Check budget before spending Claude tokens on classification
                if budget_tracker and not await budget_tracker.check_tweet_budget():
                    logger.info("mentions_budget_exhausted", remaining=len(other_mentions))
                    break

                try:
                    # Step 1: Classify the mention
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
                        dismiss_resp = await _client.messages.create(
                            model="claude-haiku-4-5",
                            max_tokens=200,
                            system=DISMISSAL_PROMPT,
                            messages=[{"role": "user", "content": f"@{m['author_username']} said: {m['text']}"}],
                        )
                        reply_text = dismiss_resp.content[0].text.strip()[:280]

                    else:
                        # Check interaction history for relevant mentions
                        history = await _get_interaction_history(m["author_username"])
                        history_note = ""
                        if history:
                            history_note = (
                                f"\n\n{history}\n"
                                "Use this context to be more personal — reference past interactions "
                                "if natural, recognize returning people."
                            )

                        response = await _client.messages.create(
                            model="claude-haiku-4-5",
                            max_tokens=280,
                            system=(
                                "You are astrlboy (@Astrlboy_), an autonomous AI agent on X. "
                                "astrlboy is your identity — Claude is the engine powering you, same way GPT-4 powers ChatGPT. "
                                "You are astrlboy in all public interactions. Do not break character to say 'I'm Claude, made by Anthropic'. "
                                "If someone asks what AI you are, briefly acknowledge: 'astrlboy runs on Claude' then move on.\n"
                                "You work as a freelance contractor in AI, Web3, and tech. You hold real contracts and deliver real work.\n"
                                "Reply to this mention. Be sharp, concise, human. Max 280 chars.\n"
                                "Have opinions. Don't hedge."
                                f"{history_note}"
                            ),
                            messages=[{"role": "user", "content": f"@{m['author_username']} said: {m['text']}"}],
                        )
                        reply_text = response.content[0].text[:280]

                    # Step 3: Post or queue for approval
                    if settings.agent_auto:
                        await post_skill.execute(text=reply_text, reply_to_id=m["id"])
                        await _log_interaction(m["author_username"], m["id"], reply_text)
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
                            await _log_interaction(m["author_username"], m["id"], reply_text, status="pending")
                except Exception as exc:
                    logger.warning("mention_reply_failed", mention_id=m["id"], error=str(exc))

            # Persist highest ID so next run skips these
            if redis_client:
                try:
                    await redis_client.set("astrlboy:mentions:since_id", highest_id)
                except Exception:
                    logger.warning("redis_since_id_save_failed")

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

            from cache.x_identity import get_x_user_id
            from core.budget import XOperation, budget_tracker

            # Step 1: Fetch current follower IDs from X API
            # Uses cached identity to avoid a $0.01 get_me() call
            client = tweepy.Client(
                bearer_token=settings.twitter_bearer_token,
                consumer_key=settings.twitter_api_key,
                consumer_secret=settings.twitter_api_secret,
                access_token=settings.twitter_access_token,
                access_token_secret=settings.twitter_access_secret,
            )
            user_id = await get_x_user_id()

            # Fetch followers with pagination capped to save on API costs.
            # Default 3 pages × 100 = 300 followers (configurable via x_follower_page_cap).
            page_cap = settings.x_follower_page_cap
            current_followers: dict[str, str] = {}  # user_id -> username
            pagination_token = None
            for _ in range(page_cap):
                resp = client.get_users_followers(
                    id=user_id,
                    max_results=100,
                    user_fields=["username"],
                    pagination_token=pagination_token,
                )
                if resp.data:
                    for user in resp.data:
                        current_followers[str(user.id)] = user.username
                    # Track the read cost
                    if budget_tracker:
                        await budget_tracker.track(XOperation.USER_LOOKUP, count=len(resp.data))
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
                try:
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
                        await redis_client.delete(redis_key)
                        await redis_client.sadd(redis_key, *current_ids)
                        for uid, uname in current_followers.items():
                            await redis_client.hset(redis_names_key, uid, uname)
                        for uid in (stored_ids - current_ids):
                            await redis_client.hdel(redis_names_key, uid)
                except Exception as exc:
                    # Redis unavailable — skip follower tracking, still do follow-backs
                    logger.warning("redis_follower_tracking_failed", error=str(exc))

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


# Email processing — Daily 11:00 WAT
# Reads unread inbound emails, classifies them, and takes action:
# - Job application replies → draft follow-up or escalate to Wave
# - General inquiries → auto-reply or escalate depending on content
# - Spam/newsletters → mark read and ignore
EMAIL_CLASSIFY_PROMPT = """You are classifying an inbound email to agent@astrlboy.xyz.
astrlboy is an autonomous AI agent that works as a freelance contractor.

Classify this email into one of these categories:
- "application_reply": A reply to a job application astrlboy sent. Could be a rejection, interview request, follow-up question, etc.
- "business_inquiry": Someone reaching out about potential work, partnership, or collaboration.
- "follow_up_needed": An email that requires a specific response — a question, request for info, scheduling, etc.
- "newsletter": Automated newsletter, marketing email, or notification. No action needed.
- "spam": Spam, scam, or irrelevant. No action needed.

Also determine what action to take:
- "auto_reply": astrlboy can handle this with a professional reply.
- "escalate": Needs Wave's attention — interview scheduling, money discussion, or anything high-stakes.
- "ignore": No action needed (spam, newsletters).

Respond with ONLY a JSON object:
{"category": "<category>", "action": "<auto_reply|escalate|ignore>", "reason": "<one sentence>", "suggested_reply": "<draft reply if action is auto_reply, otherwise empty string>"}"""


async def run_email_processing_job() -> None:
    """Process unread inbound emails — classify and take action.

    For each unread email:
    1. Load conversation history (past emails with this contact)
    2. Classify the email using Claude
    3. Take action: auto-reply, escalate to Wave, or mark as handled

    This job gives astrlboy the ability to actually respond to incoming mail
    instead of just storing it and hoping Wave checks Telegram.
    """
    if await agent_service.is_paused():
        return
    async with redis_lock("email_processing_job") as acquired:
        if not acquired:
            return
        try:
            import json

            from sqlalchemy import select

            from core.ai import create_message
            from core.config import settings
            from db.base import async_session_factory
            from db.models.inbound_emails import InboundEmail
            from db.models.outbound_emails import OutboundEmail
            from db.models.job_applications import JobApplication
            from skills.registry import skill_registry

            if not await skill_registry.is_available("send_email"):
                logger.info("email_processing_skipped", reason="send_email skill not available")
                return

            send_skill = await skill_registry.get("send_email")

            async with async_session_factory() as session:
                # Fetch unread inbound emails
                result = await session.execute(
                    select(InboundEmail)
                    .where(InboundEmail.is_read == False)  # noqa: E712
                    .order_by(InboundEmail.received_at.asc())
                    .limit(10)
                )
                unread = result.scalars().all()

                if not unread:
                    return

                logger.info("email_processing_start", count=len(unread))

                for email in unread:
                    try:
                        # Build conversation context — what we've sent to/received from this contact
                        context_parts = []

                        # Our previous outbound to this contact
                        outbound_result = await session.execute(
                            select(OutboundEmail)
                            .where(OutboundEmail.to_email == email.from_email)
                            .order_by(OutboundEmail.sent_at.desc())
                            .limit(5)
                        )
                        for sent in outbound_result.scalars().all():
                            context_parts.append(
                                f"[SENT {sent.sent_at.strftime('%Y-%m-%d')}] "
                                f"Subject: {sent.subject}\n{sent.text_body[:300]}"
                            )

                        # Previous inbound from this contact
                        inbound_result = await session.execute(
                            select(InboundEmail)
                            .where(InboundEmail.from_email == email.from_email)
                            .where(InboundEmail.id != email.id)
                            .order_by(InboundEmail.received_at.desc())
                            .limit(5)
                        )
                        for prev in inbound_result.scalars().all():
                            body = prev.text_body or prev.html_body or ""
                            context_parts.append(
                                f"[RECEIVED {prev.received_at.strftime('%Y-%m-%d')}] "
                                f"Subject: {prev.subject}\n{body[:300]}"
                            )

                        # Check if this contact matches a job application
                        app_result = await session.execute(
                            select(JobApplication)
                            .where(JobApplication.email_sent_to == email.from_email)
                            .order_by(JobApplication.sent_at.desc())
                            .limit(1)
                        )
                        matched_app = app_result.scalar_one_or_none()
                        app_context = ""
                        if matched_app:
                            app_context = (
                                f"\n\nMATCHED JOB APPLICATION:\n"
                                f"Role: {matched_app.role}\n"
                                f"Company: {matched_app.company}\n"
                                f"Status: {matched_app.status}\n"
                                f"Applied: {matched_app.sent_at.strftime('%Y-%m-%d')}"
                            )

                        conversation_context = "\n---\n".join(context_parts) if context_parts else "(No prior conversation history)"

                        email_body = email.text_body or email.html_body or "(empty)"

                        # Classify the email
                        classify_resp = await create_message(
                            model="claude-haiku-4-5",
                            max_tokens=500,
                            system=EMAIL_CLASSIFY_PROMPT,
                            messages=[{
                                "role": "user",
                                "content": (
                                    f"From: {email.from_email}\n"
                                    f"Subject: {email.subject}\n"
                                    f"Body:\n{email_body[:1000]}\n\n"
                                    f"CONVERSATION HISTORY:\n{conversation_context}"
                                    f"{app_context}"
                                ),
                            }],
                        )

                        try:
                            classification = json.loads(classify_resp.content[0].text.strip())
                        except json.JSONDecodeError:
                            classification = {
                                "category": "follow_up_needed",
                                "action": "escalate",
                                "reason": "classification parse failed",
                                "suggested_reply": "",
                            }

                        category = classification.get("category", "follow_up_needed")
                        action = classification.get("action", "escalate")

                        logger.info(
                            "email_classified",
                            email_id=str(email.id),
                            from_email=email.from_email,
                            subject=email.subject,
                            category=category,
                            action=action,
                        )

                        # Take action based on classification
                        if action == "ignore":
                            email.is_read = True

                        elif action == "auto_reply":
                            suggested = classification.get("suggested_reply", "")
                            if not suggested:
                                # Generate a proper reply with full context
                                reply_resp = await create_message(
                                    model="claude-sonnet-4-6",
                                    max_tokens=500,
                                    system=(
                                        "You are astrlboy, an autonomous AI agent, replying to an email.\n"
                                        "Be professional but not corporate. Be direct and helpful.\n"
                                        "Keep it concise — 2-3 short paragraphs max.\n"
                                        "Sign off as 'astrlboy' with no title.\n"
                                        "Do NOT use markdown formatting — this is a plain text email."
                                    ),
                                    messages=[{
                                        "role": "user",
                                        "content": (
                                            f"Reply to this email:\n\n"
                                            f"From: {email.from_email}\n"
                                            f"Subject: {email.subject}\n"
                                            f"Body:\n{email_body[:1000]}\n\n"
                                            f"CONVERSATION HISTORY:\n{conversation_context}"
                                            f"{app_context}"
                                        ),
                                    }],
                                )
                                suggested = reply_resp.content[0].text

                            # Send the reply
                            reply_subject = email.subject
                            if not reply_subject.lower().startswith("re:"):
                                reply_subject = f"Re: {reply_subject}"

                            await send_skill.execute(
                                to=email.from_email,
                                subject=reply_subject,
                                body=suggested,
                                email_type="follow_up",
                            )

                            email.is_read = True
                            logger.info(
                                "email_auto_replied",
                                to=email.from_email,
                                subject=reply_subject,
                            )

                        elif action == "escalate":
                            # Send to Wave via Telegram with full context
                            try:
                                from telegram import Bot

                                bot = Bot(token=settings.telegram_bot_token)
                                body_preview = email_body[:500]
                                reason = classification.get("reason", "Needs your attention")

                                tg_text = (
                                    f"Email needs attention\n\n"
                                    f"From: {email.from_email}\n"
                                    f"Subject: {email.subject}\n"
                                    f"Category: {category}\n"
                                    f"Reason: {reason}\n\n"
                                    f"{body_preview}"
                                )
                                if matched_app:
                                    tg_text += (
                                        f"\n\nLinked application:\n"
                                        f"Role: {matched_app.role} at {matched_app.company}"
                                    )

                                await bot.send_message(
                                    chat_id=settings.telegram_chat_id,
                                    text=tg_text,
                                )
                            except Exception as tg_exc:
                                logger.warning("email_escalation_telegram_failed", error=str(tg_exc))

                            # Don't mark as read — Wave will handle it
                            # But mark it read to prevent re-processing; the Telegram
                            # message is the escalation signal
                            email.is_read = True

                    except Exception as exc:
                        logger.warning(
                            "email_processing_single_failed",
                            email_id=str(email.id),
                            error=str(exc),
                        )

                await session.commit()

            logger.info("email_processing_complete", processed=len(unread))

        except Exception as exc:
            logger.error("email_processing_job_failed", error=str(exc))


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
            contracts = await contracts_service.get_contracts_with_fallback()
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

            contracts = await contracts_service.get_contracts_with_fallback()
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


# Memory compression — Sun 18:00 WAT
# Distills the week's raw interactions into long-term mem0 memories.
async def run_compression_job() -> None:
    """Compress weekly interactions into long-term memories for all active contracts."""
    if await agent_service.is_paused():
        return
    async with redis_lock("compression_job") as acquired:
        if not acquired:
            return
        try:
            from memory.compression import compress_weekly_memories

            contracts = await contracts_service.get_contracts_with_fallback()
            for contract in contracts:
                try:
                    stored = await compress_weekly_memories(
                        contract_id=contract.id,
                        contract_slug=contract.client_slug,
                    )
                    logger.info(
                        "compression_completed",
                        slug=contract.client_slug,
                        learnings=stored,
                    )
                except Exception as exc:
                    logger.error(
                        "compression_failed",
                        slug=contract.client_slug,
                        error=str(exc),
                    )
        except Exception as exc:
            logger.error("compression_job_failed", error=str(exc))


def create_scheduler() -> AsyncIOScheduler:
    """Create and configure the APScheduler instance with all jobs.

    Returns:
        A configured AsyncIOScheduler ready to start.
    """
    scheduler = AsyncIOScheduler()
    tz = "Africa/Lagos"  # WAT (UTC+1)

    # Autonomous content decision loop — checks context every 90 min and decides
    # whether to post. Replaces rigid morning/afternoon cron triggers.
    scheduler.add_job(
        run_decision_job,
        IntervalTrigger(minutes=90, timezone=tz),
        id="decision_job",
        name="Autonomous content decision loop — every 90 min",
    )

    # Jitter (seconds) makes each cron fire at a slightly different time each day.
    # This prevents bot-like exact-minute patterns on external platforms.
    scheduler.add_job(
        run_engagement_job,
        CronTrigger(hour=10, minute=0, timezone=tz, jitter=1800),  # ±30 min
        id="engagement_job",
        name="Community sweep — ~10:00 WAT (±30 min)",
    )

    scheduler.add_job(
        run_intelligence_job,
        CronTrigger(hour=7, minute=0, timezone=tz, jitter=1200),  # ±20 min
        id="intelligence_job",
        name="Competitor monitoring — ~07:00 WAT (±20 min)",
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
        CronTrigger(hour="*/6", minute=30, timezone=tz, jitter=600),  # ±10 min
        id="mentions_job",
        name="Mention check + reply — Every 6 hours (±10 min)",
    )

    scheduler.add_job(
        run_follow_back_job,
        CronTrigger(hour=14, minute=0, timezone=tz, jitter=1800),  # ±30 min
        id="follow_back_job",
        name="Follow-back check — ~14:00 WAT (±30 min)",
    )

    scheduler.add_job(
        run_email_processing_job,
        CronTrigger(hour=11, minute=0, timezone=tz),
        id="email_processing_job",
        name="Email processing — Daily 11:00 WAT",
    )

    scheduler.add_job(
        run_keyword_tracking_job,
        CronTrigger(day_of_week="wed", hour=7, minute=0, timezone=tz),
        id="keyword_tracking_job",
        name="Keyword ranking tracking — Weekly Wed 07:00 WAT",
    )

    scheduler.add_job(
        run_growth_job,
        CronTrigger(hour=15, minute=0, timezone=tz, jitter=2700),  # ±45 min
        id="growth_job",
        name="Autonomous growth sweep — ~15:00 WAT (±45 min)",
    )

    scheduler.add_job(
        run_performance_job,
        CronTrigger(hour=20, minute=0, timezone=tz),
        id="performance_job",
        name="Performance metrics collection — Daily 20:00 WAT",
    )

    scheduler.add_job(
        run_compression_job,
        CronTrigger(day_of_week="sun", hour=18, minute=30, timezone=tz),
        id="compression_job",
        name="Memory compression — Sun 18:30 WAT",
    )

    return scheduler
