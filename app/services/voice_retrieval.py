"""Voice retrieval helpers — k-NN over voice_samples for few-shot injection."""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import ai_client

logger = logging.getLogger(__name__)


async def find_similar(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    query_text: str,
    k: int = 3,
) -> list[dict]:
    """Top-k cosine similarity matches from this org's voice_samples.

    Returns plain dicts (id, text, platform, similarity) — fully detached
    from the SQLAlchemy session to avoid binding embeddings to the prompt
    builder. Returns [] if there are no samples or embedding fails.
    """
    if not query_text.strip():
        return []
    try:
        embedding = await ai_client.embed(query_text[:4000])
    except Exception:
        logger.exception("voice retrieval: embedding failed; skipping few-shot")
        return []

    vector_literal = "[" + ",".join(str(x) for x in embedding) + "]"
    sql = text(
        """
        SELECT id, text, platform, 1 - (embedding <=> CAST(:emb AS vector)) AS similarity
        FROM voice_samples
        WHERE organization_id = :org AND embedding IS NOT NULL
        ORDER BY embedding <=> CAST(:emb AS vector)
        LIMIT :k
        """
    )
    rows = (
        await db.execute(sql, {"emb": vector_literal, "org": organization_id, "k": k})
    ).mappings().all()
    return [
        {
            "id": str(r["id"]),
            "text": r["text"],
            "platform": r["platform"],
            "similarity": float(r["similarity"]) if r["similarity"] is not None else None,
        }
        for r in rows
    ]


def format_few_shot(samples: list[dict]) -> str:
    if not samples:
        return ""
    lines = ["ПРИМЕРЫ ТВОИХ ЛУЧШИХ ПОСТОВ НА ПОХОЖИЕ ТЕМЫ (используй как стилевой ориентир, не копируй):"]
    for i, s in enumerate(samples, 1):
        head = f"--- Пример {i}"
        if s.get("platform"):
            head += f" [{s['platform']}]"
        head += " ---"
        lines.append(head)
        lines.append(s["text"])
    return "\n".join(lines)
