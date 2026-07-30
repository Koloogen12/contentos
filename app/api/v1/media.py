"""Public media redirector: `/api/v1/media/<key>` → 302 → presigned S3 URL.

Selectel's "Публичный" bucket type silently ignores S3 bucket policies
and `x-amz-acl=public-read`; the only way to expose objects for
anonymous browser reads is via Presigned URLs. This redirector mints a
fresh 1-hour presigned URL on every hit and bounces the client to it,
so:

  - We can keep posting `https://draft.neurin.tech/api/v1/media/...`
    URLs everywhere (no expiring URLs baked into post text / DB rows).
  - Switching to a real public CDN (selcdn.ru, Cloudflare R2, etc.)
    later is a 1-line `_public_url_for` change.
  - LinkedIn / IG publish APIs that demand HTTPS URLs see a permanent-
    looking URL on our own domain — they fetch through it.

Endpoint is intentionally unauthenticated: carousel slide JPEGs are
already meant to be public (you'll be posting them to Instagram). If we
ever store *private* assets in the same bucket, gate the redirector
behind auth or use a separate bucket.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse

from app.services import storage

router = APIRouter(tags=["media"])


@router.get("/media/{key:path}")
async def get_media(key: str, download: str | None = None):
    """Redirect to a freshly-presigned S3 URL for `key`.

    Path parameter is `{key:path}` (Starlette wildcard) so multi-segment
    keys like `renders/carousel/<node_id>/<render_id>/01.jpg` work
    without URL-encoding the slashes. We strip leading slashes to be
    forgiving with caller URL construction.

    Query params:
      - `download=<filename>` — request the underlying S3 object be served
        with `Content-Disposition: attachment; filename=<filename>`. The
        browser then triggers a save dialog even though S3 itself is a
        cross-origin host. Without this flag the object is served inline
        (image preview / `<img src>` rendering).

    302 cache headers: short TTL so browsers re-hit us before the
    presigned URL would expire. Cache-Control on download responses is
    zeroed — we don't want a download attempt to be served from a
    previously-cached inline redirect.
    """
    key = key.lstrip("/")
    if not key or ".." in key:
        # Defensive: keys are emitted by us, so traversal patterns are
        # never legitimate. Refuse with 400 to keep the redirector from
        # being abused as a generic S3 oracle.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Invalid media key"
        )

    # When `download` is passed without a value (e.g. `?download`), FastAPI
    # gives us an empty string. Treat any truthy value as "yes, force
    # attachment" and derive the filename from the key.
    download_filename: str | None = None
    if download is not None:
        # Use the caller-provided filename if it's non-trivial, else
        # fall back to the last path segment of `key`.
        proposed = download.strip()
        if proposed and proposed not in ("1", "true", "yes"):
            download_filename = proposed
        else:
            download_filename = key.rsplit("/", 1)[-1] or "download.bin"

    presigned = storage.presign_get_url(
        key, expires_in=3600, download_filename=download_filename
    )
    response = RedirectResponse(url=presigned, status_code=302)
    if download_filename:
        response.headers["Cache-Control"] = "no-store"
    else:
        response.headers["Cache-Control"] = "public, max-age=300"
    return response
