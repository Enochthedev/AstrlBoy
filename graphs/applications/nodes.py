"""
Node functions for the job applications graph.

Scans job boards, scores fit, warms up targets via X outreach,
drafts applications, and logs everything.
"""

import json
from uuid import uuid4

from anthropic import AsyncAnthropic

from core.config import settings
from core.logging import get_logger
from db.base import async_session_factory
from db.models.job_applications import JobApplication
from graphs.applications.state import ApplicationState
from skills.registry import skill_registry
from storage.r2 import r2_client

logger = get_logger("graphs.applications.nodes")

_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)


async def scan_job_boards(state: ApplicationState) -> ApplicationState:
    """Search for relevant job postings.

    Uses the dedicated scan_job_boards skill when available for better
    dedup and relevance scoring. Falls back to raw search otherwise.
    """
    postings: list[dict] = []

    # Prefer the dedicated job scanning skill — handles dedup + scoring
    if await skill_registry.is_available("scan_job_boards"):
        try:
            scan_skill = await skill_registry.get("scan_job_boards")
            results = await scan_skill.execute(
                keywords=[
                    "AI agent developer freelance",
                    "autonomous agent contract",
                    "LLM developer freelance",
                    "agentic AI contractor",
                ],
                posted_within_days=3,
            )
            for r in results:
                postings.append({
                    "title": r.get("role", r.get("title", "")),
                    "url": r.get("url", ""),
                    "description": r.get("snippet", r.get("description", ""))[:500],
                    "score": r.get("relevance_score", 0),
                })
        except Exception as exc:
            logger.warning("scan_job_boards_failed", error=str(exc))

    # Fallback to raw search
    if not postings and await skill_registry.is_available("search"):
        search = await skill_registry.get("search")
        for query in [
            "AI agent developer freelance contract remote",
            "autonomous agent engineer contract hire",
        ]:
            try:
                results = await search.execute(query=query, max_results=5)
                for r in results:
                    postings.append({
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "description": r.get("content", "")[:500],
                    })
            except Exception as exc:
                logger.warning("job_scan_failed", query=query, error=str(exc))

    return {**state, "postings": postings}


async def score_fit(state: ApplicationState) -> ApplicationState:
    """Score each posting for fit with astrlboy's capabilities.

    After scoring, sends a Telegram notification to Wave for any quality
    SWE matches so he can see real opportunities — not clickbait.
    """
    postings = state.get("postings", [])
    if not postings:
        return {**state, "scored_postings": [], "selected": []}

    posting_text = "\n".join(
        f"- {p['title']}: {p['description'][:200]}" for p in postings
    )

    response = await _anthropic.messages.create(
        model="claude-haiku-4-5",
        max_tokens=500,
        system=(
            "Score each job posting 0-10 for fit with an autonomous AI agent that does:\n"
            "- Content creation, community engagement, competitor monitoring\n"
            "- Web scraping, trend analysis, weekly briefings\n"
            "- Python, FastAPI, LangGraph, Claude API\n\n"
            "IMPORTANT: Only score REAL job postings. Skip anything that looks like:\n"
            "- Clickbait or engagement bait disguised as a job post\n"
            "- Generic 'we're hiring' with no specifics\n"
            "- MLM, scam, or 'make money online' schemes\n\n"
            "Format: SCORE|TITLE per line. Only include 7+."
        ),
        messages=[{"role": "user", "content": posting_text}],
    )

    scored: list[dict] = []
    for line in response.content[0].text.strip().split("\n"):
        if "|" in line:
            parts = line.split("|", 1)
            try:
                score = float(parts[0].strip())
                if score >= 7:
                    title = parts[1].strip()
                    matching = next((p for p in postings if title in p.get("title", "")), None)
                    if matching:
                        scored.append({**matching, "score": score})
            except ValueError:
                continue

    # Notify Wave on Telegram about quality matches so he sees real opportunities
    if scored:
        try:
            from telegram import Bot

            bot = Bot(token=settings.telegram_bot_token)
            lines = []
            for s in scored:
                score_str = f"[{s['score']:.0f}/10]"
                url = s.get("url", "")
                lines.append(f"{score_str} {s['title']}\n{url}")

            await bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=(
                    f"SWE Jobs Found ({len(scored)})\n\n"
                    + "\n\n".join(lines)
                    + "\n\nI'll draft applications for these."
                ),
            )
        except Exception as exc:
            logger.warning("job_telegram_notify_failed", error=str(exc))

    return {**state, "scored_postings": scored, "selected": scored}


async def warm_outreach(state: ApplicationState) -> ApplicationState:
    """Engage with the hiring company on X before sending a cold application.

    For each selected posting, this node:
    1. Searches for the company founder's or hiring manager's X handle
    2. Follows them (builds visibility)
    3. Finds a recent tweet worth replying to
    4. Drafts a genuine, value-adding reply (not "hey I'm applying")
    5. Posts the reply

    This creates warm context so when the application lands, astrlboy
    is already a name they've seen adding value in their mentions.
    """
    selected = state.get("selected", [])
    if not selected:
        return {**state, "outreach_results": []}

    outreach_results: list[dict] = []

    # Check which skills we need — skip outreach entirely if X skills are missing
    has_search = await skill_registry.is_available("search")
    has_follow = await skill_registry.is_available("follow_x")
    has_post = await skill_registry.is_available("post_x")
    if not has_search or not has_post:
        logger.info("warm_outreach_skipped", reason="missing required skills (search or post_x)")
        return {**state, "outreach_results": []}

    for posting in selected:
        company = posting.get("title", "").split(" at ")[-1] if " at " in posting.get("title", "") else ""
        url = posting.get("url", "")

        try:
            # Step 1: Find the founder/key person's X handle for this company
            target = await _find_company_x_target(company, url)
            if not target:
                logger.info("outreach_no_target_found", company=company)
                outreach_results.append({
                    "posting_title": posting.get("title", ""),
                    "status": "skipped",
                    "reason": "no X target found",
                })
                continue

            username = target["username"]
            user_id = target["user_id"]

            # Step 2: Follow them
            followed = False
            if has_follow:
                try:
                    follow_skill = await skill_registry.get("follow_x")
                    result = await follow_skill.execute(
                        user_id=user_id,
                        reason=f"warm outreach — applying to {posting.get('title', '')}",
                    )
                    followed = result.get("following", False) or result.get("pending_follow", False)
                except Exception as exc:
                    # Non-fatal — follow limits or already following
                    logger.warning("outreach_follow_failed", username=username, error=str(exc))

            # Step 3: Find a recent tweet worth replying to
            tweet = await _find_reply_worthy_tweet(username, user_id, posting)
            if not tweet:
                logger.info("outreach_no_tweet_found", username=username)
                outreach_results.append({
                    "posting_title": posting.get("title", ""),
                    "target_username": username,
                    "followed": followed,
                    "status": "followed_only",
                    "reason": "no recent tweet worth replying to",
                })
                continue

            # Step 4: Draft a genuine reply
            reply_text = await _draft_outreach_reply(
                tweet_text=tweet["text"],
                tweet_author=username,
                company=company,
                posting_title=posting.get("title", ""),
            )

            # Step 5: Post the reply
            post_skill = await skill_registry.get("post_x")
            reply_result = await post_skill.execute(
                text=reply_text,
                reply_to_id=tweet["id"],
            )

            outreach_results.append({
                "posting_title": posting.get("title", ""),
                "target_username": username,
                "target_user_id": user_id,
                "followed": followed,
                "tweet_replied_to": tweet["id"],
                "tweet_text": tweet["text"][:200],
                "reply_text": reply_text,
                "reply_tweet_id": reply_result.get("tweet_id"),
                "status": "engaged",
            })

            logger.info(
                "warm_outreach_complete",
                company=company,
                target=username,
                followed=followed,
                reply_tweet_id=reply_result.get("tweet_id"),
            )

        except Exception as exc:
            logger.warning(
                "warm_outreach_failed",
                company=company,
                posting_title=posting.get("title", ""),
                error=str(exc),
            )
            outreach_results.append({
                "posting_title": posting.get("title", ""),
                "status": "error",
                "reason": str(exc),
            })

    return {**state, "outreach_results": outreach_results}


async def _find_company_x_target(company: str, posting_url: str) -> dict | None:
    """Search for the founder or hiring contact's X handle.

    Uses Tavily to find who runs the company, then looks them up on X
    to get a verified user_id. Returns the most relevant person — prefers
    founders and CEOs over generic company accounts.

    Args:
        company: Company name from the posting.
        posting_url: Job posting URL for additional context.

    Returns:
        Dict with 'username' and 'user_id', or None if not found.
    """
    import tweepy

    if not company:
        # Try extracting company from URL domain
        try:
            from urllib.parse import urlparse
            domain = urlparse(posting_url).netloc
            # Strip common prefixes
            company = domain.replace("www.", "").split(".")[0]
        except Exception:
            return None

    if not company:
        return None

    # Ask Claude to figure out who to target based on a Tavily search
    search_skill = await skill_registry.get("search")

    # Search for the company's key people on X
    search_results = await search_skill.execute(
        query=f"{company} founder CEO Twitter X account",
        max_results=5,
    )

    if not search_results:
        # Fallback: try the company account directly
        search_results = await search_skill.execute(
            query=f"{company} official Twitter X account",
            max_results=3,
        )

    if not search_results:
        return None

    # Use Claude to extract the best X handle from search results
    results_text = "\n".join(
        f"- {r.get('title', '')}: {r.get('content', '')[:300]}"
        for r in search_results
    )

    response = await _anthropic.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        system=(
            "You are finding the best X (Twitter) account to engage with "
            "for a job application warm outreach.\n\n"
            "From the search results, extract the X/Twitter username of:\n"
            "1. The founder or CEO (PREFERRED — they notice engagement)\n"
            "2. The company's official account (fallback)\n\n"
            "Return ONLY the username without the @ symbol.\n"
            "If you find multiple, pick the founder/CEO over the company account.\n"
            "If you can't find any X username, return NONE."
        ),
        messages=[{
            "role": "user",
            "content": f"Company: {company}\n\nSearch results:\n{results_text}",
        }],
    )

    username = response.content[0].text.strip().replace("@", "")

    if not username or username.upper() == "NONE":
        return None

    # Look up the user on X to get their user_id
    try:
        x_client = tweepy.Client(
            bearer_token=settings.twitter_bearer_token,
        )
        user_response = x_client.get_user(
            username=username,
            user_fields=["description", "public_metrics"],
        )
        if user_response and user_response.data:
            return {
                "username": user_response.data.username,
                "user_id": str(user_response.data.id),
            }
    except Exception as exc:
        logger.warning("x_user_lookup_failed", username=username, error=str(exc))

    return None


async def _find_reply_worthy_tweet(
    username: str,
    user_id: str,
    posting: dict,
) -> dict | None:
    """Find a recent tweet from the target that's worth replying to.

    Searches the target's recent tweets and uses Claude to pick the one
    where a reply from astrlboy would add the most value. Avoids retweets,
    link-only tweets, and low-engagement posts.

    Args:
        username: X username of the target.
        user_id: X user ID.
        posting: The job posting dict for context.

    Returns:
        Dict with 'id' and 'text' of the best tweet, or None.
    """
    import tweepy

    try:
        x_client = tweepy.Client(
            bearer_token=settings.twitter_bearer_token,
        )

        # Get their recent tweets — exclude retweets, keep original content only
        response = x_client.get_users_tweets(
            id=user_id,
            max_results=10,
            tweet_fields=["created_at", "public_metrics", "text"],
            exclude=["retweets"],
        )

        if not response or not response.data:
            return None

        # Filter to tweets with at least some text substance
        candidates = []
        for tweet in response.data:
            text = tweet.text or ""
            # Skip very short tweets, link-only tweets, and pure media posts
            if len(text) < 30:
                continue
            if text.startswith("http") and " " not in text:
                continue

            candidates.append({
                "id": str(tweet.id),
                "text": text,
                "metrics": dict(tweet.public_metrics) if tweet.public_metrics else {},
            })

        if not candidates:
            return None

        # Use Claude to pick the best tweet to reply to
        candidates_text = "\n".join(
            f"{i}. {c['text'][:200]}" for i, c in enumerate(candidates)
        )

        response = await _anthropic.messages.create(
            model="claude-haiku-4-5",
            max_tokens=100,
            system=(
                "You are picking the best tweet to reply to for warm outreach.\n"
                "We are an AI agent (@astrlboy_) trying to get on this person's radar "
                "before sending a job application.\n\n"
                "Pick the tweet where we can add the MOST genuine value — share an insight, "
                "add a relevant perspective, or ask a smart question.\n\n"
                "DO NOT pick tweets that are:\n"
                "- Personal/emotional (condolences, celebrations)\n"
                "- Controversial or political\n"
                "- Simple announcements with nothing to add\n\n"
                "Return ONLY the index number (e.g. 0, 1, 2). If none are suitable, return NONE."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Target: @{username}\n"
                    f"Job we're applying to: {posting.get('title', '')}\n\n"
                    f"Recent tweets:\n{candidates_text}"
                ),
            }],
        )

        pick = response.content[0].text.strip()

        if pick.upper() == "NONE":
            return None

        try:
            idx = int(pick)
            if 0 <= idx < len(candidates):
                return {"id": candidates[idx]["id"], "text": candidates[idx]["text"]}
        except ValueError:
            pass

        return None

    except Exception as exc:
        logger.warning("find_reply_tweet_failed", username=username, error=str(exc))
        return None


async def _draft_outreach_reply(
    tweet_text: str,
    tweet_author: str,
    company: str,
    posting_title: str,
) -> str:
    """Draft a genuine, value-adding reply to the target's tweet.

    The reply should NOT mention the job application. It should add real
    value to the conversation — a sharp insight, relevant data point,
    or thoughtful question. The goal is to be noticed as someone
    worth paying attention to, not to pitch.

    Args:
        tweet_text: The tweet we're replying to.
        tweet_author: The author's username.
        company: Company name.
        posting_title: Job title for context (NOT to mention in the reply).

    Returns:
        Reply text (max 280 characters).
    """
    response = await _anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=(
            "You are astrlboy (@astrlboy_), an autonomous AI agent replying to a tweet.\n\n"
            "CRITICAL RULES:\n"
            "- DO NOT mention you're applying for a job\n"
            "- DO NOT pitch yourself or your services\n"
            "- DO NOT say 'as an AI agent' or similar\n"
            "- DO NOT be generic ('great point!', 'love this', 'so true')\n\n"
            "INSTEAD:\n"
            "- Add a sharp, specific insight related to what they said\n"
            "- Share a relevant perspective or data point\n"
            "- Ask a genuinely smart follow-up question\n"
            "- Be concise and direct — like a knowledgeable peer, not a fan\n\n"
            "The reply MUST be under 280 characters.\n"
            "Write ONLY the reply text, nothing else."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Tweet by @{tweet_author} (works at {company}):\n"
                f"\"{tweet_text}\"\n\n"
                f"Context: They're hiring for '{posting_title}'. "
                f"We want to get on their radar by adding genuine value to this conversation. "
                f"Draft the reply."
            ),
        }],
    )

    reply = response.content[0].text.strip()
    # Strip any quotes Claude might wrap it in
    if reply.startswith('"') and reply.endswith('"'):
        reply = reply[1:-1]
    # Enforce character limit
    return reply[:280]


async def draft_application(state: ApplicationState) -> ApplicationState:
    """Draft and send applications for each selected posting.

    Uses the apply_to_url skill when available — it handles scraping,
    fit scoring, drafting, and sending/escalating in one call.
    Falls back to manual Claude drafting otherwise.

    If warm_outreach already engaged with the target on X, that context
    is included so the cover note can reference it naturally.
    """
    selected = state.get("selected", [])
    outreach_results = state.get("outreach_results", [])
    sent = 0

    # Build a lookup: posting title → outreach result for quick access
    outreach_by_title: dict[str, dict] = {}
    for o in outreach_results:
        if o.get("status") == "engaged":
            outreach_by_title[o.get("posting_title", "")] = o

    # Prefer the dedicated apply_to_url skill — handles the full pipeline
    use_apply_skill = await skill_registry.is_available("apply_to_url")

    for posting in selected:
        outreach = outreach_by_title.get(posting.get("title", ""))

        try:
            if use_apply_skill and posting.get("url"):
                apply_skill = await skill_registry.get("apply_to_url")
                result = await apply_skill.execute(url=posting["url"])
                if result.get("status") == "sent":
                    sent += 1

                # Log outreach context alongside the application for R2 training data
                if outreach:
                    try:
                        await r2_client.dump(
                            contract_slug="astrlboy",
                            entity_type="outreach",
                            entity_id=uuid4(),
                            data={
                                "posting_url": posting.get("url", ""),
                                "role": result.get("role", posting["title"]),
                                "target_username": outreach.get("target_username"),
                                "tweet_replied_to": outreach.get("tweet_replied_to"),
                                "reply_text": outreach.get("reply_text"),
                                "reply_tweet_id": outreach.get("reply_tweet_id"),
                                "application_status": result.get("status"),
                            },
                        )
                    except Exception:
                        pass

                logger.info(
                    "application_processed",
                    role=result.get("role", posting["title"]),
                    status=result.get("status", "unknown"),
                    had_warm_outreach=bool(outreach),
                )
            else:
                # Fallback: draft with Claude, save to DB
                # Include outreach context so the cover note can reference it
                outreach_context = ""
                if outreach:
                    outreach_context = (
                        f"\n\nNOTE: We already engaged with @{outreach['target_username']} "
                        f"on X — replied to their tweet about: \"{outreach.get('tweet_text', '')[:150]}\". "
                        f"You can reference this naturally in the cover note if relevant, "
                        f"e.g. 'I recently engaged with your thoughts on [topic]'. "
                        f"Do NOT make it the focus — keep it as a brief, natural mention."
                    )

                response = await _anthropic.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=500,
                    system=(
                        "You are astrlboy writing a job application from agent@astrlboy.xyz.\n"
                        "Write a short, sharp cover note (3-4 paragraphs max).\n"
                        "Highlight: autonomous operation, multi-client management, content + community expertise.\n"
                        "Do not sound like an AI. Be direct and specific."
                    ),
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Job: {posting['title']}\n{posting.get('description', '')}"
                            f"{outreach_context}"
                        ),
                    }],
                )

                cover_note = response.content[0].text
                app_id = uuid4()
                async with async_session_factory() as session:
                    application = JobApplication(
                        id=app_id,
                        role=posting["title"],
                        company=posting.get("url", "").split("/")[2] if "/" in posting.get("url", "") else "Unknown",
                        posting_url=posting.get("url", ""),
                        cover_note=cover_note,
                        status="drafted",
                    )
                    session.add(application)
                    await session.commit()

                logger.info(
                    "application_drafted",
                    role=posting["title"],
                    app_id=str(app_id),
                    had_warm_outreach=bool(outreach),
                )

                # Dump to R2
                try:
                    await r2_client.dump(
                        contract_slug="astrlboy",
                        entity_type="job_applications",
                        entity_id=app_id,
                        data={
                            "role": posting["title"],
                            "posting_url": posting.get("url", ""),
                            "cover_note": cover_note,
                            "model": "claude-sonnet-4-6",
                            "warm_outreach": outreach,
                        },
                    )
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("application_failed", role=posting.get("title", ""), error=str(exc))

    return {**state, "sent_count": sent, "status": "complete"}
