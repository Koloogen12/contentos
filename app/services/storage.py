"""Object storage abstraction.

In dev / when S3 is unconfigured, files go to settings.TEMP_DIR on disk.
In prod we write to Selectel S3 via boto3. The interface is the same:
    save_upload(stream, suggested_name) -> StoragePath
    open_for_read(path) -> async file-like (or local Path)
StoragePath is a string of the form "local://..." or "s3://bucket/key" so
downstream code can branch only when it actually needs the bytes.

For *public* assets (rendered carousel slides, AI covers) use
`save_public_bytes()` — it sets a public-read ACL and returns an HTTPS URL
that can be embedded in posts and fed to LinkedIn/IG publish APIs. When
S3 is unconfigured we fall back to writing into TEMP_DIR/public/ and the
returned URL is a local file:// path that the dev server should serve via
a static-files mount (TODO: wire that in main.py if local-FS rendering is
needed in dev).
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlparse

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


def _public_url_for(key: str) -> str:
    """Return an URL that resolves to the stored object for unauthenticated readers.

    Selectel S3 ignores both bucket policy `Allow s3:GetObject for *` and
    per-object `x-amz-acl=public-read` — "Публичный тип" of a bucket only
    enables Presigned URL access, not anonymous direct reads. To work
    around that we don't hand out raw S3 URLs; instead callers get a URL
    pointing at our own `/api/v1/media/<key>` redirector, which mints a
    fresh presigned S3 URL on each hit and 302s the client to it.

    The redirector lives on the same host as the rest of the API. We pull
    the public host from `S3_PUBLIC_URL_BASE` if set (lets you point at
    a CDN later), otherwise we fall back to the first CORS origin (which
    is the frontend host in dev / prod).
    """
    base = (
        settings.S3_PUBLIC_URL_BASE.rstrip("/")
        if settings.S3_PUBLIC_URL_BASE
        else (
            settings.cors_origins_list[0].rstrip("/")
            if settings.cors_origins_list
            else ""
        )
    )
    return f"{base}/api/v1/media/{key}"


def presign_get_url(
    key: str,
    *,
    expires_in: int = 3600,
    download_filename: str | None = None,
) -> str:
    """Mint a short-lived presigned URL for a single object.

    Used by the `/api/v1/media/<key>` redirector so unauthenticated
    browser readers can fetch carousel slides through us without
    Selectel rejecting the request.

    When `download_filename` is set, we ask S3 to serve the object with
    `Content-Disposition: attachment; filename=<filename>` via the
    standard `response-content-disposition` signed query param. That
    makes the browser trigger a download even though the object's host
    (Selectel S3) doesn't share our origin's CORS — `<a download>` and
    `window.location` honour `attachment` headers regardless of CORS.
    """
    params: dict[str, Any] = {"Bucket": settings.S3_BUCKET, "Key": key}
    if download_filename:
        # RFC 6266 filename* with UTF-8 to handle Cyrillic / spaces safely.
        from urllib.parse import quote as _quote
        encoded = _quote(download_filename, safe="")
        params["ResponseContentDisposition"] = (
            f"attachment; filename=\"{download_filename}\"; filename*=UTF-8''{encoded}"
        )
    return _s3_client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=expires_in,
    )


def save_public_bytes(
    content: bytes,
    *,
    key_prefix: str,
    suggested_name: str,
    content_type: str = "application/octet-stream",
    cache_seconds: int = 60 * 60 * 24 * 30,  # 30 days
) -> str:
    """Persist `content` as a publicly-readable object and return its URL.

    `key_prefix` is the logical folder ("renders/carousel/<node_id>"), the
    final key gets a uuid hash so re-renders never overwrite the previous
    version (we want to keep variants discoverable for tweak history).

    Returns an HTTPS URL when S3 is configured, otherwise a `local://` path
    pointing into TEMP_DIR/public/ for dev. Callers that publish to social
    APIs must validate the result starts with `https://`.
    """
    safe_name = Path(suggested_name).name or "asset.bin"
    key = f"{key_prefix.strip('/')}/{uuid.uuid4().hex}-{safe_name}"

    if _s3_configured():
        _s3_client().put_object(
            Bucket=settings.S3_BUCKET,
            Key=key,
            Body=content,
            ContentType=content_type,
            ACL="public-read",
            CacheControl=f"public, max-age={cache_seconds}, immutable",
        )
        return _public_url_for(key)

    public_dir = _local_root() / "public" / key_prefix.strip("/")
    public_dir.mkdir(parents=True, exist_ok=True)
    dest = public_dir / f"{uuid.uuid4().hex}-{safe_name}"
    dest.write_bytes(content)
    return f"local://{dest}"
