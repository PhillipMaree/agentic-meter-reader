"""Generic S3 wrapper — mirrors `src/utils/sql.py`'s shape.

Business-logic agnostic. Points at the SeaweedFS S3 endpoint in the
agentic-enterprise stack (`localhost:18333` from the host,
`seaweedfs:8333` on the platform network).

Keys conventionally start with ``<agent>/`` so that multiple agents can
share one bucket without collisions.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

from src.utils import S3Settings, settings

log = logging.getLogger(__name__)


class S3:
    """Thin wrapper around ``boto3.client('s3')`` for SeaweedFS / MinIO / AWS S3."""

    def __init__(self, cfg: S3Settings):
        self._client = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint,
            region_name=cfg.region or "us-east-1",
            aws_access_key_id=cfg.access_key.get_secret_value(),
            aws_secret_access_key=cfg.secret_key.get_secret_value(),
        )
        self._bucket = cfg.bucket

    @property
    def bucket(self) -> str:
        return self._bucket

    def ensure_bucket(self) -> None:
        """Create the configured bucket if it doesn't already exist. Idempotent."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchBucket"):
                self._client.create_bucket(Bucket=self._bucket)
                log.info("created s3 bucket %r", self._bucket)
            else:
                raise

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=body, ContentType=content_type
        )

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def list(self, prefix: str) -> list[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    def presigned_url(self, key: str, expires: int = 3600) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires,
        )


@lru_cache(maxsize=1)
def s3_client() -> S3:
    """Return the process-wide ``S3`` instance, configured from ``settings.s3``."""
    return S3(settings.s3)
