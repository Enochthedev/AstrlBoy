"""
OSINT lookup skill — open-source intelligence gathering.

Finds contact emails, decision-makers, social profiles, and company context
from a company name or URL. Combines Serper/Tavily search with page scraping
to build a target profile. Used by the job application flow and engagement
strategy to make smarter outreach decisions.
"""

from typing import Any

from anthropic import AsyncAnthropic
from tavily import TavilyClient

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool

logger = get_logger("skills.osint_lookup")

_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)


class OsintLookupSkill(BaseTool):
    """Gather intelligence on a company or person for outreach."""

    name = "osint_lookup"
    description = (
        "OSINT lookup — find contact emails, social handles, decision-makers, "
        "and company context from a name or URL. Use before applying to jobs "
        "or reaching out to potential clients."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        self._tavily = TavilyClient(api_key=settings.tavily_api_key)

    async def execute(
        self,
        target: str,
        lookup_type: str = "company",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run an OSINT lookup on a target.

        Args:
            target: Company name, URL, or person name to research.
            lookup_type: 'company' or 'person'.

        Returns:
            Dict with emails, socials, decision_makers, context, and outreach_angle.

        Raises:
            SkillExecutionError: If the lookup fails.
        """
        try:
            # Build targeted search queries
            queries = self._build_queries(target, lookup_type)

            # Run searches
            all_results = []
            for query in queries:
                try:
                    results = self._tavily.search(
                        query=query, max_results=3, search_depth="basic"
                    )
                    all_results.extend(results.get("results", []))
                except Exception:
                    continue

            if not all_results:
                return {"error": "No results found", "target": target}

            # Synthesize with Claude
            search_context = "\n\n".join(
                f"Source: {r.get('url', '')}\nTitle: {r.get('title', '')}\n{r.get('content', '')[:500]}"
                for r in all_results[:10]
            )

            prompt = self._build_synthesis_prompt(target, lookup_type, search_context)

            response = await _anthropic.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
            )

            synthesis = response.content[0].text

            logger.info("osint_lookup_complete", target=target, type=lookup_type)

            return {
                "target": target,
                "type": lookup_type,
                "synthesis": synthesis,
                "sources_checked": len(all_results),
            }

        except Exception as exc:
            logger.error("osint_lookup_failed", target=target, error=str(exc))
            raise SkillExecutionError(f"OSINT lookup failed for {target}: {exc}") from exc

    def _build_queries(self, target: str, lookup_type: str) -> list[str]:
        """Build search queries based on target and type."""
        if lookup_type == "person":
            return [
                f"{target} LinkedIn",
                f"{target} Twitter X",
                f"{target} email contact",
            ]
        else:
            return [
                f"{target} contact email",
                f"{target} team founders",
                f"{target} Twitter X social",
                f"site:{target} careers jobs" if "." in target else f"{target} hiring",
            ]

    def _build_synthesis_prompt(
        self, target: str, lookup_type: str, context: str
    ) -> str:
        """Build the Claude prompt for synthesizing OSINT results."""
        return (
            f"Extract intelligence on {target} from these search results.\n\n"
            f"{context}\n\n"
            "Return a concise report with:\n"
            "- Contact emails found (or best guess at format like name@company.com)\n"
            "- Social handles (Twitter/X, LinkedIn)\n"
            "- Key people (founders, hiring managers, relevant decision-makers)\n"
            "- What the company does (one line)\n"
            "- Best outreach angle for an AI agent applying to work with them\n\n"
            "Be factual. If you can't find something, say so. No guessing emails."
        )

    def get_schema(self) -> dict:
        """Return JSON schema for osint_lookup inputs."""
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Company name, URL, or person name to research",
                },
                "lookup_type": {
                    "type": "string",
                    "enum": ["company", "person"],
                    "description": "Type of lookup",
                    "default": "company",
                },
            },
            "required": ["target"],
        }
