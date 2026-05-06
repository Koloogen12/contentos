"""Seed a new organization with a small set of starter templates."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Canvas, Edge, Node


async def seed_default_templates(db: AsyncSession, organization_id: uuid.UUID) -> int:
    """Create three starter templates: YouTube→Telegram, Article→LinkedIn, Idea→Carousel.

    Returns the number of templates created. Idempotent only in the sense
    that calling it twice will create duplicates — call once at signup.
    """
    templates: list[dict[str, Any]] = [
        {
            "name": "YouTube → Telegram",
            "description": "Транскрипт с YouTube → тезисы → пост в Telegram.",
            "nodes": [
                {"type": "source", "x": 80, "y": 120, "data": {"input_type": "youtube"}},
                {"type": "extract", "x": 420, "y": 120, "data": {}},
                {"type": "format", "x": 760, "y": 120, "data": {"platform": "telegram"}},
            ],
            "edges": [(0, 1), (1, 2)],
        },
        {
            "name": "Статья → LinkedIn",
            "description": "Текст или ссылка на статью → тезисы → пост в LinkedIn.",
            "nodes": [
                {"type": "source", "x": 80, "y": 120, "data": {"input_type": "text"}},
                {"type": "extract", "x": 420, "y": 120, "data": {}},
                {"type": "format", "x": 760, "y": 120, "data": {"platform": "linkedin"}},
            ],
            "edges": [(0, 1), (1, 2)],
        },
        {
            "name": "Идея → Карусель",
            "description": "Тезис вручную → карусель из 5–10 слайдов.",
            "nodes": [
                {"type": "source", "x": 80, "y": 120, "data": {"input_type": "text"}},
                {"type": "format", "x": 480, "y": 120, "data": {"platform": "carousel"}},
            ],
            "edges": [(0, 1)],
        },
        {
            "name": "Идея → Статья",
            "description": "Тезис → длинная статья для блога (1500–2500 слов).",
            "nodes": [
                {"type": "source", "x": 80, "y": 120, "data": {"input_type": "text"}},
                {"type": "extract", "x": 420, "y": 120, "data": {}},
                {"type": "format", "x": 760, "y": 120, "data": {"platform": "article"}},
            ],
            "edges": [(0, 1), (1, 2)],
        },
    ]

    created = 0
    for tpl in templates:
        canvas = Canvas(
            organization_id=organization_id,
            name=tpl["name"],
            description=tpl["description"],
            is_template=True,
        )
        db.add(canvas)
        await db.flush()

        node_objs: list[Node] = []
        for n in tpl["nodes"]:
            node = Node(
                canvas_id=canvas.id,
                type=n["type"],
                position_x=n["x"],
                position_y=n["y"],
                data=n["data"],
                status="idle",
            )
            db.add(node)
            await db.flush()
            node_objs.append(node)

        for src_idx, tgt_idx in tpl["edges"]:
            db.add(
                Edge(
                    canvas_id=canvas.id,
                    source_node_id=node_objs[src_idx].id,
                    target_node_id=node_objs[tgt_idx].id,
                )
            )
        created += 1

    await db.flush()
    return created
