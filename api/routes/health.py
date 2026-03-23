"""
Health, status, and budget endpoints.
"""

from fastapi import APIRouter

from agent.service import agent_service
from contracts.service import contracts_service
from core.budget import budget_tracker
from core.config import settings

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """Liveness check for Railway health monitoring."""
    return {"status": "ok", "agent": settings.agent_name, "paused": settings.agent_paused}


@router.get("/status")
async def status() -> dict:
    """Agent status including active contracts and pending queue count."""
    contracts = await contracts_service.get_active_contracts()
    pending = await agent_service.get_pending_escalations()

    return {
        "agent": settings.agent_name,
        "paused": settings.agent_paused,
        "active_contracts": len(contracts),
        "contract_slugs": [c.client_slug for c in contracts],
        "pending_escalations": len(pending),
    }


@router.get("/budget")
async def budget() -> dict:
    """X API budget status — daily and monthly spend, tweet cap, tier recommendation."""
    if not budget_tracker:
        return {"error": "Budget tracker not initialized"}

    daily = await budget_tracker.get_daily_spend()
    monthly = await budget_tracker.get_monthly_spend()
    tweets_today = await budget_tracker.get_tweet_count_today()

    # Per-contract spend breakdown
    contracts = await contracts_service.get_active_contracts()
    contract_spend = {}
    for c in contracts:
        contract_spend[c.client_slug] = await budget_tracker.get_contract_spend(c.client_slug)
        # Include per-contract budget if set
        meta_budget = (c.meta or {}).get("budget", {})
        if meta_budget.get("monthly_budget_cents", 0) > 0:
            contract_spend[c.client_slug]["budget_cents"] = meta_budget["monthly_budget_cents"]

    return {
        "daily": {
            **daily,
            "tweets_today": tweets_today,
            "tweet_cap": budget_tracker.daily_tweet_cap,
            "tweets_remaining": max(0, budget_tracker.daily_tweet_cap - tweets_today),
        },
        "monthly": monthly,
        "contracts": contract_spend,
    }
