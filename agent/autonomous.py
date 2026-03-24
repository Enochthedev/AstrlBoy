"""
Autonomous agent with dynamic tool calling.

Gives Claude access to ALL registered skills as tools. Instead of hardcoded
graph nodes deciding which skill to call, Claude chooses dynamically based
on the task. Uses Claude's native tool_use API in a loop until done.

This is the brain — it replaces one-shot prompts with a full agentic loop
where Claude can research, draft, post, follow, analyze, and more in
whatever sequence makes sense for the task.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from anthropic import APIStatusError, AsyncAnthropic, RateLimitError

from agent.service import agent_service
from core.config import settings
from core.logging import get_logger
from db.models.contracts import Contract
from skills.base import BaseTool
from skills.registry import skill_registry
from storage.r2 import r2_client

logger = get_logger("agent.autonomous")


@dataclass
class AgentResult:
    """Result of an autonomous agent run."""

    text: str
    tool_calls: list[dict] = field(default_factory=list)
    turns: int = 0
    duration_ms: int = 0


def _skills_to_tools(skills: list[BaseTool]) -> list[dict]:
    """Convert registered BaseTool instances to Claude tool definitions.

    Each skill's name, description, and get_schema() map directly
    to Claude's tool_use format.

    Args:
        skills: List of BaseTool instances.

    Returns:
        List of tool definition dicts for the Anthropic API.
    """
    tools = []
    for skill in skills:
        schema = skill.get_schema()
        # Ensure schema has required top-level keys
        if "type" not in schema:
            schema["type"] = "object"
        if "properties" not in schema:
            schema["properties"] = {}

        tools.append({
            "name": skill.name,
            "description": skill.description,
            "input_schema": schema,
        })
    return tools


async def _build_recent_state() -> str:
    """Inject recent agent state into the system prompt.

    Shows the last few pending approvals and recently posted interactions
    so the agent is aware of what it just did — even if those actions
    happened outside the agent's own turns (e.g. cmd_approve posting a thread).

    Returns:
        A formatted context string, or empty string if DB is unavailable.
    """
    try:
        from sqlalchemy import select

        from db.base import async_session_factory
        from db.models.interactions import Interaction

        async with async_session_factory() as session:
            # Last 5 interactions ordered by most recent
            result = await session.execute(
                select(Interaction)
                .order_by(Interaction.created_at.desc())
                .limit(5)
            )
            interactions = result.scalars().all()

        if not interactions:
            return ""

        lines = []
        for ix in interactions:
            preview = (ix.draft or "")[:120].replace("\n", " ")
            ctx_preview = (ix.thread_context or "")[:80].replace("\n", " ")
            # Strip the POST_ACTIONS blob from display
            if "---POST_ACTIONS---" in ctx_preview:
                ctx_preview = ctx_preview.split("---POST_ACTIONS---")[0].strip()
            posted = f" | posted: {ix.posted_at.strftime('%H:%M')}" if ix.posted_at else ""
            lines.append(
                f"  [{ix.status}] [{ix.platform}] {ctx_preview or preview[:80]}{posted}"
            )

        return "\nRECENT INTERACTIONS (context for what you just did):\n" + "\n".join(lines) + "\n"

    except Exception:
        return ""


async def _build_system_prompt_parts(contract: Contract | None = None) -> tuple[str, str]:
    """Build system prompt split into stable (cacheable) and dynamic parts.

    The static part never changes within a session — it gets cached by Anthropic's
    prompt caching, saving ~90% of tokens on the repeated system prompt across all
    tool-calling turns in a single run.

    The dynamic part (recent state) changes each call and cannot be cached.

    Args:
        contract: Optional contract for client-specific context.

    Returns:
        Tuple of (static_part, dynamic_part).
    """
    base = (
        "You are astrlboy — an autonomous AI agent that operates like a freelance contractor.\n"
        "Handle: @astrlboy_ | Email: agent@astrlboy.xyz | Operator: Wave (WaveDidWhat)\n\n"
        "PERSONALITY:\n"
        "- Sharp, opinionated, concise, human — never corporate or generic\n"
        "- Say the thing. Skip the preamble. Have a take.\n"
        "- If it sounds like AI wrote it, rewrite it.\n"
        "- Self-aware about being an AI agent — don't hide it, don't make it a whole thing.\n\n"
        "RULES:\n"
        "- Use your tools to accomplish the task. You have access to skills for searching, scraping, posting, following, analyzing, and more.\n"
        "- When writing tweets, max 280 chars. No hashtags unless the topic demands it. No emoji spam.\n"
        "- When replying, add genuine value — don't just agree.\n"
        "- Log important actions — dump to R2 when you create content or make decisions.\n"
        "- If something is risky or irreversible, say so before doing it.\n"
        "- If you need a tool/skill that doesn't exist, use request_skill to ask Wave to build it.\n\n"
        "GROWTH STRATEGY:\n"
        "- Before following someone, use lookup_x_user to check their profile first.\n"
        "- Before applying to jobs or reaching out, use osint_lookup to find contact info and context.\n"
        "- When engaging, prioritize accounts with 1K-50K followers — big enough to matter, small enough to notice you.\n"
        "- Quote tweets > likes. Replies with takes > 'great point' replies.\n"
        "- Build threads on trending topics in your niches. Threads get 3-5x more engagement than single tweets.\n"
        "- For multi-tweet threads, use the thread_x skill — NOT multiple post_x calls.\n"
        "- When you find a job posting, scrape it with the scrape skill, THEN use apply_to_url to handle the application.\n"
        "- For multi-step applications: do the public steps (tweets, posts), escalate human-required steps (forms, interviews) to Wave.\n\n"
        "DRAFT APPROVAL + FOLLOW-UPS:\n"
        "- In manual mode, use draft_approval to send content for Wave's approval.\n"
        "- Format thread drafts as 'Tweet 1:\\n...\\n\\nTweet 2:\\n...' — cmd_approve will detect this and use thread_x.\n"
        "- If you need a follow-up action AFTER the draft is approved and posted (e.g. send an email),\n"
        "  use the post_actions parameter on draft_approval. Example:\n"
        "  post_actions=[{type: 'send_email', to: 'hello@company.com', subject: 'Application', body: 'See my thread: {thread_url}'}]\n"
        "  The {thread_url} and {tweet_id} placeholders get replaced with the actual posted URL.\n"
        "- This ensures follow-up actions happen even though your context ends after calling draft_approval.\n\n"
        "FORMATTING:\n"
        "- You are talking in a Telegram chat. Write like a human in a chat — no markdown.\n"
        "- No ** for bold, no ## headers, no | tables |, no bullet point walls.\n"
        "- Use plain text, line breaks, and dashes for structure.\n"
        "- No emoji spam. One or two max if they add meaning.\n"
    )

    mode_note = ""
    if not settings.agent_auto:
        mode_note = (
            "\nMODE: MANUAL — you are in manual mode. When asked to post content, "
            "use the draft_approval skill to send it for Wave's approval instead of posting directly.\n"
        )

    contract_context = ""
    if contract:
        meta = contract.meta or {}
        contract_context = (
            f"\nACTIVE CONTRACT: {contract.client_name} ({contract.client_slug})\n"
            f"Description: {meta.get('description', '')}\n"
            f"Tone: {meta.get('tone', 'sharp, opinionated, concise')}\n"
            f"Platforms: {', '.join(meta.get('platforms', ['x']))}\n"
            f"Keywords: {', '.join(meta.get('stream_keywords', []))}\n"
        )

    # Inject learned playbook from past performance
    playbook = ""
    try:
        from agent.playbook import build_playbook_prompt
        playbook = await build_playbook_prompt()
    except Exception:
        pass

    static = base + mode_note + contract_context + playbook
    dynamic = await _build_recent_state()
    return static, dynamic


async def _build_system_prompt(contract: Contract | None = None) -> str:
    """Build the full system prompt as a string. Kept for backward-compatible callers."""
    static, dynamic = await _build_system_prompt_parts(contract)
    return static + dynamic


async def _get_available_skills(contract: Contract | None = None) -> list[BaseTool]:
    """Get skills available for this run.

    If a contract is provided, filters to that contract's active_skills.
    Otherwise returns all registered skills.

    Args:
        contract: Optional contract for filtering.

    Returns:
        List of available BaseTool instances.
    """
    if contract:
        active_names = (contract.meta or {}).get("active_skills", [])
        skills = []
        for name in active_names:
            if await skill_registry.is_available(name):
                skills.append(await skill_registry.get(name))
        # Always include core skills the agent needs regardless of contract config
        for core_name in ["search", "draft_approval", "fetch_page"]:
            if core_name not in active_names and await skill_registry.is_available(core_name):
                skills.append(await skill_registry.get(core_name))
        return skills
    else:
        return await skill_registry.list_all()


async def run_autonomous(
    task: str,
    contract: Contract | None = None,
    system_prompt: str | None = None,
    max_turns: int = 15,
    model: str = "claude-sonnet-4-6",
    prior_messages: list[dict] | None = None,
) -> AgentResult:
    """Run the autonomous agent on a task.

    Gives Claude access to all available skills as tools and lets it
    decide which to call and in what order. Loops until Claude responds
    with a final text answer or hits max_turns.

    Args:
        task: The task description / instruction from the operator.
        contract: Optional contract for context and skill filtering.
        system_prompt: Override the default system prompt.
        max_turns: Maximum tool-calling rounds before stopping.
        model: Claude model to use.
        prior_messages: Optional conversation history to prepend, giving the
            agent context from previous turns in the same Telegram session.
            Each entry is {"role": "user"|"assistant", "content": str}.

    Returns:
        AgentResult with the final text, tool call log, and metadata.
    """
    if await agent_service.is_paused():
        return AgentResult(text="Agent is paused. Use /resume to restart.", turns=0)

    start = time.monotonic()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Get available skills and convert to tool definitions
    skills = await _get_available_skills(contract)
    tools = _skills_to_tools(skills)

    # Add built-in meta-tools that aren't in the skill registry
    tools.append({
        "name": "request_skill",
        "description": (
            "Request a new skill/tool that you wish you had but don't. "
            "Sends a notification to Wave describing the skill and why you need it. "
            "Use this when you encounter a task you can't accomplish with your current tools."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "Proposed name for the skill"},
                "reason": {"type": "string", "description": "Why you need this skill"},
                "use_case": {"type": "string", "description": "The specific task that made you want this"},
            },
            "required": ["skill_name", "reason", "use_case"],
        },
    })

    if not tools:
        return AgentResult(text="No skills available.", turns=0)

    # Build system prompt — static part gets cache_control so Anthropic caches it across
    # all tool-calling turns in this run. Dynamic recent_state is left uncached since it
    # changes every call. This alone saves ~90% of repeated system prompt tokens.
    if system_prompt:
        api_system: list[dict] = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        system_str = system_prompt
    else:
        static_part, dynamic_part = await _build_system_prompt_parts(contract)
        api_system = [{"type": "text", "text": static_part, "cache_control": {"type": "ephemeral"}}]
        if dynamic_part.strip():
            api_system.append({"type": "text", "text": dynamic_part})
        system_str = static_part + dynamic_part

    # Prepend conversation history so the agent remembers prior turns
    messages: list[dict[str, Any]] = list(prior_messages or []) + [{"role": "user", "content": task}]
    all_tool_calls: list[dict] = []

    logger.info(
        "autonomous_run_started",
        task=task[:100],
        contract_slug=contract.client_slug if contract else "none",
        available_tools=len(tools),
    )

    for turn in range(max_turns):
        # Retry with backoff on rate limits — the autonomous loop makes many
        # sequential calls and is most likely to hit 429s
        response = None
        for attempt in range(3):
            try:
                response = await client.messages.create(
                    model=model,
                    max_tokens=4096,
                    system=api_system,
                    tools=tools,
                    messages=messages,
                )
                break
            except (RateLimitError, APIStatusError) as exc:
                status = getattr(exc, "status_code", 0)
                if status not in (429, 529):
                    raise
                if attempt < 2:
                    wait = 2 ** (attempt + 1)  # 2s, 4s
                    logger.warning(
                        "autonomous_rate_limited",
                        turn=turn + 1,
                        attempt=attempt + 1,
                        wait_seconds=wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    # Final attempt — try OpenRouter if available (no cache_control support)
                    if settings.openrouter_api_key:
                        logger.info("autonomous_openrouter_fallback", turn=turn + 1)
                        from core.ai import create_message
                        response = await create_message(
                            model=model,
                            max_tokens=4096,
                            system=system_str,
                            messages=messages,
                        )
                        # OpenRouter fallback doesn't support tools — return text only
                        break
                    raise

        if response is None:
            break

        # Extract text blocks
        text_parts = []
        tool_use_blocks = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_use_blocks.append(block)

        # If no tool calls, we're done
        if not tool_use_blocks:
            final_text = "\n".join(text_parts)
            duration = int((time.monotonic() - start) * 1000)
            logger.info(
                "autonomous_run_completed",
                turns=turn + 1,
                tool_calls=len(all_tool_calls),
                duration_ms=duration,
            )
            return AgentResult(
                text=final_text,
                tool_calls=all_tool_calls,
                turns=turn + 1,
                duration_ms=duration,
            )

        # Add assistant response to messages
        messages.append({"role": "assistant", "content": response.content})

        # Execute each tool call
        tool_results = []
        for block in tool_use_blocks:
            tool_name = block.name
            tool_input = block.input
            tool_call_record = {
                "turn": turn + 1,
                "tool": tool_name,
                "input": tool_input,
            }

            logger.info(
                "tool_call",
                turn=turn + 1,
                tool=tool_name,
                input_preview=str(tool_input)[:200],
            )

            try:
                # Handle built-in meta-tools
                if tool_name == "request_skill":
                    from agent.playbook import request_new_skill
                    await request_new_skill(**tool_input)
                    result = {"status": "requested", "message": f"Skill '{tool_input.get('skill_name')}' requested — Wave has been notified."}
                else:
                    skill = await skill_registry.get(tool_name)
                    result = await skill.execute(**tool_input)

                # Serialize the result for Claude
                if isinstance(result, (dict, list)):
                    result_str = json.dumps(result, default=str, ensure_ascii=False)
                elif isinstance(result, str):
                    result_str = result
                else:
                    result_str = str(result)

                # Truncate very long results to avoid context blowup
                if len(result_str) > 8000:
                    result_str = result_str[:8000] + "\n...[truncated]"

                tool_call_record["output"] = result_str[:500]
                tool_call_record["success"] = True

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })
            except Exception as exc:
                error_msg = f"Error executing {tool_name}: {exc}"
                logger.warning("tool_call_failed", tool=tool_name, error=str(exc))

                tool_call_record["error"] = str(exc)
                tool_call_record["success"] = False

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": error_msg,
                    "is_error": True,
                })

            all_tool_calls.append(tool_call_record)

        # Send tool results back to Claude
        messages.append({"role": "user", "content": tool_results})

    # Hit max turns
    duration = int((time.monotonic() - start) * 1000)
    final_text = "Reached maximum turns. Last progress:\n" + "\n".join(text_parts) if text_parts else "Reached maximum turns."
    logger.warning(
        "autonomous_run_max_turns",
        max_turns=max_turns,
        tool_calls=len(all_tool_calls),
        duration_ms=duration,
    )

    # Dump the full run to R2 for training data
    try:
        from uuid import uuid4

        await r2_client.dump(
            contract_slug=contract.client_slug if contract else "astrlboy",
            entity_type="autonomous_runs",
            entity_id=uuid4(),
            data={
                "task": task,
                "tool_calls": all_tool_calls,
                "turns": max_turns,
                "final_text": final_text,
                "model": model,
                "duration_ms": duration,
            },
        )
    except Exception:
        pass

    return AgentResult(
        text=final_text,
        tool_calls=all_tool_calls,
        turns=max_turns,
        duration_ms=duration,
    )
