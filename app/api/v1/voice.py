"""Voice samples: store the user's own posts for few-shot retrieval at format time."""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.knowledge import BrandContext, VoiceSample
from app.schemas.voice import (
    VoiceSampleBulkCreate,
    VoiceSampleBulkResult,
    VoiceSampleCreate,
    VoiceSampleOut,
    VoiceTraitsExtracted,
)
from app.services import ai_client

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
async def delete_sample(sample_id: uuid.UUID, current: CurrentUser, db: DbSession) -> None:
    obj = await db.scalar(
        select(VoiceSample).where(
            VoiceSample.id == sample_id, VoiceSample.organization_id == current.organization_id
        )
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sample not found")
    await db.delete(obj)


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
