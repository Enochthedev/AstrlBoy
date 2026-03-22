"""
Pydantic Settings configuration.

Validates every required environment variable at startup.
The app refuses to start if anything is missing or malformed.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All environment variables for astrlboy, validated at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # AI
    anthropic_api_key: str
    openrouter_api_key: str = ""

    # Observability
    langsmith_api_key: str = ""
    langsmith_project: str = "astrlboy"
    langchain_endpoint: str = "https://eu.api.smith.langchain.com"

    # Scraping & Search
    firecrawl_api_key: str = ""
    tavily_api_key: str = ""
    serper_api_key: str = ""

    # Social — X (OAuth 1.0a for posting + OAuth 2.0 for app auth)
    twitter_api_key: str = ""
    twitter_api_secret: str = ""
    twitter_access_token: str = ""
    twitter_access_secret: str = ""
    twitter_bearer_token: str = ""
    twitter_client_id: str = ""
    twitter_client_secret: str = ""

    # Social — LinkedIn
    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    linkedin_access_token: str = ""

    # Email — SMTP outbound
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""

    # Email — IMAP inbound
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_pass: str = ""

    agent_email: str = "agent@astrlboy.xyz"

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Primary DB
    database_url: str = Field(
        ...,
        description="Neon PostgreSQL async URL (postgresql+asyncpg://...)",
    )

    # R2 (training data + raw dumps)
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "astrlboy-data"
    r2_endpoint_url: str = ""

    # Redis
    redis_url: str = ""

    # Agent control
    agent_paused: bool = False
    agent_name: str = "astrlboy"
    agent_handle: str = "@astrlboy"
    log_level: str = "INFO"


# Singleton — import this everywhere
settings = Settings()  # type: ignore[call-arg]
