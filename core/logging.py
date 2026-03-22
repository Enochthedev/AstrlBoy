"""
Structured JSON logging configuration.

Uses structlog for structured logging. Railway captures stdout,
so all output is JSON-formatted for easy parsing and search.
"""

import logging
import sys

import structlog

from core.config import settings


def setup_logging() -> None:
    """Configure structured JSON logging for the entire application."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Set root logger level from config
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger bound with the given module name.

    Args:
        name: Module name for the logger (e.g. 'graphs.content').

    Returns:
        A structlog BoundLogger instance.
    """
    return structlog.get_logger(name)
