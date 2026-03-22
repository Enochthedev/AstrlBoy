"""
Competitor monitoring skill.

Scrapes a competitor URL and uses Claude to analyze meaningful changes since the
last check. Stores snapshots to R2 so diffs can be computed across runs. Use this
for weekly competitor intelligence sweeps rather than one-off scraping.
"""

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from anthropic import AsyncAnthropic
from firecrawl import FirecrawlApp

from core.config import settings
from core.exceptions import SkillExecutionError
from core.logging import get_logger
from skills.base import BaseTool
from storage.r2 import r2_client

logger = get_logger("skills.monitor_competitor")

# Claude client for semantic diff analysis
_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)


class MonitorCompetitorSkill(BaseTool):
    """Scrape a competitor and extract meaningful changes since last check.

    Combines Firecrawl scraping with Claude diff analysis to surface pricing
    changes, new features, new blog posts, and hiring signals. Each snapshot
    is stored to R2 so the next run can compare against it.
    """

    name = "monitor_competitor"
    description = (
        "Scrape a competitor site and analyze what changed semantically since "
        "the last snapshot. Use for scheduled competitor intelligence sweeps."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        self._firecrawl = FirecrawlApp(api_key=settings.firecrawl_api_key)

    async def execute(
        self,
        competitor_url: str,
        contract_slug: str,
        sections_to_watch: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Scrape a competitor and detect changes.

        Args:
            competitor_url: Root URL of the competitor to monitor.
            contract_slug: Client slug — used for R2 key and previous snapshot lookup.
            sections_to_watch: Page sections to focus on. Defaults to
                ['pricing', 'features', 'blog', 'jobs'].

        Returns:
            Dict with changes_detected, changes list, new_content list,
            and snapshot_r2_key.

        Raises:
            SkillExecutionError: If scraping or analysis fails.
        """
        if sections_to_watch is None:
            sections_to_watch = ["pricing", "features", "blog", "jobs"]

        # --- 1. Scrape the competitor ---
        try:
            scrape_result = self._firecrawl.scrape_url(
                competitor_url,
                params={"formats": ["markdown"]},
            )
            current_markdown = scrape_result.get("markdown", "")
        except Exception as exc:
            logger.error(
                "competitor_scrape_failed",
                url=competitor_url,
                contract_slug=contract_slug,
                error=str(exc),
            )
            raise SkillExecutionError(
                f"Failed to scrape competitor {competitor_url}: {exc}"
            ) from exc

        if not current_markdown:
            raise SkillExecutionError(
                f"No content returned from {competitor_url}"
            )

        # --- 2. Load previous snapshot from R2 (best-effort) ---
        previous_markdown = await self._load_previous_snapshot(
            competitor_url, contract_slug
        )

        # --- 3. Use Claude to analyze changes ---
        try:
            analysis = await self._analyze_changes(
                competitor_url=competitor_url,
                current_markdown=current_markdown,
                previous_markdown=previous_markdown,
                sections_to_watch=sections_to_watch,
            )
        except Exception as exc:
            logger.error(
                "competitor_analysis_failed",
                url=competitor_url,
                contract_slug=contract_slug,
                error=str(exc),
            )
            raise SkillExecutionError(
                f"Claude analysis failed for {competitor_url}: {exc}"
            ) from exc

        # --- 4. Store current snapshot to R2 ---
        snapshot_id = uuid4()
        try:
            snapshot_r2_key = await r2_client.dump(
                contract_slug=contract_slug,
                entity_type="competitor_snapshots",
                entity_id=snapshot_id,
                data={
                    "competitor_url": competitor_url,
                    "markdown": current_markdown,
                    "sections_to_watch": sections_to_watch,
                    "analysis": analysis,
                    "model": "claude-sonnet-4-5",
                },
            )
        except Exception as exc:
            # Non-fatal — we still return the analysis even if R2 fails
            logger.warning(
                "competitor_snapshot_r2_failed",
                url=competitor_url,
                error=str(exc),
            )
            snapshot_r2_key = ""

        logger.info(
            "competitor_monitored",
            url=competitor_url,
            contract_slug=contract_slug,
            changes_detected=analysis.get("changes_detected", False),
            change_count=len(analysis.get("changes", [])),
        )

        return {
            "changes_detected": analysis.get("changes_detected", False),
            "changes": analysis.get("changes", []),
            "new_content": analysis.get("new_content", []),
            "snapshot_r2_key": snapshot_r2_key,
        }

    async def _load_previous_snapshot(
        self, competitor_url: str, contract_slug: str
    ) -> str | None:
        """Try to load the most recent snapshot for this competitor from R2.

        Returns None if no previous snapshot exists. Failures are logged but
        never raised — a missing baseline just means we treat the first scrape
        as the baseline.

        Args:
            competitor_url: The competitor URL to find prior snapshots for.
            contract_slug: Client slug for R2 key prefix.

        Returns:
            Previous markdown content, or None.
        """
        # R2 doesn't support listing by prefix natively through our client,
        # so we rely on the caller or graph layer to pass previous snapshot
        # keys in future iterations. For now, return None (first-run baseline).
        return None

    async def _analyze_changes(
        self,
        competitor_url: str,
        current_markdown: str,
        previous_markdown: str | None,
        sections_to_watch: list[str],
    ) -> dict[str, Any]:
        """Use Claude to semantically diff current vs previous content.

        Args:
            competitor_url: The competitor URL being analyzed.
            current_markdown: Current scraped page content.
            previous_markdown: Previous scraped content, or None if first run.
            sections_to_watch: Sections to focus the analysis on.

        Returns:
            Dict with changes_detected, changes, and new_content keys.
        """
        sections_str = ", ".join(sections_to_watch)

        if previous_markdown:
            prompt = (
                f"You are a competitive intelligence analyst. Compare these two "
                f"snapshots of {competitor_url} and identify meaningful changes.\n\n"
                f"Focus on these sections: {sections_str}\n\n"
                f"PREVIOUS SNAPSHOT:\n{previous_markdown[:8000]}\n\n"
                f"CURRENT SNAPSHOT:\n{current_markdown[:8000]}\n\n"
                f"Respond with JSON only (no markdown fences):\n"
                f'{{\n'
                f'  "changes_detected": true/false,\n'
                f'  "changes": [\n'
                f'    {{"section": "...", "what_changed": "...", "significance": "low|medium|high"}}\n'
                f'  ],\n'
                f'  "new_content": [\n'
                f'    {{"url": "...", "title": "...", "summary": "..."}}\n'
                f'  ]\n'
                f'}}'
            )
        else:
            prompt = (
                f"You are a competitive intelligence analyst. This is the first "
                f"snapshot of {competitor_url}. Analyze the page and identify "
                f"noteworthy elements that we should track going forward.\n\n"
                f"Focus on these sections: {sections_str}\n\n"
                f"CURRENT SNAPSHOT:\n{current_markdown[:12000]}\n\n"
                f"Respond with JSON only (no markdown fences):\n"
                f'{{\n'
                f'  "changes_detected": false,\n'
                f'  "changes": [],\n'
                f'  "new_content": [\n'
                f'    {{"url": "...", "title": "...", "summary": "..."}}\n'
                f'  ]\n'
                f'}}'
            )

        response = await _anthropic.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text.strip()

        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning(
                "competitor_analysis_parse_failed",
                url=competitor_url,
                raw_text=raw_text[:500],
            )
            # Return safe fallback rather than crashing the pipeline
            return {
                "changes_detected": False,
                "changes": [],
                "new_content": [],
            }

    def get_schema(self) -> dict:
        """Return JSON schema for monitor_competitor inputs."""
        return {
            "type": "object",
            "properties": {
                "competitor_url": {
                    "type": "string",
                    "description": "Root URL of the competitor to monitor",
                },
                "contract_slug": {
                    "type": "string",
                    "description": "Client slug for context and R2 storage",
                },
                "sections_to_watch": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["pricing", "features", "blog", "jobs"],
                    "description": "Page sections to focus the analysis on",
                },
            },
            "required": ["competitor_url", "contract_slug"],
        }
