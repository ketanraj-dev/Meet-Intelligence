"""Thin wrapper over an S3-compatible object store (MinIO locally, S3/R2 in prod)."""
from pathlib import Path

import boto3
from botocore.client import Config

from common.config import Settings, get_settings


class StorageClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        scheme = "https" if self.settings.minio_secure else "http"
        self.bucket = self.settings.minio_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=f"{scheme}://{self.settings.minio_endpoint}",
            aws_access_key_id=self.settings.minio_access_key,
            aws_secret_access_key=self.settings.minio_secret_key,
            region_name="us-east-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def ensure_bucket(self) -> None:
        existing = {b["Name"] for b in self._client.list_buckets().get("Buckets", [])}
        if self.bucket not in existing:
            self._client.create_bucket(Bucket=self.bucket)

    def upload_file(self, local_path: str | Path, key: str) -> None:
        self._client.upload_file(str(local_path), self.bucket, key)

    def upload_bytes(self, data: bytes, key: str) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def download_file(self, key: str, local_path: str | Path) -> None:
        self._client.download_file(self.bucket, key, str(local_path))

    def list_objects(self, prefix: str) -> list[str]:
        """All keys under prefix, in whatever order S3 returns them (not
        necessarily sorted) — callers that need ordering (e.g. worker/
        diarization.py concatenating audio_00000.wav, audio_00001.wav, ...
        in order) must sort the result themselves."""
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in,
        )
