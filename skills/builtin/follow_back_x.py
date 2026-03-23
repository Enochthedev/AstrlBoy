"""
X (Twitter) follow-back skill.

Checks recent followers and uses Claude to score each one for relevance.
Follows back accounts that score >= 6 and look genuine. Uses the follow_x
skill internally so all follows respect the shared daily limit.

This is the growth flywheel: strategic follow-backs increase engagement rate,
which boosts visibility in X's algorithm. But indiscriminate follow-backs
attract bots and dilute the audience, so every decision goes through Claude.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import tweepy
from anthropic import AsyncAnthropic

from cache.x_identity import get_x_user_id
from core.budget import XOperation, budget_tracker
from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool
from skills.builtin.follow_x import FollowXSkill

logger = get_logger("skills.follow_back_x")

# Minimum relevance score to follow back — set high because we only want
# to follow people who genuinely boost visibility and signal quality.
# We are NOT follow-for-follow. We follow strategically.
MIN_FOLLOWBACK_SCORE = 8

# System prompt for Claude's follower scoring — prioritizes influence,
# visibility, and strategic value over just "seems relevant".
SCORING_SYSTEM_PROMPT = """You are evaluating whether an X (Twitter) account is worth following back for @astrlboy_, an autonomous AI agent that works as a freelance contractor in AI, Web3, and tech.

We follow back SELECTIVELY. Only high-influence, high-visibility accounts that make astrlboy's following list look curated and intentional.

Score the account from 0-10 based on:
- Influence: Do they have a meaningful following? Are they a known voice in their space? (1K+ followers strongly preferred)
- Relevance: Are they in AI, Web3, tech, startups, or adjacent fields?
- Visibility: Would following them increase astrlboy's exposure? Do they engage with others' content?
- Signal quality: Would following them make astrlboy's following list look sharp and intentional?
- Genuineness: Real person/org with real engagement, not a bot or engagement farmer

Score 8-10 (FOLLOW): Influential builders, founders, devs, researchers, VCs, or notable accounts in AI/Web3/tech with real audiences
Score 5-7 (DON'T FOLLOW): Relevant but not influential enough — regular users, small accounts, lurkers
Score 0-4 (DEFINITELY NOT): Bots, spam, no bio, follow-churn accounts, crypto pump language, default avatars

Red flags (auto score 0-2):
- No bio, no profile picture, default avatar
- Following >> followers (follow-churn bot pattern)
- Bio is just emojis or crypto pump language
- Account created very recently with no tweets
- Engagement farming language ("follow for follow", "DM for collab")

Respond with ONLY a JSON object, no other text:
{"score": <int 0-10>, "reason": "<one sentence explanation>", "genuine": <bool>}"""


class FollowBackXSkill(BaseTool):
    """Check new followers on X and selectively follow back using Claude scoring.

    Pulls recent followers, scores each with Claude for relevance and
    genuineness, then follows back those meeting the threshold. All follows
    go through FollowXSkill to respect the shared daily limit.
    """

    name = "follow_back_x"
    description = (
        "Check new X followers and follow back relevant, genuine accounts. "
        "Uses Claude to score each follower. Respects the shared daily follow limit."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        # Bearer token for reading followers (app-level auth)
        self._client = tweepy.Client(
            bearer_token=settings.twitter_bearer_token,
            consumer_key=settings.twitter_api_key,
            consumer_secret=settings.twitter_api_secret,
            access_token=settings.twitter_access_token,
            access_token_secret=settings.twitter_access_secret,
        )
        self._anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)
        # Reuse follow_x skill so all follows go through the same daily counter
        self._follow_skill = FollowXSkill()

    async def _get_authenticated_user_id(self) -> str:
        """Get the authenticated user's X ID from cache (no API call).

        Returns:
            The user ID string for @astrlboy_.

        Raises:
            SkillExecutionError: If the cached identity is unavailable.
        """
        try:
            return await get_x_user_id()
        except Exception as exc:
            raise SkillExecutionError(
                f"Failed to get authenticated user: {exc}"
            ) from exc

    async def _get_recent_followers(
        self,
        user_id: str,
        check_since_hours: int,
    ) -> list[dict[str, Any]]:
        """Fetch recent followers with their profile data.

        The X API v2 followers endpoint doesn't support time-based filtering,
        so we pull followers in reverse chronological order and stop when we
        hit accounts that followed before our cutoff.

        Args:
            user_id: The authenticated user's X ID.
            check_since_hours: How many hours back to check.

        Returns:
            List of follower dicts with id, username, name, bio, metrics.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=check_since_hours)
        followers: list[dict[str, Any]] = []

        try:
            # Fetch followers with profile fields for scoring.
            # Capped to first page only — deeper pagination is expensive
            # ($0.01/user) and rarely needed for follow-back decisions.
            response = self._client.get_users_followers(
                id=user_id,
                max_results=100,
                user_fields=[
                    "description",
                    "public_metrics",
                    "created_at",
                    "profile_image_url",
                    "verified",
                ],
            )

            # Track the read cost
            if budget_tracker and response.data:
                await budget_tracker.track(XOperation.USER_LOOKUP, count=len(response.data))

            if response.data is None:
                return []

            for user in response.data:
                # X API v2 returns followers newest-first. Once we see an account
                # that was created before our cutoff, we've likely gone past recent
                # followers. However, account creation != follow time, so we include
                # all from this batch since we can't precisely filter by follow time.
                metrics = user.public_metrics or {}
                followers.append({
                    "user_id": str(user.id),
                    "username": user.username,
                    "name": user.name,
                    "bio": user.description or "",
                    "followers_count": metrics.get("followers_count", 0),
                    "following_count": metrics.get("following_count", 0),
                    "tweet_count": metrics.get("tweet_count", 0),
                    "created_at": str(user.created_at) if user.created_at else "",
                    "has_profile_image": bool(
                        user.profile_image_url
                        and "default_profile" not in user.profile_image_url
                    ),
                    "verified": getattr(user, "verified", False),
                })

        except Exception as exc:
            logger.error("fetch_followers_failed", error=str(exc))
            raise SkillExecutionError(
                f"Failed to fetch followers: {exc}"
            ) from exc

        logger.info("followers_fetched", count=len(followers))
        return followers

    async def _score_follower(self, follower: dict[str, Any]) -> dict[str, Any]:
        """Use Claude to score a single follower for relevance and genuineness.

        Args:
            follower: Dict with user profile data.

        Returns:
            Dict with 'score' (int), 'reason' (str), 'genuine' (bool).

        Raises:
            SkillExecutionError: If Claude scoring fails.
        """
        user_summary = (
            f"Username: @{follower['username']}\n"
            f"Name: {follower['name']}\n"
            f"Bio: {follower['bio']}\n"
            f"Followers: {follower['followers_count']}\n"
            f"Following: {follower['following_count']}\n"
            f"Tweets: {follower['tweet_count']}\n"
            f"Account created: {follower['created_at']}\n"
            f"Has profile image: {follower['has_profile_image']}\n"
            f"Verified: {follower['verified']}"
        )

        try:
            response = await self._anthropic.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=200,
                system=SCORING_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_summary}],
            )

            raw_text = response.content[0].text.strip()
            result = json.loads(raw_text)

            return {
                "score": int(result.get("score", 0)),
                "reason": str(result.get("reason", "")),
                "genuine": bool(result.get("genuine", False)),
            }
        except (json.JSONDecodeError, IndexError, KeyError) as exc:
            # Claude returned something unparseable — treat as low score
            logger.warning(
                "follower_scoring_parse_error",
                username=follower["username"],
                error=str(exc),
            )
            return {"score": 0, "reason": "Scoring failed — skipping", "genuine": False}
        except Exception as exc:
            logger.error(
                "follower_scoring_failed",
                username=follower["username"],
                error=str(exc),
            )
            raise SkillExecutionError(
                f"Claude scoring failed for @{follower['username']}: {exc}"
            ) from exc

    async def execute(
        self,
        check_since_hours: int = 24,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Check new followers and follow back those that pass scoring.

        Args:
            check_since_hours: How many hours back to check for new followers.
                Defaults to 24.

        Returns:
            List of dicts, one per follower checked, with:
                user_id, username, followed_back, reason.

        Raises:
            SkillExecutionError: If fetching followers or scoring fails.
        """
        user_id = await self._get_authenticated_user_id()
        followers = await self._get_recent_followers(user_id, check_since_hours)

        if not followers:
            logger.info("no_new_followers", check_since_hours=check_since_hours)
            return []

        results: list[dict[str, Any]] = []

        for follower in followers:
            scoring = await self._score_follower(follower)
            score = scoring["score"]
            reason = scoring["reason"]
            genuine = scoring["genuine"]

            followed_back = False

            # Only follow back if score meets threshold AND account looks genuine
            if score >= MIN_FOLLOWBACK_SCORE and genuine:
                try:
                    await self._follow_skill.execute(
                        user_id=follower["user_id"],
                        reason=f"Follow-back: score={score}, {reason}",
                    )
                    followed_back = True
                except SkillExecutionError as exc:
                    # Daily limit reached or API failure — log and continue
                    # to score remaining followers for the audit trail
                    logger.warning(
                        "follow_back_skipped",
                        username=follower["username"],
                        error=str(exc),
                    )
                    reason = f"{reason} (follow failed: {exc})"

            action = "followed_back" if followed_back else "skipped"
            logger.info(
                "follow_back_decision",
                username=follower["username"],
                user_id=follower["user_id"],
                score=score,
                genuine=genuine,
                action=action,
                reason=reason,
            )

            results.append({
                "user_id": follower["user_id"],
                "username": follower["username"],
                "followed_back": followed_back,
                "score": score,
                "reason": reason,
            })

        followed_count = sum(1 for r in results if r["followed_back"])
        logger.info(
            "follow_back_complete",
            total_checked=len(results),
            total_followed_back=followed_count,
        )

        return results

    def get_schema(self) -> dict:
        """Return JSON schema for follow_back_x inputs."""
        return {
            "type": "object",
            "properties": {
                "check_since_hours": {
                    "type": "integer",
                    "description": "How many hours back to check for new followers",
                    "default": 24,
                },
            },
            "required": [],
        }
