"""
Typed exception hierarchy.

Every external call and critical path raises from this hierarchy.
Generic try/except blocks should catch AstrlboyException at most —
never bare Exception.
"""


class AstrlboyException(Exception):
    """Base exception for all astrlboy errors."""

    pass


class SkillExecutionError(AstrlboyException):
    """A skill failed during execution (e.g. Firecrawl, Tavily, X API)."""

    pass


class ExternalAPIError(AstrlboyException):
    """An external API returned an error after retries."""

    pass


class DatabaseError(AstrlboyException):
    """A database operation failed."""

    pass


class EscalationRequired(AstrlboyException):
    """The current situation requires human intervention from the operator."""

    pass


class ContractNotFound(AstrlboyException):
    """Requested contract slug does not exist or is not active."""

    pass


class SkillNotFound(AstrlboyException):
    """Requested skill name is not registered in the SkillRegistry."""

    pass
