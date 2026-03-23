"""
X API budget tracker — allocation-based cost control.

You set a monthly dollar budget (e.g. $5) and the system automatically
splits it across operation categories based on priority weights:

  Content creation (posts/replies) gets the biggest share because
  that's the core growth activity. Reads and follows get smaller
  allocations since they're support activities.

If a category is exhausted, the system blocks that operation type
and logs it. If the total monthly budget is nearly gone, it blocks
everything except replies to our own tweets.

Tier recommendations fire when projected monthly spend exceeds
what a fixed tier would cost.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from cache.redis import redis_client
from core.logging import get_logger

logger = get_logger("core.budget")

# Redis key prefixes
_DAILY_KEY = "astrlboy:budget:daily"
_MONTHLY_KEY = "astrlboy:budget:monthly"
_DAILY_TWEETS_KEY = "astrlboy:budget:tweets_today"

# TTLs
_ONE_DAY = 86400
_THIRTY_DAYS = 2592000


class XOperation(str, Enum):
    """X API operations with exact pay-per-use pricing from X Developer Console.

    Source: https://docs.x.com/x-api/getting-started/pricing (March 2026)
    """
    POST_CREATE = "post_create"         # $0.010 per request
    POST_READ = "post_read"             # $0.005 per resource
    USER_LOOKUP = "user_read"           # $0.010 per resource
    USER_INTERACTION = "user_interact"  # $0.015 per request (follow, like, RT)
    DM_READ = "dm_read"                 # $0.010 per resource
    DM_CREATE = "dm_create"             # $0.015 per request


# Cost per operation in cents — matches X Developer Console exactly
COST_MAP: dict[XOperation, float] = {
    XOperation.POST_CREATE: 1.0,        # $0.010
    XOperation.POST_READ: 0.5,          # $0.005
    XOperation.USER_LOOKUP: 1.0,        # $0.010
    XOperation.USER_INTERACTION: 1.5,   # $0.015
    XOperation.DM_READ: 1.0,            # $0.010
    XOperation.DM_CREATE: 1.5,          # $0.015
}

# Budget allocation weights — how the monthly budget is split.
# Higher weight = bigger share of the budget.
# Content creation gets the lion's share because posting is the core activity.
ALLOCATION_WEIGHTS: dict[XOperation, float] = {
    XOperation.POST_CREATE: 0.35,       # 35% — tweets and replies (core growth)
    XOperation.POST_READ: 0.25,         # 25% — reading mentions, threads, metrics
    XOperation.USER_LOOKUP: 0.15,       # 15% — follower checks, profile lookups
    XOperation.USER_INTERACTION: 0.15,  # 15% — follows, likes
    XOperation.DM_READ: 0.05,           # 5%  — DM monitoring
    XOperation.DM_CREATE: 0.05,         # 5%  — DM sending
}


class BudgetTracker:
    """Allocation-based X API budget tracker.

    Given a monthly budget (e.g. $5), automatically splits it across
    operation types and enforces per-category and total limits.
    """

    def __init__(
        self,
        daily_tweet_cap: int = 15,
        monthly_budget_cents: int = 500,
    ) -> None:
        self.daily_tweet_cap = daily_tweet_cap
        self.monthly_budget_cents = monthly_budget_cents

        # Pre-compute per-category monthly budgets in cents
        self.category_budgets: dict[XOperation, float] = {
            op: monthly_budget_cents * weight
            for op, weight in ALLOCATION_WEIGHTS.items()
        }

    def _daily_key(self) -> str:
        """Redis key for today's spend."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"{_DAILY_KEY}:{today}"

    def _monthly_key(self) -> str:
        """Redis key for this month's spend."""
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return f"{_MONTHLY_KEY}:{month}"

    async def track(
        self,
        operation: XOperation,
        count: int = 1,
        contract_slug: str | None = None,
    ) -> float:
        """Record an API operation and return the cost in cents.

        Tracks against both the global budget and the per-contract budget
        (if a contract_slug is provided). This lets you see spend per client.

        Args:
            operation: The type of X API operation.
            count: Number of units (e.g. 10 tweets read = count=10).
            contract_slug: Optional contract slug to track per-client spend.

        Returns:
            Total cost in cents for this operation.
        """
        cost = COST_MAP.get(operation, 0) * count

        if redis_client is None:
            return cost

        try:
            daily_key = self._daily_key()
            monthly_key = self._monthly_key()

            pipe = redis_client.pipeline()
            # Global tracking
            pipe.hincrbyfloat(daily_key, operation.value, cost)
            pipe.hincrbyfloat(monthly_key, operation.value, cost)
            pipe.expire(daily_key, _ONE_DAY)
            pipe.expire(monthly_key, _THIRTY_DAYS)

            # Per-contract tracking — enables per-client budget dashboards
            if contract_slug:
                contract_monthly = f"{_MONTHLY_KEY}:{datetime.now(timezone.utc).strftime('%Y-%m')}:{contract_slug}"
                contract_daily = f"{_DAILY_KEY}:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}:{contract_slug}"
                pipe.hincrbyfloat(contract_monthly, operation.value, cost)
                pipe.hincrbyfloat(contract_daily, operation.value, cost)
                pipe.expire(contract_monthly, _THIRTY_DAYS)
                pipe.expire(contract_daily, _ONE_DAY)

            await pipe.execute()

            logger.debug(
                "budget_tracked",
                operation=operation.value,
                count=count,
                cost_cents=cost,
                contract_slug=contract_slug,
            )
        except Exception as exc:
            logger.warning("budget_track_failed", error=str(exc))

        return cost

    async def can_spend(self, operation: XOperation, count: int = 1) -> bool:
        """Check if an operation is within its allocated budget.

        Checks both the per-category allocation and the total monthly budget.

        Args:
            operation: The operation to check.
            count: How many units.

        Returns:
            True if within budget, False if the allocation is exhausted.
        """
        if redis_client is None:
            return True

        try:
            monthly_key = self._monthly_key()
            all_costs = await redis_client.hgetall(monthly_key)

            if not all_costs:
                return True

            # Check per-category allocation
            category_spent = float(all_costs.get(operation.value, 0))
            category_budget = self.category_budgets.get(operation, 0)
            proposed_cost = COST_MAP.get(operation, 0) * count

            if category_spent + proposed_cost > category_budget:
                logger.warning(
                    "category_budget_exhausted",
                    operation=operation.value,
                    spent_cents=category_spent,
                    budget_cents=category_budget,
                )
                return False

            # Check total monthly budget
            total_spent = sum(float(v) for v in all_costs.values())
            if total_spent + proposed_cost > self.monthly_budget_cents:
                logger.warning(
                    "monthly_budget_exhausted",
                    total_spent_cents=total_spent,
                    budget_cents=self.monthly_budget_cents,
                )
                return False

            return True
        except Exception:
            return True  # Budget check failure should not block work

    async def check_tweet_budget(self) -> bool:
        """Check if we can still post tweets today.

        Returns:
            True if under the daily tweet cap, False if exhausted.
        """
        if redis_client is None:
            return True

        try:
            count = await redis_client.get(_DAILY_TWEETS_KEY)
            current = int(count) if count else 0
            return current < self.daily_tweet_cap
        except Exception:
            return True

    async def increment_tweet_count(self) -> int:
        """Increment daily tweet counter. Returns new count."""
        if redis_client is None:
            return 1

        try:
            count = await redis_client.incr(_DAILY_TWEETS_KEY)
            if count == 1:
                await redis_client.expire(_DAILY_TWEETS_KEY, _ONE_DAY)
            return count
        except Exception:
            return 1

    async def get_tweet_count_today(self) -> int:
        """Get current daily tweet count."""
        if redis_client is None:
            return 0

        try:
            count = await redis_client.get(_DAILY_TWEETS_KEY)
            return int(count) if count else 0
        except Exception:
            return 0

    async def check_monthly_budget(self) -> bool:
        """Check if we're still within the total monthly budget."""
        if redis_client is None:
            return True

        try:
            monthly_key = self._monthly_key()
            all_costs = await redis_client.hgetall(monthly_key)
            total_cents = sum(float(v) for v in all_costs.values()) if all_costs else 0
            return total_cents < self.monthly_budget_cents
        except Exception:
            return True

    async def get_daily_spend(self) -> dict[str, Any]:
        """Get today's spend breakdown in dollars."""
        if redis_client is None:
            return {"total": 0, "breakdown": {}}

        try:
            daily_key = self._daily_key()
            all_costs = await redis_client.hgetall(daily_key)
            breakdown = {k: round(float(v) / 100, 4) for k, v in all_costs.items()} if all_costs else {}
            total = sum(breakdown.values())
            return {"total": round(total, 4), "breakdown": breakdown}
        except Exception:
            return {"total": 0, "breakdown": {}}

    async def get_monthly_spend(self) -> dict[str, Any]:
        """Get this month's spend with per-category budgets and tier recommendation."""
        if redis_client is None:
            return {
                "total": 0, "breakdown": {}, "allocations": {},
                "budget_dollars": self.monthly_budget_cents / 100,
                "budget_remaining": self.monthly_budget_cents / 100,
                "tier_recommendation": None,
            }

        try:
            monthly_key = self._monthly_key()
            all_costs = await redis_client.hgetall(monthly_key)
            breakdown = {k: round(float(v) / 100, 4) for k, v in all_costs.items()} if all_costs else {}
            total = sum(breakdown.values())
            budget_dollars = round(self.monthly_budget_cents / 100, 2)
            budget_remaining = round(budget_dollars - total, 2)

            # Per-category allocation status
            allocations = {}
            for op, weight in ALLOCATION_WEIGHTS.items():
                alloc_dollars = round(budget_dollars * weight, 2)
                spent_dollars = breakdown.get(op.value, 0)
                allocations[op.value] = {
                    "allocated": alloc_dollars,
                    "spent": round(spent_dollars, 4),
                    "remaining": round(alloc_dollars - spent_dollars, 4),
                    "pct_used": round((spent_dollars / alloc_dollars * 100), 1) if alloc_dollars > 0 else 0,
                }

            tier_rec = self._recommend_tier(total)

            return {
                "total": round(total, 4),
                "breakdown": breakdown,
                "allocations": allocations,
                "budget_dollars": budget_dollars,
                "budget_remaining": budget_remaining,
                "pct_used": round((total / budget_dollars * 100), 1) if budget_dollars > 0 else 0,
                "tier_recommendation": tier_rec,
            }
        except Exception:
            return {
                "total": 0, "breakdown": {}, "allocations": {},
                "budget_dollars": self.monthly_budget_cents / 100,
                "budget_remaining": self.monthly_budget_cents / 100,
                "tier_recommendation": None,
            }

    async def get_contract_spend(self, contract_slug: str) -> dict[str, Any]:
        """Get this month's spend for a specific contract.

        Args:
            contract_slug: The client slug (e.g. 'mentorable').

        Returns:
            Dict with per-operation breakdown and total in dollars.
        """
        if redis_client is None:
            return {"total": 0, "breakdown": {}}

        try:
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            key = f"{_MONTHLY_KEY}:{month}:{contract_slug}"
            all_costs = await redis_client.hgetall(key)
            breakdown = {k: round(float(v) / 100, 4) for k, v in all_costs.items()} if all_costs else {}
            total = sum(breakdown.values())
            return {"contract_slug": contract_slug, "total": round(total, 4), "breakdown": breakdown}
        except Exception:
            return {"contract_slug": contract_slug, "total": 0, "breakdown": {}}

    async def check_contract_budget(
        self,
        contract_slug: str,
        contract_budget_cents: int,
    ) -> bool:
        """Check if a contract is within its dedicated budget.

        Args:
            contract_slug: The client slug.
            contract_budget_cents: The contract's monthly budget in cents.

        Returns:
            True if under budget, False if exhausted.
        """
        if redis_client is None or contract_budget_cents <= 0:
            return True  # 0 = no per-contract limit, use global

        try:
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            key = f"{_MONTHLY_KEY}:{month}:{contract_slug}"
            all_costs = await redis_client.hgetall(key)
            total_cents = sum(float(v) for v in all_costs.values()) if all_costs else 0
            return total_cents < contract_budget_cents
        except Exception:
            return True

    def _recommend_tier(self, current_monthly_dollars: float) -> dict[str, Any] | None:
        """Recommend a tier change if projected spend exceeds a fixed tier's cost."""
        now = datetime.now(timezone.utc)
        day_of_month = now.day
        if day_of_month < 3:
            return None

        projected = (current_monthly_dollars / day_of_month) * 30

        if projected > 200:
            savings = round(projected - 200, 2)
            return {
                "current_projected": round(projected, 2),
                "recommended_tier": "basic",
                "tier_cost": 200,
                "projected_savings": savings,
                "reason": f"Projected ${projected:.2f}/mo — Basic tier ($200/mo) saves ${savings:.2f}",
            }

        if projected > 50:
            return {
                "current_projected": round(projected, 2),
                "recommended_tier": "pay_per_use",
                "tier_cost": 0,
                "projected_savings": 0,
                "reason": f"Projected ${projected:.2f}/mo — watch closely, Basic tier is cheaper above $200/mo",
            }

        return None


# Singleton — configured from settings at startup
budget_tracker: BudgetTracker | None = None


def init_budget(daily_tweet_cap: int = 15, monthly_budget_cents: int = 500) -> BudgetTracker:
    """Initialize the budget tracker singleton.

    Called from main.py startup with values from settings.

    Args:
        daily_tweet_cap: Max tweets (posts + replies) per day.
        monthly_budget_cents: Monthly X API budget in cents.

    Returns:
        The initialized BudgetTracker.
    """
    global budget_tracker
    budget_tracker = BudgetTracker(
        daily_tweet_cap=daily_tweet_cap,
        monthly_budget_cents=monthly_budget_cents,
    )

    # Log the allocation plan
    budget_dollars = monthly_budget_cents / 100
    for op, weight in ALLOCATION_WEIGHTS.items():
        alloc = round(budget_dollars * weight, 2)
        logger.info(
            "budget_allocation",
            operation=op.value,
            allocated_dollars=alloc,
            pct=round(weight * 100),
        )

    logger.info(
        "budget_initialized",
        daily_tweet_cap=daily_tweet_cap,
        monthly_budget_dollars=budget_dollars,
    )
    return budget_tracker
