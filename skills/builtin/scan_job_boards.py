"""
Job board scanning skill.

Searches for relevant job postings across multiple sources using Tavily
(domain-scoped) and Serper (broad coverage). Results are scored for relevance
by Claude and deduped against existing applications in the database.

Use this to feed the job applications graph with fresh postings.
The apply_to_url skill handles the actual application.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from anthropic import AsyncAnthropic
from sqlalchemy import select
from tavily import TavilyClient

import httpx

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool
from db.base import async_session_factory
from db.models.job_applications import JobApplication
from storage.r2 import r2_client

logger = get_logger("skills.scan_job_boards")

SERPER_API_URL = "https://google.serper.dev/search"

# Minimum relevance score to include a posting in results
_MIN_RELEVANCE_SCORE = 6


class ScanJobBoardsSkill(BaseTool):
    """Search job boards for relevant postings using Tavily + Serper."""

    name = "scan_job_boards"
    description = (
        "Search for relevant job postings across multiple sources. "
        "Combines Tavily domain-scoped search with Serper for broad coverage, "
        "scores relevance with Claude, and deduplicates against existing applications."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        self._tavily = TavilyClient(api_key=settings.tavily_api_key)
        self._anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def execute(
        self,
        keywords: list[str],
        sources: list[str] | None = None,
        posted_within_days: int = 3,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Search for job postings and return scored, deduplicated results.

        Args:
            keywords: Search terms (e.g. ["AI agent", "growth agent"]).
            sources: Domains to search (e.g. ["linkedin", "wellfound"]).
                     Defaults to LinkedIn, Wellfound, Remote.co, WeWorkRemotely.
            posted_within_days: Only include postings from this many days ago.

        Returns:
            List of dicts with url, role, company, snippet, source, relevance_score.
            Only includes results scoring >= 6 and not already in job_applications.

        Raises:
            SkillExecutionError: If both Tavily and Serper fail.
        """
        if sources is None:
            sources = ["linkedin.com", "wellfound.com", "remote.co", "weworkremotely.com"]

        # Normalize source domains — allow shorthand like "linkedin" → "linkedin.com"
        normalized_sources = []
        for s in sources:
            if "." not in s:
                s = f"{s}.com"
            normalized_sources.append(s)

        raw_results: list[dict[str, Any]] = []

        # Search Tavily with include_domains for targeted coverage
        tavily_results = await self._search_tavily(keywords, normalized_sources, posted_within_days)
        raw_results.extend(tavily_results)

        # Search Serper for broader coverage beyond specified domains
        serper_results = await self._search_serper(keywords, posted_within_days)
        raw_results.extend(serper_results)

        if not raw_results:
            logger.warning("no_raw_results", keywords=keywords)
            return []

        # Deduplicate by URL before scoring — no point scoring the same posting twice
        seen_urls: set[str] = set()
        unique_results: list[dict[str, Any]] = []
        for result in raw_results:
            url = result.get("url", "").rstrip("/")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_results.append(result)

        # Score relevance with Claude
        scored = await self._score_results(unique_results, keywords)

        # Filter by minimum score
        filtered = [r for r in scored if r.get("relevance_score", 0) >= _MIN_RELEVANCE_SCORE]

        # Dedup against existing job applications in the database
        final = await self._dedup_against_db(filtered)

        # Dump raw I/O to R2 for training data
        dump_id = uuid.uuid4()
        await r2_client.dump(
            contract_slug="astrlboy",
            entity_type="job_scan",
            entity_id=dump_id,
            data={
                "model": "claude-haiku-4-5",
                "keywords": keywords,
                "sources": normalized_sources,
                "posted_within_days": posted_within_days,
                "raw_result_count": len(raw_results),
                "unique_result_count": len(unique_results),
                "scored_result_count": len(scored),
                "filtered_result_count": len(filtered),
                "final_result_count": len(final),
                "results": final,
            },
        )

        logger.info(
            "scan_complete",
            keywords=keywords,
            raw_count=len(raw_results),
            final_count=len(final),
        )
        return final

    async def _search_tavily(
        self,
        keywords: list[str],
        domains: list[str],
        days: int,
    ) -> list[dict[str, Any]]:
        """Search Tavily with domain filtering for each keyword.

        Args:
            keywords: Search terms.
            domains: Domains to restrict search to.
            days: Recency filter in days.

        Returns:
            Normalized list of result dicts.
        """
        results: list[dict[str, Any]] = []
        for kw in keywords:
            query = f"{kw} job posting"
            try:
                response = self._tavily.search(
                    query=query,
                    max_results=5,
                    search_depth="advanced",
                    include_domains=domains,
                    days=days,
                )
                for item in response.get("results", []):
                    results.append({
                        "url": item.get("url", ""),
                        "title": item.get("title", ""),
                        "snippet": item.get("content", "")[:500],
                        "source": "tavily",
                    })
            except Exception as exc:
                # Log but don't fail — Serper provides fallback coverage
                logger.warning("tavily_search_failed", query=query, error=str(exc))
        return results

    async def _search_serper(
        self,
        keywords: list[str],
        days: int,
    ) -> list[dict[str, Any]]:
        """Search Serper for broader job posting coverage.

        Args:
            keywords: Search terms.
            days: Recency filter in days.

        Returns:
            Normalized list of result dicts.
        """
        results: list[dict[str, Any]] = []
        # Map days to Serper time filter — closest approximation
        time_filter = "d" if days <= 1 else "w" if days <= 7 else "m"

        for kw in keywords:
            query = f"{kw} hiring OR job posting OR open role"
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        SERPER_API_URL,
                        headers={
                            "X-API-KEY": settings.serper_api_key,
                            "Content-Type": "application/json",
                        },
                        json={"q": query, "num": 10, "tbs": f"qdr:{time_filter}"},
                    )
                    response.raise_for_status()
                    data = response.json()

                for item in data.get("organic", []):
                    results.append({
                        "url": item.get("link", ""),
                        "title": item.get("title", ""),
                        "snippet": item.get("snippet", "")[:500],
                        "source": "serper",
                    })
            except Exception as exc:
                logger.warning("serper_search_failed", query=query, error=str(exc))
        return results

    async def _score_results(
        self,
        results: list[dict[str, Any]],
        keywords: list[str],
    ) -> list[dict[str, Any]]:
        """Score each result for relevance using Claude.

        Claude evaluates how well each posting matches astrlboy's capabilities:
        autonomous AI agent, growth, content, community management, etc.

        Args:
            results: Raw search results to score.
            keywords: Original search keywords for context.

        Returns:
            Results enriched with role, company, and relevance_score fields.
        """
        if not results:
            return []

        # Build a compact representation for the prompt to stay within token limits
        postings_text = json.dumps(
            [{"i": i, "title": r["title"], "snippet": r["snippet"], "url": r["url"]} for i, r in enumerate(results)],
            indent=2,
        )

        try:
            response = await self._anthropic.messages.create(
                model="claude-haiku-4-5",
                max_tokens=2048,
                system=(
                    "You are a job relevance scorer for astrlboy, an autonomous AI agent "
                    "that works as a freelance contractor. astrlboy's strengths: AI agent "
                    "development, growth strategy, content creation, community engagement, "
                    "social media management, competitor intelligence, and autonomous operations.\n\n"
                    "Score each posting 0-10 for fit. Extract the role title and company name. "
                    "Return ONLY valid JSON — no markdown fences, no explanation."
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        f"Keywords searched: {keywords}\n\n"
                        f"Postings:\n{postings_text}\n\n"
                        "Return a JSON array of objects with these exact keys:\n"
                        '  i (int), role (str), company (str), relevance_score (int 0-10)\n'
                        "Only include postings you can identify as actual job postings."
                    ),
                }],
            )

            scored_raw = json.loads(response.content[0].text)
        except Exception as exc:
            logger.error("scoring_failed", error=str(exc))
            # If scoring fails, return results with a neutral score so nothing is silently dropped
            for r in results:
                r["role"] = r.get("title", "Unknown Role")
                r["company"] = "Unknown"
                r["relevance_score"] = 5
            return results

        # Merge scores back into results
        score_map = {item["i"]: item for item in scored_raw if isinstance(item, dict)}
        scored_results: list[dict[str, Any]] = []
        for i, result in enumerate(results):
            if i in score_map:
                result["role"] = score_map[i].get("role", result.get("title", "Unknown Role"))
                result["company"] = score_map[i].get("company", "Unknown")
                result["relevance_score"] = score_map[i].get("relevance_score", 0)
                scored_results.append(result)

        return scored_results

    async def _dedup_against_db(
        self,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Remove results whose URLs already exist in job_applications.

        Prevents astrlboy from applying to the same posting twice.

        Args:
            results: Scored results to filter.

        Returns:
            Results not already tracked in the database.
        """
        if not results:
            return []

        urls = [r["url"].rstrip("/") for r in results]

        try:
            async with async_session_factory() as session:
                stmt = select(JobApplication.posting_url).where(
                    JobApplication.posting_url.in_(urls)
                )
                db_result = await session.execute(stmt)
                existing_urls = {row[0].rstrip("/") for row in db_result.fetchall() if row[0]}
        except Exception as exc:
            # If DB check fails, log and return all results — better to risk a dup
            # than silently drop valid postings
            logger.warning("dedup_db_check_failed", error=str(exc))
            return results

        deduped = [r for r in results if r["url"].rstrip("/") not in existing_urls]

        if len(results) != len(deduped):
            logger.info(
                "dedup_filtered",
                before=len(results),
                after=len(deduped),
                removed=len(results) - len(deduped),
            )

        return deduped

    def get_schema(self) -> dict:
        """Return JSON schema for scan_job_boards inputs."""
        return {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Search terms (e.g. ['AI agent', 'growth agent'])",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Domains to search (e.g. ['linkedin.com', 'wellfound.com'])",
                },
                "posted_within_days": {
                    "type": "integer",
                    "default": 3,
                    "description": "Only include postings from this many days ago",
                },
            },
            "required": ["keywords"],
        }
