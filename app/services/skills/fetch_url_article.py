"""Skill: fetch the main article body from a public URL into a source node.

Powers the SourceNode's URL tab. Users paste a blog/article link (e.g.
zamesin.ru/foo, paulgraham.com/bar, a Notion-style essay) and the skill
runs trafilatura to extract the cleaned text. The result lands in
`node.data.content` so downstream extract/format nodes can consume it
exactly like a YouTube transcript or pasted text.

Why a dedicated skill and not lazy fetch inside the extract step:
    1. Visibility — the source node shows a status dot + spinner during
       the fetch, just like YouTube/audio sources. Errors surface in the
       source where they happened, not buried inside an extract failure.
    2. Caching — once fetched, the content is persisted on the node, so
       re-running extract doesn't re-hit the URL (and doesn't get a
       different result if the page changed).
    3. Quotas — the run is tracked as a SkillRun the same as any other,
       so preview/trial gating naturally applies.

Why not reuse `voice_importers.fetch_url_article`:
    That helper is tuned for VOICE samples: caps at 6K chars, drops sub-80
    char results, etc. For content ingestion we want the full article and
    a useful error message when extraction fails.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.services.skills.base import register

logger = logging.getLogger(__name__)


# Hard cap: viral_talking_points truncates source content to 24K chars,
# so anything past ~30K is unused budget for the dominant downstream
# consumer. We leave a little headroom for the user to see the raw text
# in the source-node preview without ever pushing nonsense into the LLM.
_MAX_CONTENT_CHARS = 30_000


@register("fetch_url_article")
async def run(
    db: AsyncSession,
    node: Node,
    system_context: str,
    skill_input: dict[str, Any],
) -> dict[str, Any]:
    raw_url = (skill_input.get("url") or "").strip()
    if not raw_url:
        raise ValueError("URL не передан")

    parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    if not parsed.netloc or parsed.scheme not in ("http", "https"):
        raise ValueError("Это не похоже на корректный URL")
    # Re-normalize in case the user pasted a bare domain.
    url = parsed.geturl()

    extracted, page_title = await asyncio.to_thread(_extract_sync, url)
    if not extracted:
        raise ValueError(
            "Не удалось извлечь текст со страницы. Возможно, она требует "
            "JavaScript / логин или блокирует ботов. Скопируй текст вручную "
            "и вставь во вкладку «Текст»."
        )

    truncated = extracted[:_MAX_CONTENT_CHARS]

    new_data = dict(node.data or {})
    new_data.update(
        {
            "input_type": "url",
            "url": url,
            "content": truncated,
            "url_title": page_title,
            "url_host": parsed.netloc,
            "url_chars": len(truncated),
        }
    )
    return {
        "node_data": new_data,
        "meta": {
            "chars": len(truncated),
            "host": parsed.netloc,
            "truncated": len(extracted) > _MAX_CONTENT_CHARS,
        },
    }


def _extract_sync(url: str) -> tuple[str | None, str | None]:
    """Blocking trafilatura fetch+extract; called via to_thread."""
    import trafilatura

    try:
        downloaded = trafilatura.fetch_url(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trafilatura fetch failed for %s: %s", url, exc)
        return None, None
    if not downloaded:
        return None, None

    title: str | None = None
    try:
        meta = trafilatura.extract_metadata(downloaded)
        if meta is not None:
            title = getattr(meta, "title", None) or None
    except Exception:  # noqa: BLE001
        # Metadata extraction is best-effort; title is non-critical.
        pass

    try:
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("trafilatura extract failed for %s: %s", url, exc)
        return None, title
    if not text:
        return None, title

    # Normalize whitespace inline — short helper not worth a shared import.
    import re as _re
    text = _re.sub(r"[ \t]+", " ", text).strip()
    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text, title
