"""Auto-import helpers for voice training.

Pulls the founder's existing public content from three free-tier sources
so we can seed the voice_samples table without making them paste 10 posts
by hand:

1. Telegram public channel — via the public web-view `https://t.me/s/<handle>`
   (no bot setup, no MTProto auth, just an HTML scrape of the open channel
   widget). Returns the last N posts that are reasonable in length.
2. YouTube channel — via yt-dlp (flat extraction of the channel uploads
   playlist) + our existing `transcription.youtube.transcribe` (captions
   first, Whisper fallback). Each video becomes one voice_sample.
3. Arbitrary blog URL — via trafilatura for clean article-body extraction
   that strips nav, footer, ads. One voice_sample per URL.

Each helper returns a list of plain strings (clean text) so the caller can
embed + write to `voice_samples` in a transaction without leaking
provider-specific shapes.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def _proxied(url: str) -> str:
    """Wrap a target URL via the configured Cloudflare fetch-proxy.

    If `VOICE_FETCH_PROXY_URL` is empty, returns the URL unchanged (local
    dev / unblocked hosts). Otherwise emits
    `<proxy>?url=<percent-encoded-target>`.
    """
    base = settings.VOICE_FETCH_PROXY_URL.rstrip("/")
    if not base:
        return url
    return f"{base}/?url={quote(url, safe='')}"

# Public-content user-agent. Some hosts (notably Telegram's t.me/s) gate
# on a real-looking UA — a bare `httpx/0.28` gets you a stripped page.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)

_MIN_SAMPLE_CHARS = 80  # filter out tiny captions, single emoji, etc.
_MAX_SAMPLE_CHARS = 6000  # one big tg post / one yt transcript chunk


# ---------------------------------------------------------------------------
# Telegram public channel via web-view
# ---------------------------------------------------------------------------

_TG_TEXT_SELECTOR = "div.tgme_widget_message_text"


async def fetch_telegram_channel_posts(handle: str, limit: int = 50) -> list[str]:
    """Scrape the last `limit` text posts from a public Telegram channel.

    `handle` accepts `@kochnefff`, `kochnefff`, `https://t.me/kochnefff`.
    Returns posts ordered newest-first. Filters out very short messages
    (single emoji reactions, short forwards) — they're noise for voice
    training. If the channel is private or non-existent returns [].
    """
    from bs4 import BeautifulSoup

    h = handle.strip().lstrip("@")
    if h.startswith("https://t.me/"):
        h = h.removeprefix("https://t.me/")
    if h.startswith("t.me/"):
        h = h.removeprefix("t.me/")
    h = h.split("/")[0].strip()
    if not h:
        return []

    url = _proxied(f"https://t.me/s/{h}")
    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, headers={"User-Agent": _UA}
    ) as client:
        try:
            r = await client.get(url)
        except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
            # Either outbound to t.me is blocked (no proxy configured) OR
            # the proxy itself is unreachable. Surface distinctly so the
            # caller can return a friendly note instead of "channel empty".
            logger.warning("tg unreachable for @%s: %s", h, exc)
            raise RuntimeError("tg-unreachable") from exc
        except httpx.HTTPError as exc:
            logger.warning("tg fetch failed for @%s: %s", h, exc)
            return []
    if r.status_code != 200:
        logger.info("tg web-view returned %s for @%s", r.status_code, h)
        return []

    # If the channel is private, t.me/s/ returns 200 with a stub page that
    # has no .tgme_widget_message_text nodes — handled implicitly below.
    soup = BeautifulSoup(r.text, "html.parser")
    posts: list[str] = []
    seen: set[str] = set()
    # Web-view renders oldest → newest in the DOM; reverse so the caller
    # sees newest first (matches what a human checks).
    for node in reversed(soup.select(_TG_TEXT_SELECTOR)):
        # `get_text` with separator preserves paragraph breaks roughly.
        text = node.get_text("\n", strip=True)
        text = _normalize_whitespace(text)
        if not text or text in seen:
            continue
        if len(text) < _MIN_SAMPLE_CHARS or len(text) > _MAX_SAMPLE_CHARS:
            continue
        seen.add(text)
        posts.append(text)
        if len(posts) >= limit:
            break
    return posts


# ---------------------------------------------------------------------------
# YouTube channel — videos → transcripts
# ---------------------------------------------------------------------------

# Channel URL accepts handle (@danil), legacy /c/, legacy /user/, /channel/UC...
def _normalize_youtube_channel(url_or_handle: str) -> str:
    s = url_or_handle.strip()
    if s.startswith("@"):
        return f"https://www.youtube.com/{s}/videos"
    if s.startswith("UC") and len(s) >= 20 and "/" not in s:
        return f"https://www.youtube.com/channel/{s}/videos"
    if s.startswith("http://") or s.startswith("https://"):
        # Strip query/anchor so flat extract sees the channel page cleanly.
        s = s.split("?")[0].split("#")[0].rstrip("/")
        if not s.endswith("/videos"):
            s = s + "/videos"
        return s
    # Bare word → treat as a handle.
    return f"https://www.youtube.com/@{s}/videos"


async def fetch_youtube_channel_videos(
    channel: str, limit: int = 10
) -> list[str]:
    """Return up to `limit` video IDs from a channel's uploads, newest first."""
    import yt_dlp

    url = _normalize_youtube_channel(channel)

    def _run() -> list[str]:
        ydl_opts = {
            "quiet": True,
            "extract_flat": True,
            "playlistend": limit,
            "skip_download": True,
            "noprogress": True,
            "no_warnings": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("yt channel scan failed for %s: %s", url, exc)
            return []
        entries = info.get("entries") or []
        ids: list[str] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            vid = e.get("id")
            if vid:
                ids.append(vid)
            if len(ids) >= limit:
                break
        return ids

    return await asyncio.to_thread(_run)


async def fetch_youtube_video_transcript(video_id: str) -> str | None:
    """Reuse the canvas transcription path on a bare video_id.

    Returns the transcript text (captions preferred, Whisper fallback) or
    None if neither succeeded. We don't surface Whisper errors here — for
    voice training, transcript-via-captions is enough; if captions are
    missing on every channel video we'd rather skip than slow the import
    by 10x doing audio downloads.
    """
    from app.services.transcription.youtube import _captions_text

    chunks = await asyncio.to_thread(
        _captions_text, video_id, ["ru", "en", "uk", "kk"]
    )
    if chunks is None:
        return None
    text, _lang = chunks
    text = text.strip()
    if not text:
        return None
    return text


# ---------------------------------------------------------------------------
# Arbitrary blog URL — main body via trafilatura
# ---------------------------------------------------------------------------

async def fetch_url_article(url: str) -> str | None:
    """Pull the main article body from a public URL. Strips nav/footer/ads.

    Returns None if nothing usable extracted (link rot, login wall, JS-only
    SPA without server-side render). Caller decides whether to ask the user
    to paste manually.
    """
    import trafilatura

    def _run() -> str | None:
        try:
            downloaded = trafilatura.fetch_url(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("trafilatura fetch failed for %s: %s", url, exc)
            return None
        if not downloaded:
            return None
        try:
            extracted = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=False,
                favor_recall=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("trafilatura extract failed for %s: %s", url, exc)
            return None
        if not extracted:
            return None
        extracted = _normalize_whitespace(extracted)
        if len(extracted) < _MIN_SAMPLE_CHARS:
            return None
        # If the page contains a wall-of-text essay, slice to a reasonable
        # sample — voice retrieval cares more about style fingerprints than
        # full-document recall.
        return extracted[:_MAX_SAMPLE_CHARS]

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

_WS_COLLAPSE = re.compile(r"[ \t]+")
_BLANK_RUN = re.compile(r"\n{3,}")


def _normalize_whitespace(text: str) -> str:
    text = _WS_COLLAPSE.sub(" ", text or "")
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Convenience meta type returned to callers
# ---------------------------------------------------------------------------

class ImportResult(dict[str, Any]):
    """Plain dict alias for type readability in routers."""
