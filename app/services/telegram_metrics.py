"""Telegram channel metrics auto-pull via the public t.me web view.

Why HTML scraping instead of MTProto / Bot API:

  - Bot API doesn't expose `message.views` for arbitrary message IDs after
    the send. `sendMessage` returns a Message without views, and there's
    no `getMessage(chat_id, message_id)` method (TG didn't ship it).
  - MTProto (Telethon/Pyrogram) gives us full message metadata including
    views, forwards, reactions, but requires a USER session — the founder's
    own Telegram account auth + 2FA + session file management. Too heavy
    for a self-host MVP, and a single banned phone number locks the whole
    pipeline.
  - Public web view (`t.me/<channel>/<msg_id>?embed=1`) renders the same
    counters Telegram shows in-app to anonymous web visitors. No auth, no
    rate-limit headers we've seen so far. Same Cloudflare-Worker proxy we
    already use for voice import handles RU-region outbound to t.me.

Caveats:

  - Only **public** channels with a handle have a web view. Private
    supergroups (chat_id like `-1001234567890` without a username) return
    404 — we mark such targets with `public_handle=None` in DB and skip.
  - View counts on the public page are bucketed ("1.2K", "5.6M") for posts
    over 1000 views. We parse the suffix and approximate — exact view
    counts aren't available from this surface. Within a single bucket the
    scraper returns the same value across a 6-hour cron cycle, which is
    fine for the founder's per-post comparison.
  - Reactions are shown on the embed page only when the post has at least
    one reaction. Missing reactions = `{}`, not "fetch failed".
  - The embed HTML structure is owned by Telegram and may change without
    notice. We log unparseable shape rather than crashing the worker, and
    fall back to `views=None` on parse failure.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


# Public-content user-agent — t.me embeds strip down to JSON-LD only for
# bare httpx UAs, but render full HTML for a realistic Safari signature.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)


# ---------------------------------------------------------------------------
# Number parsing — t.me renders "1.2K" / "5.6M" / "452" / "1 234"
# ---------------------------------------------------------------------------
_VIEWS_BUCKET_RE = re.compile(
    r"^\s*([\d.,]+)\s*([KMB])?\s*$", re.IGNORECASE
)


def parse_view_count(raw: str) -> int | None:
    """Convert t.me's bucketed view-count string into an integer.

    Examples::

        "452"      -> 452
        "1.2K"     -> 1200
        "5.6M"     -> 5_600_000
        "1 234"    -> 1234
        "1,234"    -> 1234
        ""         -> None
        "garbage"  -> None
    """
    raw = (raw or "").strip().replace(" ", " ")
    if not raw:
        return None
    # Plain integer with thousands separators (space, comma) — exact count.
    plain = raw.replace(" ", "").replace(",", "")
    if plain.isdigit():
        return int(plain)
    m = _VIEWS_BUCKET_RE.match(raw)
    if not m:
        return None
    number_str = m.group(1).replace(",", ".")
    try:
        n = float(number_str)
    except ValueError:
        return None
    suffix = (m.group(2) or "").upper()
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return int(round(n * mult))


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _proxied(url: str) -> str:
    """Same Cloudflare-Worker proxy as voice_importers — see that module."""
    base = settings.VOICE_FETCH_PROXY_URL.rstrip("/")
    if not base:
        return url
    return f"{base}/?url={quote(url, safe='')}"


def derive_handle(*, chat_id: str | None, public_handle: str | None) -> str | None:
    """Resolve a usable public channel handle (without @) for the scraper.

    Preference order:
      1. `public_handle` column on the target (explicit).
      2. `chat_id` when it starts with '@' (legacy / convenience).
      3. None — caller must skip metrics fetch (private channel).
    """
    if public_handle:
        h = public_handle.strip().lstrip("@")
        if h:
            return h
    if chat_id and chat_id.strip().startswith("@"):
        h = chat_id.strip().lstrip("@")
        if h:
            return h
    return None


# ---------------------------------------------------------------------------
# HTML parsing — selectors on the t.me embed widget
# ---------------------------------------------------------------------------


async def fetch_post_metrics(
    *,
    channel: str,
    message_id: int,
    timeout_seconds: float = 30.0,
) -> dict[str, Any] | None:
    """Fetch view/forward/reaction counts for a single channel post.

    Returns a dict matching `PublishLog.metrics` shape, or None if the post
    can't be reached (404 / blocked / parse failed). The 404 case is the
    common "you deleted the message" path, so a None return is informative
    rather than an error.
    """
    from bs4 import BeautifulSoup  # local import — keeps cold-start fast

    handle = channel.strip().lstrip("@")
    if not handle or message_id <= 0:
        return None

    url = _proxied(f"https://t.me/{handle}/{message_id}?embed=1&mode=tme")
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": _UA},
    ) as client:
        try:
            r = await client.get(url)
        except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
            logger.warning(
                "tg metrics unreachable for @%s/%s: %s", handle, message_id, exc
            )
            return None
        except httpx.HTTPError as exc:
            logger.warning(
                "tg metrics fetch failed for @%s/%s: %s", handle, message_id, exc
            )
            return None
    if r.status_code == 404:
        # Post deleted or channel gone. Caller can decide to clear the
        # `metrics` column or mark the post as deleted — for now we just
        # return None and the cron leaves the existing metrics in place.
        logger.info("tg metrics 404 for @%s/%s", handle, message_id)
        return None
    if r.status_code != 200:
        logger.warning(
            "tg metrics non-200 (%s) for @%s/%s", r.status_code, handle, message_id
        )
        return None

    soup = BeautifulSoup(r.text, "lxml")

    # The embed page may contain MULTIPLE message widgets when t.me decides
    # to show context (the requested post + N siblings). The widget for
    # *our* post is the one whose `data-post` attribute equals
    # `<handle>/<message_id>` — match exactly.
    target_data_post = f"{handle}/{message_id}"
    message_widget = soup.find(
        "div", attrs={"class": "tgme_widget_message", "data-post": target_data_post}
    )
    if message_widget is None:
        # Fallback: take the first message widget on the page. Common when
        # the embed?mode=tme view doesn't decorate the target attribute.
        message_widget = soup.find("div", class_="tgme_widget_message")
    if message_widget is None:
        logger.info("tg metrics: no message widget for @%s/%s", handle, message_id)
        return None

    views_node = message_widget.find(class_="tgme_widget_message_views")
    views = parse_view_count(views_node.get_text() if views_node else "")

    # Forwards aren't always shown on the embed page; when present, they
    # live next to views in a parallel <span>. We scrape best-effort.
    forwards = None
    forwards_node = message_widget.find(class_="tgme_widget_message_forwards")
    if forwards_node is not None:
        forwards = parse_view_count(forwards_node.get_text())

    # Reactions: each emoji + count lives in
    #   <span class="tgme_reaction_emoji">😀</span>
    #   <span class="tgme_reaction_counter">12</span>
    # within `.tgme_widget_message_reactions`. Counts use the same bucket
    # format as views.
    reactions: dict[str, int] = {}
    reactions_node = message_widget.find(class_="tgme_widget_message_reactions")
    if reactions_node is not None:
        for reaction in reactions_node.find_all(class_="tgme_reaction"):
            emoji_el = reaction.find(class_="tgme_reaction_emoji")
            counter_el = reaction.find(class_="tgme_reaction_counter")
            if not emoji_el or not counter_el:
                continue
            emoji = emoji_el.get_text(strip=True)
            count = parse_view_count(counter_el.get_text())
            if emoji and count is not None:
                reactions[emoji] = count

    return {
        "views": views,
        "forwards": forwards,
        "reactions": reactions,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "tme_web_view",
    }
