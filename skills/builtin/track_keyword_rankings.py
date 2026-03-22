"""
Track keyword rankings skill.

Tracks how target keywords rank in Google search results over time.
Uses Serper to fetch SERP data and extracts position, URL, and title for each
keyword. Designed to be run on a schedule so the agent can detect ranking changes
and alert the operator when a client gains or loses visibility.
"""

from typing import Any

import httpx

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.track_keyword_rankings")

SERPER_API_URL = "https://google.serper.dev/search"


class TrackKeywordRankingsSkill(BaseTool):
    """Track keyword positions in Google search results via Serper."""

    name = "track_keyword_rankings"
    description = (
        "Track how target keywords rank in Google. Returns position, URL, "
        "and title for each keyword. Use on a schedule to detect ranking "
        "changes over time."
    )
    version = "1.0.0"

    async def execute(
        self,
        keywords: list[str],
        contract_slug: str,
        gl: str = "us",
        num_results: int = 20,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch Google ranking data for each keyword.

        Args:
            keywords: Keywords to track (e.g. ["tokenized mentorship", "onchain tutoring"]).
            contract_slug: Client slug — included in output for downstream storage.
            gl: Country code for localized results.
            num_results: How deep to scan per keyword (default 20).

        Returns:
            List of dicts with keyword, position, url, title, contract_slug.

        Raises:
            SkillExecutionError: If the Serper API call fails.
        """
        try:
            rankings: list[dict[str, Any]] = []
            headers = {
                "X-API-KEY": settings.serper_api_key,
                "Content-Type": "application/json",
            }

            async with httpx.AsyncClient() as client:
                for keyword in keywords:
                    try:
                        response = await client.post(
                            SERPER_API_URL,
                            headers=headers,
                            json={"q": keyword, "num": num_results, "gl": gl},
                        )
                        response.raise_for_status()
                        data = response.json()

                        organic = data.get("organic", [])
                        if organic:
                            # Record each result's position for this keyword
                            for idx, result in enumerate(organic, start=1):
                                rankings.append({
                                    "keyword": keyword,
                                    "position": idx,
                                    "url": result.get("link", ""),
                                    "title": result.get("title", ""),
                                    "contract_slug": contract_slug,
                                })
                        else:
                            # No organic results — still record the keyword as unranked
                            rankings.append({
                                "keyword": keyword,
                                "position": None,
                                "url": None,
                                "title": None,
                                "contract_slug": contract_slug,
                            })

                    except Exception as exc:
                        # Log per-keyword failures but keep going
                        logger.warning(
                            "keyword_ranking_failed",
                            keyword=keyword,
                            error=str(exc),
                        )
                        rankings.append({
                            "keyword": keyword,
                            "position": None,
                            "url": None,
                            "title": None,
                            "contract_slug": contract_slug,
                            "error": str(exc),
                        })

            logger.info(
                "keyword_rankings_tracked",
                contract_slug=contract_slug,
                keywords_count=len(keywords),
                results_count=len(rankings),
            )
            return rankings

        except SkillExecutionError:
            raise
        except Exception as exc:
            logger.error(
                "track_keyword_rankings_failed",
                contract_slug=contract_slug,
                error=str(exc),
            )
            raise SkillExecutionError(
                f"Track keyword rankings failed: {exc}"
            ) from exc

    def get_schema(self) -> dict:
        """Return JSON schema for track_keyword_rankings inputs."""
        return {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keywords to track in Google rankings",
                },
                "contract_slug": {
                    "type": "string",
                    "description": "Client slug for associating results",
                },
                "gl": {
                    "type": "string",
                    "default": "us",
                    "description": "Country code for localized results",
                },
                "num_results": {
                    "type": "integer",
                    "default": 20,
                    "description": "How deep to scan per keyword",
                },
            },
            "required": ["keywords", "contract_slug"],
        }
