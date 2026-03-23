"""
Cloudflare R2 client for raw data dumps.

Every significant agent action dumps its raw I/O here for future model training.
Key naming: {contract_slug}/{yyyy}/{mm}/{dd}/{entity_type}/{uuid}.json

boto3 calls are synchronous — we run them via asyncio.to_thread() to avoid
blocking the event loop. SSL verification is disabled because R2 endpoints
use Cloudflare-issued certificates that some container runtimes don't trust.
"""

import asyncio
import json
import os
import urllib3
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config as BotoConfig

# R2 uses Cloudflare-issued certs that boto3 can't verify in some environments.
# Disable warnings globally and also set env vars that urllib3/requests check.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
os.environ.setdefault("PYTHONHTTPSVERIFY", "0")

from core.config import settings
from core.exceptions import ExternalAPIError
from core.logging import get_logger

logger = get_logger("storage.r2")


class R2Client:
    """Cloudflare R2 storage client for training data and raw dumps.

    All model I/O, scraped content, and trend signals are stored here
    from day one to build the training dataset.
    """

    def __init__(self) -> None:
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
            config=BotoConfig(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
            verify=False,
        )
        self._bucket = settings.r2_bucket_name

    def _build_key(
        self,
        contract_slug: str,
        entity_type: str,
        entity_id: UUID,
        timestamp: datetime | None = None,
    ) -> str:
        """Build a consistent R2 object key.

        Args:
            contract_slug: Client slug (e.g. 'mentorable') or 'astrlboy' for agent-level data.
            entity_type: Type of entity (e.g. 'content', 'trend_signals').
            entity_id: UUID of the entity.
            timestamp: When the entity was created. Defaults to now.

        Returns:
            Key string like 'mentorable/2026/03/22/content/a1b2c3d4.json'.
        """
        ts = timestamp or datetime.now(timezone.utc)
        return f"{contract_slug}/{ts.year}/{ts.month:02d}/{ts.day:02d}/{entity_type}/{entity_id}.json"

    async def dump(
        self,
        contract_slug: str,
        entity_type: str,
        entity_id: UUID,
        data: dict[str, Any],
        timestamp: datetime | None = None,
    ) -> str:
        """Dump a JSON payload to R2.

        Args:
            contract_slug: Client slug or 'astrlboy'.
            entity_type: Type of entity being stored.
            entity_id: UUID of the entity.
            data: The payload to store (must be JSON-serializable).
            timestamp: Optional timestamp for key generation.

        Returns:
            The R2 key where the data was stored.

        Raises:
            ExternalAPIError: If the upload fails.
        """
        key = self._build_key(contract_slug, entity_type, entity_id, timestamp)
        payload = {
            "entity_id": str(entity_id),
            "entity_type": entity_type,
            "contract_slug": contract_slug,
            "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
            **data,
        }

        try:
            body = json.dumps(payload, default=str)
            # boto3 is synchronous — run in a thread to avoid blocking the loop
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
            )
            logger.info(
                "r2_dump_stored",
                key=key,
                contract_slug=contract_slug,
                entity_type=entity_type,
            )
            return key
        except Exception as exc:
            logger.error("r2_dump_failed", key=key, error=str(exc))
            raise ExternalAPIError(f"R2 upload failed for {key}: {exc}") from exc

    async def get(self, key: str) -> dict[str, Any]:
        """Retrieve a JSON payload from R2.

        Args:
            key: The R2 object key.

        Returns:
            The parsed JSON payload.

        Raises:
            ExternalAPIError: If the retrieval fails.
        """
        try:
            # boto3 is synchronous — run in a thread to avoid blocking the loop
            response = await asyncio.to_thread(
                self._client.get_object, Bucket=self._bucket, Key=key
            )
            body = response["Body"].read().decode("utf-8")
            return json.loads(body)
        except Exception as exc:
            logger.error("r2_get_failed", key=key, error=str(exc))
            raise ExternalAPIError(f"R2 get failed for {key}: {exc}") from exc


# Singleton
r2_client = R2Client()
