"""Builds the system prompt context that gets injected into every skill run.

Order of layers (per PRD section 12.1):
    1. Global brand context (per-organization)
    2. Project context (if canvas is attached to a project)
    3. Selected knowledge items (attached to the node)
    4. Voice samples (top-K few-shot examples via pgvector retrieval) — optional
"""
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Canvas, Node
from app.models.knowledge import (
    BrandContext,
    KnowledgeItem,
    NodeKnowledge,
    Project,
)


def _format_brand(data: dict[str, Any]) -> str:
    if not data:
        return ""
    parts: list[str] = []
    if author_name := data.get("author_name"):
        line = f"Ты пишешь от лица {author_name}"
        if handle := data.get("author_handle"):
            line += f" ({handle})"
        parts.append(line + ".")
    if bio := data.get("author_bio"):
        parts.append(bio)
    if products := data.get("active_products"):
        parts.append(f"\nАКТИВНЫЕ ПРОДУКТЫ: {products}")
    if voice := data.get("voice_rules"):
        parts.append(f"\nГОЛОС И СТИЛЬ:\n{voice}")
    if taboo := data.get("taboo_list"):
        parts.append(f"\nТАБУ (никогда не писать):\n{taboo}")
    if manifesto := data.get("manifesto"):
        parts.append(f"\nУБЕЖДЕНИЯ:\n{manifesto}")
    if cta := data.get("cta_keywords"):
        if isinstance(cta, list):
            cta = ", ".join(cta)
        parts.append(f"\nCTA-слова: {cta}")
    return "\n".join(parts).strip()


def _format_project(project: Project) -> str:
    ctx = project.context or {}
    parts = [f"КОНТЕКСТ ПРОЕКТА «{project.name}»:"]
    if desc := ctx.get("product_description"):
        parts.append(f"О продукте: {desc}")
    if audience := ctx.get("target_audience"):
        parts.append(f"Аудитория: {audience}")
    if themes := ctx.get("key_themes"):
        if isinstance(themes, list):
            themes = "; ".join(themes)
        parts.append(f"Ключевые темы: {themes}")
    if tone := ctx.get("tone_notes"):
        parts.append(f"Тон проекта: {tone}")
    return "\n".join(parts) if len(parts) > 1 else ""


def _format_knowledge(items: list[KnowledgeItem]) -> str:
    if not items:
        return ""
    parts = ["РЕЛЕВАНТНЫЕ ЗНАНИЯ И РЕФЕРЕНСЫ:"]
    for it in items:
        score = f" [score {it.viral_score}]" if it.viral_score else ""
        parts.append(f"- ({it.type}){score} {it.title}\n  {it.body}")
    return "\n".join(parts)


async def build_skill_context(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    canvas_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
) -> str:
    """Assembles the system prompt for a skill run."""
    layers: list[str] = []

    brand = await db.scalar(
        select(BrandContext).where(BrandContext.organization_id == organization_id)
    )
    if brand and brand.data:
        if formatted := _format_brand(brand.data):
            layers.append(formatted)

    if canvas_id:
        canvas = await db.scalar(select(Canvas).where(Canvas.id == canvas_id))
        if canvas and canvas.project_id:
            project = await db.scalar(select(Project).where(Project.id == canvas.project_id))
            if project:
                if formatted := _format_project(project):
                    layers.append(formatted)

    if node_id:
        stmt = (
            select(KnowledgeItem)
            .join(NodeKnowledge, NodeKnowledge.knowledge_item_id == KnowledgeItem.id)
            .where(NodeKnowledge.node_id == node_id)
        )
        items = list((await db.scalars(stmt)).all())
        if formatted := _format_knowledge(items):
            layers.append(formatted)

    return "\n\n".join(layers).strip()


async def collect_input_for_skill(db: AsyncSession, node: Node) -> dict[str, Any]:
    """Walks the incoming edge and pulls the input data for a skill run.

    - extract: takes upstream source.content
    - format:  takes upstream extract.talking_points[selected_index].text
               (or upstream source.content if connected directly to a source)
    """
    from app.models.canvas import Edge  # local to avoid a circular import at module load

    incoming = await db.scalar(
        select(Edge).where(Edge.target_node_id == node.id)
    )
    if incoming is None:
        return {"error": "Нет входящей связи"}

    parent = await db.scalar(select(Node).where(Node.id == incoming.source_node_id))
    if parent is None:
        return {"error": "Источник не найден"}

    parent_data = parent.data or {}

    if node.type == "extract":
        content = parent_data.get("content")
        if not content:
            return {"error": "У источника пустой content"}
        return {"source_content": content, "parent_node_id": str(parent.id)}

    if node.type == "format":
        if parent.type == "extract":
            tps = parent_data.get("talking_points") or []
            idx = parent_data.get("selected_index")
            if idx is None or idx < 0 or idx >= len(tps):
                return {"error": "Не выбран тезис в источнике"}
            return {
                "talking_point": tps[idx].get("text", ""),
                "platform": (node.data or {}).get("platform", "telegram"),
                "parent_node_id": str(parent.id),
            }
        if parent.type == "source":
            content = parent_data.get("content")
            if not content:
                return {"error": "У источника пустой content"}
            return {
                "talking_point": content[:2000],
                "platform": (node.data or {}).get("platform", "telegram"),
                "parent_node_id": str(parent.id),
            }
        return {"error": f"Format не поддерживает вход {parent.type}"}

    return {"error": f"Неизвестный тип ноды {node.type}"}
