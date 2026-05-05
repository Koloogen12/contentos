"""Object storage abstraction.

In dev / when S3 is unconfigured, files go to settings.TEMP_DIR on disk.
In prod we write to Selectel S3 via boto3. The interface is the same:
    save_upload(stream, suggested_name) -> StoragePath
    open_for_read(path) -> async file-like (or local Path)
StoragePath is a string of the form "local://..." or "s3://bucket/key" so
downstream code can branch only when it actually needs the bytes.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import BinaryIO

import boto3
from botocore.client import Config as BotoConfig

from app.config import settings


def _local_root() -> Path:
    p = Path(settings.TEMP_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _s3_configured() -> bool:
    return bool(settings.S3_ENDPOINT_URL and settings.S3_ACCESS_KEY and settings.S3_SECRET_KEY)


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=BotoConfig(signature_version="s3v4"),
    )


def save_upload(stream: BinaryIO, suggested_name: str) -> str:
    """Persist an uploaded file. Returns a storage path the worker can resolve."""
    safe_name = Path(suggested_name).name or "upload.bin"
    key = f"uploads/{uuid.uuid4().hex}-{safe_name}"

    if _s3_configured():
        _s3_client().upload_fileobj(stream, settings.S3_BUCKET, key)
        return f"s3://{settings.S3_BUCKET}/{key}"

    dest = _local_root() / key.replace("/", "_")
    with dest.open("wb") as f:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    return f"local://{dest}"


def resolve_to_local(path: str) -> Path:
    """Materialize a storage path on the local filesystem so libraries that
    only know how to read from disk (pydub, whisper) can work with it.

    For local:// paths this is a no-op. For s3:// paths we download to
    settings.TEMP_DIR. Caller is responsible for cleanup if it cares.
    """
    if path.startswith("local://"):
        return Path(path[len("local://"):])
    if path.startswith("s3://"):
        without_scheme = path[len("s3://"):]
        bucket, _, key = without_scheme.partition("/")
        local = _local_root() / f"s3-{uuid.uuid4().hex}-{Path(key).name}"
        _s3_client().download_file(bucket, key, str(local))
        return local
    raise ValueError(f"Unknown storage scheme: {path}")


def cleanup(path: str) -> None:
    if path.startswith("local://"):
        try:
            os.unlink(path[len("local://"):])
        except OSError:
            pass
