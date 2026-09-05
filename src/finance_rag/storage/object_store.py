"""Durable object storage for uploads and extracted media.

Uploads previously went to ``data/uploads`` on the task filesystem. On Fargate
that is ephemeral: the file disappears on redeploy, and with more than one task
the document is invisible to every task except the one that received it. A
document that only sometimes exists is worse than one that never did, because
retrieval succeeds intermittently.

S3 in production, local disk in development, chosen by configuration rather than
by branching at each call site.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class StoredObject:
    uri: str
    key: str
    size: int
    checksum: str

    @property
    def is_remote(self) -> bool:
        return self.uri.startswith("s3://")


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> StoredObject: ...
    def get(self, key: str) -> bytes: ...
    def download(self, key: str, destination: Path) -> Path: ...
    def exists(self, key: str) -> bool: ...


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_key(org_id: str, filename: str, data: bytes) -> str:
    """Content-addressed key, namespaced by tenant.

    Re-uploading the same bytes yields the same key, so a retried upload does not
    create a second copy that would then be indexed twice.
    """
    digest = _checksum(data)[:16]
    safe = Path(filename).name.replace("/", "_").replace("\\", "_") or "upload.bin"
    return f"{org_id}/uploads/{digest}/{safe}"


class LocalObjectStore:
    """Filesystem-backed store for development."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        root = self.root.resolve()
        # A key arriving from a filename must not escape the store.
        if not str(path).startswith(str(root)):
            raise ValueError(f"key escapes storage root: {key!r}")
        return path

    def put(self, key: str, data: bytes) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StoredObject(
            uri=path.as_uri(), key=key, size=len(data), checksum=_checksum(data)
        )

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def download(self, key: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._path(key), destination)
        return destination

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class S3ObjectStore:
    """S3-backed store for deployed environments."""

    def __init__(self, bucket: str, prefix: str = "", region: str | None = None) -> None:
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client("s3", region_name=region)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes) -> StoredObject:
        full = self._key(key)
        self._client.put_object(
            Bucket=self.bucket,
            Key=full,
            Body=data,
            # Server-side encryption is not optional for client tax documents.
            ServerSideEncryption="AES256",
        )
        logger.info("object_stored", bucket=self.bucket, key=full, size=len(data))
        return StoredObject(
            uri=f"s3://{self.bucket}/{full}",
            key=key,
            size=len(data),
            checksum=_checksum(data),
        )

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()

    def download(self, key: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self.bucket, self._key(key), str(destination))
        return destination

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError:
            return False


def build_object_store() -> ObjectStore:
    """Select a backend from configuration.

    Falls back to local storage when no bucket is configured, so development
    needs no AWS credentials -- but a deployment without a bucket silently
    losing uploads is exactly the failure this module exists to prevent, so the
    fallback is logged as a warning outside development.
    """
    settings = get_settings()
    if settings.s3_bucket:
        return S3ObjectStore(
            bucket=settings.s3_bucket,
            prefix=settings.s3_prefix,
            region=settings.aws_region,
        )
    if settings.app_env != "development":
        logger.warning(
            "object_store_local_in_deployment",
            reason="S3_BUCKET unset; uploads will not survive a redeploy",
        )
    return LocalObjectStore(settings.upload_dir)


def read_into(store: ObjectStore, obj: StoredObject, workdir: Path) -> Path:
    """Materialise an object locally so parsers that need a real path can run.

    pypdf and pdfplumber both want a filesystem path, so a remote object is
    staged into the working directory rather than parsed from memory.
    """
    target = workdir / Path(obj.key).name
    if isinstance(store, LocalObjectStore):
        return store._path(obj.key)
    return store.download(obj.key, target)
