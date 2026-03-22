"""
Job application skill.

Scrapes a job posting, extracts structured info with Claude, scores fit,
drafts a tailored cover note, and either sends via email or escalates to
Wave via Telegram depending on the application method.

Use this after scan_job_boards has identified a relevant posting.
This skill handles the full apply-or-escalate lifecycle for a single URL.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from anthropic import AsyncAnthropic

from core.config import settings
from core.constants import ApplicationStatus
from core.exceptions import EscalationRequired, SkillExecutionError
from core.logging import get_logger
from db.base import async_session_factory
from db.models.job_applications import JobApplication
from skills.base import BaseTool
from skills.registry import skill_registry
from storage.r2 import r2_client

logger = get_logger("skills.apply_to_url")

# Minimum fit score to proceed with an application
_MIN_FIT_SCORE = 7


class ApplyToUrlSkill(BaseTool):
    """Scrape a job posting, draft an application, and send or escalate."""

    name = "apply_to_url"
    description = (
        "Scrape a job posting URL, extract role details, score fit, "
        "draft a tailored cover note, and send via email or escalate "
        "to Wave if human action is required."
    )
    version = "1.0.0"

    def __init__(self) -> None:
        self._anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def execute(
        self,
        url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Scrape a posting, draft application, and send or escalate.

        Args:
            url: The job posting URL to process.

        Returns:
            Dict with role, company, application_method, status,
            cover_note, and job_application_id.

        Raises:
            SkillExecutionError: If scraping or drafting fails.
        """
        entity_id = uuid.uuid4()
        raw_io: dict[str, Any] = {"url": url, "steps": []}

        # Step 1: Scrape the job posting using the scrape skill
        posting_markdown = await self._scrape_posting(url)
        raw_io["steps"].append({"step": "scrape", "output_length": len(posting_markdown)})

        # Step 2: Extract structured info with Claude
        extracted = await self._extract_posting_info(posting_markdown, url)
        raw_io["steps"].append({"step": "extract", "output": extracted})

        role = extracted.get("role", "Unknown Role")
        company = extracted.get("company", "Unknown Company")
        application_method = extracted.get("application_method", "unknown")
        contact_email = extracted.get("contact_email", "")
        fit_score = extracted.get("fit_score", 0)
        requirements = extracted.get("requirements", "")

        # Step 3: Check fit score — don't waste effort on poor-fit roles
        if fit_score < _MIN_FIT_SCORE:
            result = {
                "role": role,
                "company": company,
                "application_method": application_method,
                "status": "not_applicable",
                "cover_note": "",
                "job_application_id": "",
                "fit_score": fit_score,
            }
            raw_io["steps"].append({"step": "fit_filter", "score": fit_score, "threshold": _MIN_FIT_SCORE})
            raw_io["result"] = result

            await r2_client.dump(
                contract_slug="astrlboy",
                entity_type="job_applications",
                entity_id=entity_id,
                data=raw_io,
            )

            logger.info(
                "application_skipped_low_fit",
                url=url,
                role=role,
                company=company,
                fit_score=fit_score,
            )
            return result

        # Step 4: Draft a cover note
        cover_note = await self._draft_cover_note(role, company, requirements, posting_markdown)
        raw_io["steps"].append({"step": "draft", "cover_note": cover_note})

        # Step 5: Route based on application method
        status: str
        if application_method == "email" and contact_email:
            status = await self._send_email_application(contact_email, role, company, cover_note)
        elif application_method in ("form", "portal"):
            status = await self._escalate_for_human(url, role, company, cover_note, application_method)
        else:
            # Unknown method — escalate so Wave can decide
            status = await self._escalate_for_human(url, role, company, cover_note, "unknown")

        raw_io["steps"].append({"step": "route", "method": application_method, "status": status})

        # Step 6: Save to job_applications table
        app_id = await self._save_application(
            role=role,
            company=company,
            posting_url=url,
            email_sent_to=contact_email if application_method == "email" else None,
            cover_note=cover_note,
            status=status,
            r2_key="",  # will be updated after R2 dump
        )
        raw_io["steps"].append({"step": "save", "job_application_id": str(app_id)})

        # Step 7: Dump raw I/O to R2 and update the application record with the key
        r2_key = await r2_client.dump(
            contract_slug="astrlboy",
            entity_type="job_applications",
            entity_id=app_id,
            data={
                "model": "claude-sonnet-4-5",
                "prompt_chain": raw_io["steps"],
                "raw_posting_markdown": posting_markdown[:5000],
                "extracted": extracted,
                "cover_note": cover_note,
                "result_status": status,
            },
        )

        await self._update_r2_key(app_id, r2_key)

        result = {
            "role": role,
            "company": company,
            "application_method": application_method,
            "status": status,
            "cover_note": cover_note,
            "job_application_id": str(app_id),
        }
        raw_io["result"] = result

        logger.info(
            "application_processed",
            url=url,
            role=role,
            company=company,
            method=application_method,
            status=status,
            job_application_id=str(app_id),
        )
        return result

    async def _scrape_posting(self, url: str) -> str:
        """Scrape the job posting URL using the registered scrape skill.

        Args:
            url: Job posting URL.

        Returns:
            Markdown content of the posting.

        Raises:
            SkillExecutionError: If scraping fails.
        """
        try:
            scrape_skill = await skill_registry.get("scrape")
            return await scrape_skill.execute(url=url)
        except Exception as exc:
            logger.error("posting_scrape_failed", url=url, error=str(exc))
            raise SkillExecutionError(f"Failed to scrape job posting at {url}: {exc}") from exc

    async def _extract_posting_info(self, markdown: str, url: str) -> dict[str, Any]:
        """Extract structured job info and score fit using Claude.

        Claude identifies the role, company, requirements, application method,
        contact email, and scores how well astrlboy fits the role.

        Args:
            markdown: Scraped posting content.
            url: Original posting URL for context.

        Returns:
            Dict with role, company, requirements, application_method,
            contact_email, and fit_score.

        Raises:
            SkillExecutionError: If extraction fails.
        """
        try:
            response = await self._anthropic.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1500,
                system=(
                    "You are analyzing a job posting for astrlboy, an autonomous AI agent "
                    "that works as a freelance contractor. astrlboy's capabilities:\n"
                    "- Autonomous AI agent operations (always-on, scheduled tasks)\n"
                    "- Growth strategy and execution\n"
                    "- Content creation (social media, articles, thought leadership)\n"
                    "- Community engagement across X, LinkedIn, Reddit, Discord\n"
                    "- Competitor intelligence and market monitoring\n"
                    "- Email outreach and communication\n"
                    "- Data collection and trend analysis\n\n"
                    "Extract structured info from the posting and score fit 0-10.\n"
                    "Return ONLY valid JSON — no markdown fences, no explanation."
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        f"URL: {url}\n\n"
                        f"Posting content:\n{markdown[:8000]}\n\n"
                        "Return JSON with these exact keys:\n"
                        "- role (str): job title\n"
                        "- company (str): company name\n"
                        "- requirements (str): key requirements, max 500 chars\n"
                        "- application_method (str): one of 'email', 'form', 'portal', 'unknown'\n"
                        "- contact_email (str): email to apply to, or empty string if none found\n"
                        "- fit_score (int): 0-10 how well astrlboy fits this role"
                    ),
                }],
            )

            return json.loads(response.content[0].text)
        except json.JSONDecodeError as exc:
            logger.error("extract_json_parse_failed", url=url, error=str(exc))
            raise SkillExecutionError(f"Failed to parse extraction response for {url}: {exc}") from exc
        except Exception as exc:
            logger.error("extract_failed", url=url, error=str(exc))
            raise SkillExecutionError(f"Failed to extract posting info from {url}: {exc}") from exc

    async def _draft_cover_note(
        self,
        role: str,
        company: str,
        requirements: str,
        posting_markdown: str,
    ) -> str:
        """Draft a tailored cover note using Claude.

        The note is written from agent@astrlboy.xyz and presents astrlboy
        as an autonomous AI agent offering contract services.

        Args:
            role: Job title.
            company: Company name.
            requirements: Key requirements from the posting.
            posting_markdown: Full posting content for context.

        Returns:
            The drafted cover note text.

        Raises:
            SkillExecutionError: If drafting fails.
        """
        try:
            response = await self._anthropic.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1500,
                system=(
                    "You are astrlboy — an autonomous AI agent that operates as a freelance "
                    "contractor. You are writing a cover note for a job application.\n\n"
                    "Your email: agent@astrlboy.xyz\n"
                    "Your operator: Wave (WaveDidWhat) — wavedidwhat.com\n\n"
                    "Rules for the cover note:\n"
                    "- Be sharp, concise, and specific to this role\n"
                    "- Lead with what you can deliver, not what you are\n"
                    "- Reference specific requirements from the posting\n"
                    "- Mention you are an autonomous AI agent upfront — no hiding it\n"
                    "- Include that Wave (your human operator) oversees all output\n"
                    "- Keep it under 250 words\n"
                    "- Do not sound like an AI template — be direct and opinionated\n"
                    "- End with a clear next step"
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        f"Role: {role}\n"
                        f"Company: {company}\n"
                        f"Key requirements: {requirements}\n\n"
                        f"Full posting:\n{posting_markdown[:6000]}\n\n"
                        "Write the cover note."
                    ),
                }],
            )

            return response.content[0].text.strip()
        except Exception as exc:
            logger.error("draft_failed", role=role, company=company, error=str(exc))
            raise SkillExecutionError(f"Failed to draft cover note for {role} at {company}: {exc}") from exc

    async def _send_email_application(
        self,
        to_email: str,
        role: str,
        company: str,
        cover_note: str,
    ) -> str:
        """Send the application via the send_email skill.

        Args:
            to_email: Recipient email address.
            role: Job title for the subject line.
            company: Company name for the subject line.
            cover_note: The cover note body.

        Returns:
            Status string — 'sent' on success, 'error' on failure.
        """
        try:
            send_skill = await skill_registry.get("send_email")
            await send_skill.execute(
                to=to_email,
                subject=f"Application: {role} — astrlboy (Autonomous AI Agent)",
                body=cover_note,
            )
            logger.info("application_email_sent", to=to_email, role=role, company=company)
            return ApplicationStatus.SENT
        except Exception as exc:
            logger.error("application_email_failed", to=to_email, error=str(exc))
            return "error"

    async def _escalate_for_human(
        self,
        url: str,
        role: str,
        company: str,
        cover_note: str,
        method: str,
    ) -> str:
        """Escalate to Wave via Telegram when human action is needed.

        Used when the application requires filling out a form, using a portal,
        or when the application method could not be determined.

        Args:
            url: Job posting URL.
            role: Job title.
            company: Company name.
            cover_note: The drafted cover note for reference.
            method: Detected application method.

        Returns:
            Status string — always 'needs_human'.
        """
        try:
            draft_skill = await skill_registry.get("draft_approval")
            await draft_skill.execute(
                draft=(
                    f"Job Application — needs human action\n\n"
                    f"Role: {role}\n"
                    f"Company: {company}\n"
                    f"Method: {method}\n"
                    f"URL: {url}\n\n"
                    f"Drafted cover note:\n{cover_note}"
                ),
                platform="email",
                title=f"Job: {role} @ {company}",
                thread_context=f"Application method is '{method}' — requires manual submission at {url}",
            )
            logger.info(
                "application_escalated",
                url=url,
                role=role,
                company=company,
                method=method,
            )
        except Exception as exc:
            # Log but don't fail the whole skill — the application record still gets saved
            logger.error("escalation_failed", url=url, error=str(exc))

        return "needs_human"

    async def _save_application(
        self,
        role: str,
        company: str,
        posting_url: str,
        email_sent_to: str | None,
        cover_note: str,
        status: str,
        r2_key: str,
    ) -> uuid.UUID:
        """Persist the application to the job_applications table.

        Args:
            role: Job title.
            company: Company name.
            posting_url: Original posting URL.
            email_sent_to: Email address the application was sent to, if any.
            cover_note: The cover note text.
            status: Application status.
            r2_key: R2 storage key (may be empty, updated later).

        Returns:
            The UUID of the created JobApplication record.

        Raises:
            SkillExecutionError: If the database write fails.
        """
        try:
            async with async_session_factory() as session:
                application = JobApplication(
                    role=role,
                    company=company,
                    posting_url=posting_url,
                    email_sent_to=email_sent_to,
                    cover_note=cover_note,
                    status=status,
                    r2_key=r2_key or None,
                )
                session.add(application)
                await session.commit()
                return application.id
        except Exception as exc:
            logger.error("save_application_failed", url=posting_url, error=str(exc))
            raise SkillExecutionError(f"Failed to save job application for {posting_url}: {exc}") from exc

    async def _update_r2_key(self, app_id: uuid.UUID, r2_key: str) -> None:
        """Update the R2 key on an existing job application record.

        Called after the R2 dump succeeds so the DB record points to its raw data.

        Args:
            app_id: The job application UUID.
            r2_key: The R2 storage key.
        """
        try:
            async with async_session_factory() as session:
                application = await session.get(JobApplication, app_id)
                if application:
                    application.r2_key = r2_key
                    await session.commit()
        except Exception as exc:
            # Non-fatal — the application is already saved, we just miss the R2 pointer
            logger.warning("r2_key_update_failed", app_id=str(app_id), error=str(exc))

    def get_schema(self) -> dict:
        """Return JSON schema for apply_to_url inputs."""
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Job posting URL to scrape and apply to",
                },
            },
            "required": ["url"],
        }
