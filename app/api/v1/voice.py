"""Voice samples: store the user's own posts for few-shot retrieval at format time."""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.knowledge import BrandContext, VoiceSample
from app.schemas.voice import (
    TelegramImportRequest,
    UrlImportRequest,
    VoiceImportResult,
    VoiceSampleBulkCreate,
    VoiceSampleBulkResult,
    VoiceSampleCreate,
    VoiceSampleOut,
    VoiceTraitsExtracted,
    YoutubeImportRequest,
)
from app.services import ai_client, voice_importers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice-samples", tags=["voice"])


def _to_out(sample: VoiceSample) -> VoiceSampleOut:
    return VoiceSampleOut(
        id=sample.id,
        organization_id=sample.organization_id,
        project_id=sample.project_id,
        platform=sample.platform,
        text=sample.text,
        meta=sample.meta or {},
        has_embedding=sample.embedding is not None,
        created_at=sample.created_at,
        updated_at=sample.updated_at,
    )


async def _embed_safely(text: str) -> list[float] | None:
    try:
        return await ai_client.embed(text[:4000])
    except Exception:
        logger.exception("embed failed; storing sample without embedding")
        return None


@router.get("", response_model=list[VoiceSampleOut])
async def list_samples(current: CurrentUser, db: DbSession) -> list[VoiceSampleOut]:
    rows = await db.scalars(
        select(VoiceSample)
        .where(VoiceSample.organization_id == current.organization_id)
        .order_by(VoiceSample.created_at.desc())
    )
    return [_to_out(r) for r in rows.all()]


@router.post("", response_model=VoiceSampleOut, status_code=status.HTTP_201_CREATED)
async def create_sample(
    payload: VoiceSampleCreate, current: CurrentUser, db: DbSession
) -> VoiceSampleOut:
    embedding = await _embed_safely(payload.text)
    obj = VoiceSample(
        organization_id=current.organization_id,
        project_id=payload.project_id,
        platform=payload.platform,
        text=payload.text,
        embedding=embedding,
        meta=payload.meta,
    )
    db.add(obj)
    await db.flush()
    return _to_out(obj)


@router.post("/bulk", response_model=VoiceSampleBulkResult, status_code=status.HTTP_201_CREATED)
async def create_bulk(
    payload: VoiceSampleBulkCreate, current: CurrentUser, db: DbSession
) -> VoiceSampleBulkResult:
    created: list[VoiceSampleOut] = []
    skipped = 0
    for item in payload.samples:
        if len(item.text.strip()) < 20:
            skipped += 1
            continue
        embedding = await _embed_safely(item.text)
        obj = VoiceSample(
            organization_id=current.organization_id,
            project_id=item.project_id,
            platform=item.platform,
            text=item.text,
            embedding=embedding,
            meta=item.meta,
        )
        db.add(obj)
        await db.flush()
        created.append(_to_out(obj))
    return VoiceSampleBulkResult(created=len(created), skipped=skipped, items=created)


@router.delete("/{sample_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sample(sample_id: uuid.UUID, current: CurrentUser, db: DbSession):
    obj = await db.scalar(
        select(VoiceSample).where(
            VoiceSample.id == sample_id, VoiceSample.organization_id == current.organization_id
        )
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sample not found")
    await db.delete(obj)


# ---------------------------------------------------------------------------
# Auto-import — free-tier sources (Telegram public / YouTube / blog URL)
# ---------------------------------------------------------------------------

async def _persist_imported(
    db,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID | None,
    platform: str,
    texts: list[str],
    meta_factory,
    source: str,
) -> VoiceImportResult:
    """Common path for the three importers: dedup against existing samples
    (cheap exact-prefix check), embed each new text, write to DB.

    `meta_factory(index, text) -> dict` lets each caller attach
    source-specific fields (tg post URL, yt video_id, blog URL).
    """
    # Cheap dedup: pull the first 200 chars of every existing sample for
    # this org and skip imports that start with the same prefix. Avoids
    # users re-running import and doubling their voice corpus.
    existing_prefixes: set[str] = set()
    rows = await db.scalars(
        select(VoiceSample.text).where(VoiceSample.organization_id == organization_id)
    )
    for t in rows.all():
        if t:
            existing_prefixes.add(t[:200].strip())

    created: list[VoiceSampleOut] = []
    skipped = 0
    notes: list[str] = []
    for i, text in enumerate(texts):
        t = (text or "").strip()
        if len(t) < 60:
            skipped += 1
            continue
        if t[:200].strip() in existing_prefixes:
            skipped += 1
            continue
        embedding = await _embed_safely(t)
        obj = VoiceSample(
            organization_id=organization_id,
            project_id=project_id,
            platform=platform,
            text=t,
            embedding=embedding,
            meta=meta_factory(i, t),
        )
        db.add(obj)
        await db.flush()
        created.append(_to_out(obj))
        existing_prefixes.add(t[:200].strip())

    if not created:
        notes.append(
            "Ничего нового не добавлено — либо источник пуст, либо все посты "
            "уже импортированы ранее."
        )
    return VoiceImportResult(
        source=source,
        created=len(created),
        skipped=skipped,
        items=created,
        notes=notes,
    )


@router.post(
    "/import/telegram",
    response_model=VoiceImportResult,
    status_code=status.HTTP_201_CREATED,
)
async def import_from_telegram(
    payload: TelegramImportRequest, current: CurrentUser, db: DbSession
) -> VoiceImportResult:
    """Stream the last N posts from a public TG channel into voice_samples.

    Uses `https://t.me/s/<handle>` web-view — no bot setup, no MTProto,
    just an HTML scrape of the open channel widget. Private channels and
    typo'd handles return 0 created with a friendly note.
    """
    handle = payload.handle.strip()
    try:
        posts = await voice_importers.fetch_telegram_channel_posts(
            handle, limit=payload.limit
        )
    except RuntimeError as exc:
        if "tg-unreachable" in str(exc):
            return VoiceImportResult(
                source="telegram",
                created=0,
                skipped=0,
                items=[],
                notes=[
                    "Сервер не может достучаться до t.me — у хостера заблокирован "
                    "исходящий трафик к Telegram. Пока используй ручной paste либо "
                    "YouTube / Blog импорт. Чиним отдельной задачей через прокси."
                ],
            )
        raise
    if not posts:
        return VoiceImportResult(
            source="telegram",
            created=0,
            skipped=0,
            items=[],
            notes=[
                f"Не удалось вытащить посты из канала «{handle}». "
                "Проверь что канал публичный и handle правильный."
            ],
        )

    handle_clean = handle.lstrip("@").rstrip("/")
    return await _persist_imported(
        db,
        organization_id=current.organization_id,
        project_id=payload.project_id,
        platform="telegram",
        texts=posts,
        meta_factory=lambda i, _t: {
            "source": "telegram",
            "channel": handle_clean,
            "import_index": i,
        },
        source="telegram",
    )


@router.post(
    "/import/youtube",
    response_model=VoiceImportResult,
    status_code=status.HTTP_201_CREATED,
)
async def import_from_youtube(
    payload: YoutubeImportRequest, current: CurrentUser, db: DbSession
) -> VoiceImportResult:
    """Pull captions for the last N videos of a channel into voice_samples.

    Captions only — Whisper fallback would 10x the import time and most
    founder channels have captions enabled.
    """
    video_ids = await voice_importers.fetch_youtube_channel_videos(
        payload.channel, limit=payload.limit
    )
    if not video_ids:
        return VoiceImportResult(
            source="youtube",
            created=0,
            skipped=0,
            items=[],
            notes=[
                "YouTube не отдал список видео. Убедись что канал публичный "
                "и проверь правильность handle / ссылки."
            ],
        )

    # Pull all transcripts in parallel — they're independent network calls.
    transcripts = await asyncio.gather(
        *[voice_importers.fetch_youtube_video_transcript(v) for v in video_ids],
        return_exceptions=False,
    )

    paired = [
        (vid, t) for vid, t in zip(video_ids, transcripts) if t and len(t) >= 200
    ]
    texts = [t for _, t in paired]
    vid_for_index = [vid for vid, _ in paired]

    notes: list[str] = []
    missing = len(video_ids) - len(paired)
    if missing > 0:
        notes.append(
            f"У {missing} из {len(video_ids)} видео не нашлось субтитров — "
            "пропустил их."
        )

    result = await _persist_imported(
        db,
        organization_id=current.organization_id,
        project_id=payload.project_id,
        platform="youtube",
        texts=texts,
        meta_factory=lambda i, _t: {
            "source": "youtube",
            "video_id": vid_for_index[i] if i < len(vid_for_index) else None,
            "url": f"https://youtu.be/{vid_for_index[i]}"
            if i < len(vid_for_index) else None,
        },
        source="youtube",
    )
    result.notes = notes + result.notes
    return result


@router.post(
    "/import/url",
    response_model=VoiceImportResult,
    status_code=status.HTTP_201_CREATED,
)
async def import_from_url(
    payload: UrlImportRequest, current: CurrentUser, db: DbSession
) -> VoiceImportResult:
    """Extract article bodies from public URLs into voice_samples.

    Uses trafilatura to strip nav/footer/ads. One sample per successful URL.
    """
    bodies = await asyncio.gather(
        *[voice_importers.fetch_url_article(u) for u in payload.urls],
        return_exceptions=False,
    )
    paired = [
        (u, b) for u, b in zip(payload.urls, bodies) if b and len(b) >= 200
    ]
    texts = [b for _, b in paired]
    urls_for_index = [u for u, _ in paired]

    notes: list[str] = []
    missing = len(payload.urls) - len(paired)
    if missing > 0:
        notes.append(
            f"С {missing} ссылок не получилось вытащить текст — "
            "часто это SPA-страницы или login-wall."
        )

    result = await _persist_imported(
        db,
        organization_id=current.organization_id,
        project_id=payload.project_id,
        platform="article",
        texts=texts,
        meta_factory=lambda i, _t: {
            "source": "url",
            "url": urls_for_index[i] if i < len(urls_for_index) else None,
        },
        source="url",
    )
    result.notes = notes + result.notes
    return result


_EXTRACT_SYSTEM = """\
Ты лингвист-аналитик. На входе — посты одного автора. Извлеки его авторский голос:
- voice_traits: 5–10 коротких маркеров стиля автора (фразы-маркеры, синтаксис, структура).
- voice_avoid: 5–10 паттернов которых автор ИЗБЕГАЕТ (что в его постах не встречается, чего читатель не увидит).
- recurring_phrases: до 10 фраз/слов которые повторяются у автора.
- tone_calibration: одно предложение про тон («жёсткий / мягкий», «академичный / разговорный», «личный / отстранённый» и т.д.).

Ответ строго JSON:
{
  "voice_traits": ["..."],
  "voice_avoid": ["..."],
  "recurring_phrases": ["..."],
  "tone_calibration": "..."
}"""


@router.post("/extract-traits", response_model=VoiceTraitsExtracted)
async def extract_traits(current: CurrentUser, db: DbSession) -> VoiceTraitsExtracted:
    """Analyse all of this org's voice samples → write voice_traits/voice_avoid/etc into BrandContext."""
    samples = list(
        (
            await db.scalars(
                select(VoiceSample)
                .where(VoiceSample.organization_id == current.organization_id)
                .order_by(VoiceSample.created_at.desc())
                .limit(50)
            )
        ).all()
    )
    if len(samples) < 3:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Нужно как минимум 3 поста для анализа голоса.",
        )

    user_msg = "\n\n---\n\n".join(s.text for s in samples)
    parsed = await ai_client.chat_json(
        system=_EXTRACT_SYSTEM,
        user=user_msg[:30000],
        temperature=0.3,
        max_tokens=2000,
    )

    traits = [str(t).strip() for t in (parsed.get("voice_traits") or []) if str(t).strip()]
    avoid = [str(t).strip() for t in (parsed.get("voice_avoid") or []) if str(t).strip()]
    phrases = [str(t).strip() for t in (parsed.get("recurring_phrases") or []) if str(t).strip()]
    tone = str(parsed.get("tone_calibration", "")).strip()

    bc = await db.scalar(
        select(BrandContext).where(BrandContext.organization_id == current.organization_id)
    )
    if bc is None:
        bc = BrandContext(organization_id=current.organization_id, data={}, version=1)
        db.add(bc)

    data = dict(bc.data or {})
    data["voice_traits"] = traits
    data["voice_avoid"] = avoid
    data["recurring_phrases"] = phrases
    data["tone_calibration"] = tone
    bc.data = data
    bc.version = (bc.version or 1) + 1
    await db.flush()

    return VoiceTraitsExtracted(
        voice_traits=traits,
        voice_avoid=avoid,
        recurring_phrases=phrases,
        tone_calibration=tone,
        samples_analyzed=len(samples),
    )
