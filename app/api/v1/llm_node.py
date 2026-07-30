"""LLM node — a canvas chat node backed by Opus 4.8.

The user wires any nodes (source / extract / format) INTO an llm node; those
become the conversation's context. Then they chat with the model about that
material right on the canvas. History lives in `node.data.messages`.

Synchronous by design: a chat turn is interactive and short enough that a
request/response round-trip beats the polling ceremony of a background job.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.auth import Organization
from app.models.canvas import Canvas, Edge, Node
from app.services import ai_client
from app.services import trial as trial_svc
from app.services.brand_context import build_skill_context

router = APIRouter(tags=["llm-node"])

# Keep the assembled upstream context from blowing the model's window. Opus
# handles large inputs, but there's no point pushing a 500k-char transcript —
# clip each block and the total.
_MAX_BLOCK_CHARS = 12_000
_MAX_TOTAL_CONTEXT_CHARS = 48_000
# Trim history sent to the model (full history still persisted). Keeps latency
# and cost bounded on long chats; the last N turns carry the live thread.
_MAX_HISTORY_MESSAGES = 30


class LlmChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class LlmConfigRequest(BaseModel):
    """Update the node's own system instruction (role/task). Server-side
    merge into node.data preserves the chat history — this must NOT go
    through the generic node PATCH, which replaces the whole data blob and
    would clobber messages that live only in the client's chat state until
    the next refetch."""

    system_prompt: str = Field(default="", max_length=6000)


class LlmChatMessage(BaseModel):
    role: str
    content: str
    ts: str | None = None


class LlmChatResponse(BaseModel):
    reply: str
    messages: list[LlmChatMessage]
    context_node_count: int


def _node_to_context_block(node: Node) -> str | None:
    """Render one upstream node into a labeled text block for the LLM."""
    data = node.data or {}
    if node.type == "source":
        title = (
            data.get("url_title")
            or data.get("youtube_title")
            or data.get("file_name")
            or "Источник"
        )
        body = (data.get("content") or "").strip()
        if not body:
            return None
        return f"[ИСТОЧНИК: {title}]\n{body[:_MAX_BLOCK_CHARS]}"

    if node.type == "extract":
        mode = data.get("extract_mode") or "talking_points"
        if mode == "summary":
            summary = (data.get("summary") or "").strip()
            return f"[САММАРИ]\n{summary[:_MAX_BLOCK_CHARS]}" if summary else None
        if mode == "story_arc":
            scenes = data.get("scenes") or []
            if not scenes:
                return None
            lines = [
                f"{i + 1}. [{s.get('stage', '')}] {s.get('hook', '')} — {s.get('talking_point', '')}"
                for i, s in enumerate(scenes)
                if isinstance(s, dict)
            ]
            return "[АРКА СЦЕН]\n" + "\n".join(lines)[:_MAX_BLOCK_CHARS]
        tps = data.get("talking_points") or []
        if not tps:
            return None
        lines = [
            f"{i + 1}. {tp.get('text', '')} (score {tp.get('viral_score', '?')})"
            for i, tp in enumerate(tps)
            if isinstance(tp, dict)
        ]
        return "[ТЕЗИСЫ]\n" + "\n".join(lines)[:_MAX_BLOCK_CHARS]

    if node.type == "format":
        text = (data.get("full_text") or data.get("body") or "").strip()
        if not text:
            return None
        platform = data.get("platform", "post")
        return f"[ГОТОВЫЙ ПОСТ · {platform}]\n{text[:_MAX_BLOCK_CHARS]}"

    # Don't recurse into other llm nodes' chats.
    return None


async def _gather_context(db, llm_node: Node) -> tuple[str, int]:
    """Assemble context text from every node wired INTO this llm node."""
    incoming = (
        await db.scalars(
            select(Edge).where(Edge.target_node_id == llm_node.id)
        )
    ).all()
    source_ids = [e.source_node_id for e in incoming]
    if not source_ids:
        return "", 0

    nodes = (
        await db.scalars(select(Node).where(Node.id.in_(source_ids)))
    ).all()
    blocks: list[str] = []
    used = 0
    total = 0
    for n in nodes:
        block = _node_to_context_block(n)
        if not block:
            continue
        if total + len(block) > _MAX_TOTAL_CONTEXT_CHARS:
            block = block[: max(0, _MAX_TOTAL_CONTEXT_CHARS - total)]
        if not block:
            break
        blocks.append(block)
        total += len(block)
        used += 1
        if total >= _MAX_TOTAL_CONTEXT_CHARS:
            break
    return "\n\n".join(blocks), used


@router.post("/nodes/{node_id}/llm-chat", response_model=LlmChatResponse)
async def llm_chat(
    node_id: uuid.UUID,
    payload: LlmChatRequest,
    current: CurrentUser,
    db: DbSession,
) -> LlmChatResponse:
    node = await db.scalar(
        select(Node)
        .join(Canvas)
        .where(Node.id == node_id, Canvas.organization_id == current.organization_id)
    )
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found")
    if node.type != "llm":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not an LLM node")

    # Trial/preview quota gate — same policy as skill runs. No-op for regular.
    org = await db.scalar(
        select(Organization).where(Organization.id == current.organization_id)
    )
    if org is not None:
        await trial_svc.assert_ai_quota(db, org)

    context_text, ctx_count = await _gather_context(db, node)

    # Brand/voice context so the assistant speaks in the author's frame when
    # asked to draft — but it's a general helper, not a fixed skill.
    canvas = await db.scalar(select(Canvas).where(Canvas.id == node.canvas_id))
    brand = await build_skill_context(
        db,
        organization_id=current.organization_id,
        canvas_id=node.canvas_id if canvas else None,
    )

    system_parts = [
        "Ты — AI-ассистент внутри контент-редактора на бесконечном канвасе. "
        "Пользователь подключил к тебе ноды (источники, тезисы, готовые посты) "
        "и хочет обсуждать и дорабатывать этот материал вместе с тобой. "
        "Отвечай по делу, на русском, без воды. Можешь предлагать правки, "
        "тезисы, структуру, варианты — но не выдумывай факты, которых нет в "
        "подключённом материале.",
    ]
    # The node's own instruction (role/task) — highest-priority steer. This
    # is what tells the assistant WHY the nodes are attached and what to do
    # with them, per-node (a critic node vs a hook-generator node vs a
    # fact-checker node on the same canvas).
    node_instruction = (node.data.get("system_prompt") or "").strip()
    if node_instruction:
        system_parts.append("ТВОЯ РОЛЬ И ЗАДАЧА (задана автором):\n" + node_instruction)
    if brand.strip():
        system_parts.append("КОНТЕКСТ БРЕНДА АВТОРА:\n" + brand.strip())
    if context_text.strip():
        system_parts.append("ПОДКЛЮЧЁННЫЙ МАТЕРИАЛ:\n" + context_text.strip())
    else:
        system_parts.append(
            "Пока к ноде не подключено ни одного источника с контентом — "
            "если пользователь просит поработать с материалом, подскажи ему "
            "подключить ноду-источник в эту LLM-ноду."
        )
    system = "\n\n".join(system_parts)

    history: list[dict[str, Any]] = list(node.data.get("messages") or [])
    now = datetime.now(timezone.utc).isoformat()
    history.append({"role": "user", "content": payload.message, "ts": now})

    reply = await ai_client.chat_conversation(
        system=system,
        history=history[-_MAX_HISTORY_MESSAGES:],
        max_tokens=4000,
        temperature=0.7,
    )

    history.append(
        {
            "role": "assistant",
            "content": reply,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    )
    node.data = {**(node.data or {}), "messages": history}
    await db.flush()

    if org is not None:
        await trial_svc.incr_ai_usage(db, org.id)

    return LlmChatResponse(
        reply=reply,
        messages=[LlmChatMessage(**m) for m in history],
        context_node_count=ctx_count,
    )


@router.patch("/nodes/{node_id}/llm-config", status_code=status.HTTP_204_NO_CONTENT)
async def update_llm_config(
    node_id: uuid.UUID,
    payload: LlmConfigRequest,
    current: CurrentUser,
    db: DbSession,
):
    """Set only the node's system_prompt, merging server-side so the chat
    history is never touched."""
    node = await db.scalar(
        select(Node)
        .join(Canvas)
        .where(Node.id == node_id, Canvas.organization_id == current.organization_id)
    )
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Node not found")
    if node.type != "llm":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not an LLM node")
    prompt = payload.system_prompt.strip()
    node.data = {**(node.data or {}), "system_prompt": prompt}
    await db.flush()
