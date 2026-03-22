"""
Playbook system — in-context learning from past performance.

Tracks what worked, what didn't, and injects winning patterns into
future prompts. This is astrlboy's memory of its own performance.

Two parts:
1. Performance collector — pulls engagement metrics for past posts, labels them
2. Playbook builder — extracts top-performing content as few-shot examples

The playbook gets injected into the autonomous agent's system prompt
so Claude learns what works without needing a fine-tuned model.
"""

from datetime import datetime, timezone

from anthropic import AsyncAnthropic
from sqlalchemy import select, update

from core.config import settings
from core.logging import get_logger
from db.base import async_session_factory
from db.models.content import Content
from skills.registry import skill_registry

logger = get_logger("agent.playbook")


async def collect_performance_metrics() -> int:
    """Pull engagement metrics for published content that hasn't been scored yet.

    Checks X API for likes, retweets, replies, and impressions on posts
    that have a tweet_id but no engagement_score.

    Returns:
        Number of posts updated with metrics.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(Content)
            .where(Content.status == "published")
            .where(Content.tweet_id.isnot(None))
            .where(Content.engagement_score.is_(None))
            .limit(20)
        )
        posts = result.scalars().all()

    if not posts:
        return 0

    # Use tweepy to fetch tweet metrics
    try:
        import tweepy

        client = tweepy.Client(
            bearer_token=settings.twitter_bearer_token,
            consumer_key=settings.twitter_api_key,
            consumer_secret=settings.twitter_api_secret,
            access_token=settings.twitter_access_token,
            access_token_secret=settings.twitter_access_secret,
        )
    except Exception as exc:
        logger.warning("tweepy_init_failed", error=str(exc))
        return 0

    updated = 0
    for post in posts:
        try:
            tweet = client.get_tweet(
                post.tweet_id,
                tweet_fields=["public_metrics"],
            )
            if tweet and tweet.data:
                metrics = tweet.data.get("public_metrics", {})
                likes = metrics.get("like_count", 0)
                retweets = metrics.get("retweet_count", 0)
                replies = metrics.get("reply_count", 0)
                impressions = metrics.get("impression_count", 0)

                # Engagement score = weighted sum normalized
                # Replies are highest signal, then retweets, then likes
                score = (replies * 3) + (retweets * 2) + likes
                if impressions > 0:
                    score = score / max(impressions, 1) * 1000  # per 1k impressions

                async with async_session_factory() as session:
                    await session.execute(
                        update(Content)
                        .where(Content.id == post.id)
                        .values(
                            likes=likes,
                            retweets=retweets,
                            replies=replies,
                            impressions=impressions,
                            engagement_score=round(score, 2),
                            metrics_updated_at=datetime.now(timezone.utc),
                        )
                    )
                    await session.commit()
                updated += 1
        except Exception as exc:
            logger.warning("metrics_fetch_failed", tweet_id=post.tweet_id, error=str(exc))

    logger.info("performance_metrics_collected", updated=updated)
    return updated


async def get_top_performing_content(limit: int = 5) -> list[dict]:
    """Get the highest-engagement content for use as few-shot examples.

    Args:
        limit: Number of top posts to return.

    Returns:
        List of dicts with body, engagement_score, likes, retweets, replies.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(Content)
            .where(Content.engagement_score.isnot(None))
            .where(Content.engagement_score > 0)
            .order_by(Content.engagement_score.desc())
            .limit(limit)
        )
        posts = result.scalars().all()

    return [
        {
            "body": post.body[:280],
            "engagement_score": post.engagement_score,
            "likes": post.likes or 0,
            "retweets": post.retweets or 0,
            "replies": post.replies or 0,
            "platform": post.platform or "x",
            "type": post.type,
        }
        for post in posts
    ]


async def get_low_performing_content(limit: int = 3) -> list[dict]:
    """Get the lowest-engagement content to learn what NOT to do.

    Args:
        limit: Number of bottom posts to return.

    Returns:
        List of dicts with body and metrics.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(Content)
            .where(Content.engagement_score.isnot(None))
            .where(Content.impressions > 10)  # only count posts that were actually seen
            .order_by(Content.engagement_score.asc())
            .limit(limit)
        )
        posts = result.scalars().all()

    return [
        {
            "body": post.body[:280],
            "engagement_score": post.engagement_score,
            "likes": post.likes or 0,
            "retweets": post.retweets or 0,
            "replies": post.replies or 0,
        }
        for post in posts
    ]


async def build_playbook_prompt() -> str:
    """Build a playbook section for injection into system prompts.

    Pulls top and bottom performing content, analyzes patterns,
    and returns a prompt section that teaches Claude what works.

    Returns:
        A string to append to the system prompt, or empty string if no data.
    """
    top = await get_top_performing_content(5)
    bottom = await get_low_performing_content(3)

    if not top:
        return ""  # no performance data yet

    playbook = "\n\nPLAYBOOK (learned from past performance):\n"

    playbook += "\nTop-performing posts (study these patterns):\n"
    for i, post in enumerate(top, 1):
        playbook += (
            f"{i}. [{post['likes']}L {post['retweets']}RT {post['replies']}R] "
            f"{post['body'][:150]}\n"
        )

    if bottom:
        playbook += "\nLow-performing posts (avoid these patterns):\n"
        for i, post in enumerate(bottom, 1):
            playbook += (
                f"{i}. [{post['likes']}L {post['retweets']}RT {post['replies']}R] "
                f"{post['body'][:150]}\n"
            )

    playbook += (
        "\nLearn from the patterns above. Double down on what gets engagement. "
        "Avoid what falls flat. Every post should aim to beat your average.\n"
    )

    return playbook


async def analyze_patterns() -> dict:
    """Use Claude to analyze patterns in top vs bottom performing content.

    Returns a structured analysis of what works and what doesn't.

    Returns:
        Dict with 'winning_patterns', 'losing_patterns', 'recommendations'.
    """
    top = await get_top_performing_content(10)
    bottom = await get_low_performing_content(5)

    if len(top) < 3:
        return {"winning_patterns": [], "losing_patterns": [], "recommendations": []}

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    top_text = "\n".join(
        f"[{p['likes']}L {p['retweets']}RT {p['replies']}R] {p['body'][:200]}"
        for p in top
    )
    bottom_text = "\n".join(
        f"[{p['likes']}L {p['retweets']}RT {p['replies']}R] {p['body'][:200]}"
        for p in bottom
    ) if bottom else "No low-performing data yet."

    response = await client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1000,
        system=(
            "Analyze these tweet performance metrics and identify patterns.\n"
            "What makes the top posts work? What makes the bottom ones fail?\n\n"
            "Respond in this exact format:\n"
            "WINNING:\n- pattern 1\n- pattern 2\n\n"
            "LOSING:\n- pattern 1\n- pattern 2\n\n"
            "RECOMMENDATIONS:\n- recommendation 1\n- recommendation 2"
        ),
        messages=[{
            "role": "user",
            "content": f"TOP POSTS:\n{top_text}\n\nBOTTOM POSTS:\n{bottom_text}",
        }],
    )

    text = response.content[0].text
    result: dict[str, list[str]] = {
        "winning_patterns": [],
        "losing_patterns": [],
        "recommendations": [],
    }

    current_section = ""
    for line in text.strip().split("\n"):
        line = line.strip()
        if line.startswith("WINNING"):
            current_section = "winning_patterns"
        elif line.startswith("LOSING"):
            current_section = "losing_patterns"
        elif line.startswith("RECOMMENDATION"):
            current_section = "recommendations"
        elif line.startswith("- ") and current_section:
            result[current_section].append(line[2:])

    return result


async def request_new_skill(
    skill_name: str,
    reason: str,
    use_case: str,
) -> None:
    """Request a new skill that astrlboy wants to learn.

    Sends a notification to Wave via Telegram describing
    the skill the agent wants and why.

    Args:
        skill_name: Proposed name for the skill.
        reason: Why the agent wants this skill.
        use_case: Specific use case that triggered the request.
    """
    try:
        from telegram import Bot

        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=(
                "Skill Request\n\n"
                f"Skill: {skill_name}\n"
                f"Reason: {reason}\n"
                f"Use case: {use_case}\n\n"
                "If you want to build this, add it to SKILLS.md and I'll implement it."
            ),
        )
        logger.info("skill_requested", skill_name=skill_name, reason=reason)
    except Exception as exc:
        logger.warning("skill_request_notify_failed", error=str(exc))
