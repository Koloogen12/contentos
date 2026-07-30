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
    if traits := data.get("voice_traits"):
        if isinstance(traits, list):
            traits = "\n".join(f"- {t}" for t in traits)
        parts.append(f"\nХАРАКТЕРНЫЕ ЧЕРТЫ ГОЛОСА:\n{traits}")
    if avoid := data.get("voice_avoid"):
        if isinstance(avoid, list):
            avoid = "\n".join(f"- {t}" for t in avoid)
        parts.append(f"\nЧЕГО АВТОР ИЗБЕГАЕТ:\n{avoid}")
    if phrases := data.get("recurring_phrases"):
        if isinstance(phrases, list):
            phrases = ", ".join(phrases)
        parts.append(f"\nХАРАКТЕРНЫЕ СЛОВА И ФРАЗЫ: {phrases}")
    if tone := data.get("tone_calibration"):
        parts.append(f"\nТОН: {tone}")
    if taboo := data.get("taboo_list"):
        parts.append(f"\nТАБУ (никогда не писать):\n{taboo}")
    if manifesto := data.get("manifesto"):
        parts.append(f"\nУБЕЖДЕНИЯ:\n{manifesto}")
    if cta := data.get("cta_keywords"):
        if isinstance(cta, list):
            cta = ", ".join(cta)
        parts.append(f"\nCTA-слова: {cta}")
    # Content pillars (R1/R2/R3/R4 — or whatever the user named them).
    # Critical so that viral_talking_points doesn't make up pillars from
    # its own training data — see prompt comment in that skill.
    if pillars := data.get("content_pillars"):
        if isinstance(pillars, dict) and pillars:
            lines = [f"- {k}: {v}" for k, v in pillars.items()]
            parts.append("\nСТОЛБЫ КОНТЕНТА АВТОРА:\n" + "\n".join(lines))
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
    voice_query: str | None = None,
) -> str:
    """Assembles the system prompt for a skill run.

    If ``voice_query`` is provided, top-K similar voice_samples are
    appended as few-shot examples (used by format skills).
    """
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

    if voice_query:
        from app.services import voice_retrieval

        samples = await voice_retrieval.find_similar(
            db, organization_id=organization_id, query_text=voice_query, k=3
        )
        if formatted := voice_retrieval.format_few_shot(samples):
            layers.append(formatted)

    return "\n\n".join(layers).strip()


def _llm_last_reply(data: dict[str, Any]) -> str:
    """The output of an llm node = its most recent assistant reply. Moving
    target by design (each chat turn changes it); the node's port is labeled
    to say so."""
    msgs = data.get("messages") or []
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "assistant":
            return (m.get("content") or "").strip()
    return ""


async def collect_input_for_skill(db: AsyncSession, node: Node) -> dict[str, Any]:
    """Walks the incoming edge and pulls the input data for a skill run.

    - extract: takes upstream source.content
    - format:  takes upstream extract.talking_points[tezis_index].text
               (where tezis_index comes from the edge.data when the user
                spawned the format node from a specific tezis card; otherwise
                falls back to the parent extract node's selected_index).
               Or upstream source.content if connected directly to a source.
    """
    from app.models.canvas import Edge  # local to avoid a circular import at module load

    incoming = await db.scalar(
        select(Edge).where(Edge.target_node_id == node.id)
    )

    # Story-arc extract is the one node-type that may legitimately run
    # without an upstream — the arc config + big_topic live on the node
    # itself. All other node types still require an incoming edge.
    node_mode = (node.data or {}).get("extract_mode")
    if incoming is None:
        if node.type == "extract" and node_mode == "story_arc":
            return {"source_content": "", "parent_node_id": None}
        return {"error": "Нет входящей связи"}

    parent = await db.scalar(select(Node).where(Node.id == incoming.source_node_id))
    if parent is None:
        return {"error": "Источник не найден"}

    parent_data = parent.data or {}
    edge_data = incoming.data or {}

    if node.type == "extract":
        # story_arc mode can run without upstream content (the big_topic
        # + config on the node itself is enough). Other modes still need
        # source text.
        mode = (node.data or {}).get("extract_mode", "talking_points")
        # Upstream can be a source (content) OR an llm node (last reply).
        if parent.type == "llm":
            content = _llm_last_reply(parent_data)
        else:
            content = parent_data.get("content")
        if mode == "story_arc":
            return {
                "source_content": content or "",
                "parent_node_id": str(parent.id),
            }
        if not content:
            return {"error": "У источника пустой content"}
        return {"source_content": content, "parent_node_id": str(parent.id)}

    if node.type == "format":
        if parent.type == "extract":
            parent_mode = parent_data.get("extract_mode") or "talking_points"

            # Summary-mode extract: the whole summary IS the talking point.
            # No tezis index needed; per-edge override is ignored.
            if parent_mode == "summary":
                summary = (parent_data.get("summary") or "").strip()
                if not summary:
                    return {"error": "У источника пустое саммари"}
                return {
                    "talking_point": summary[:4000],
                    "platform": (node.data or {}).get("platform", "telegram"),
                    "parent_node_id": str(parent.id),
                    "from_summary": True,
                }

            # Story-arc mode: pick the scene the user spawned this format
            # node from. Edge.data.tezis_index doubles as "scene index"
            # for fanout. Scene.talking_point is the body input; scene.hook
            # gets passed as preset_hook for format-skills that want to
            # reuse it (most just overwrite, which is fine).
            if parent_mode == "story_arc":
                scenes = parent_data.get("scenes") or []
                idx = edge_data.get("tezis_index")
                if not isinstance(idx, int):
                    idx = parent_data.get("selected_index")
                if idx is None or idx < 0 or idx >= len(scenes):
                    return {"error": "Не выбрана сцена арки"}
                scene = scenes[idx] or {}
                return {
                    "talking_point": str(scene.get("talking_point") or "").strip(),
                    "platform": (node.data or {}).get(
                        "platform", scene.get("platform") or "telegram"
                    ),
                    "preset_hook": str(scene.get("hook") or "").strip() or None,
                    "scene_stage": scene.get("stage"),
                    "scene_order": scene.get("order"),
                    "parent_node_id": str(parent.id),
                    "tezis_index": idx,
                }
            # Рецензия работает по всему материалу, а не по одному тезису:
            # отдаём все извлечённые тезисы плюс исходный текст, до которого
            # поднимаемся через родителя extract-ноды. Единственный формат,
            # которому нужен весь вход целиком, — поэтому ветка здесь, а не
            # в общем сборе.
            if (node.data or {}).get("platform") == "review":
                source_content = ""
                grandparent_edge = await db.scalar(
                    select(Edge).where(Edge.target_node_id == parent.id)
                )
                if grandparent_edge is not None:
                    grandparent = await db.scalar(
                        select(Node).where(Node.id == grandparent_edge.source_node_id)
                    )
                    if grandparent is not None:
                        gp_data = grandparent.data or {}
                        source_content = str(
                            gp_data.get("content")
                            or _llm_last_reply(gp_data)
                            or ""
                        )
                return {
                    "talking_points": parent_data.get("talking_points") or [],
                    "source_content": source_content,
                    "platform": "review",
                    "parent_node_id": str(parent.id),
                }

            tps = parent_data.get("talking_points") or []
            # Prefer per-edge override (set when format node was spawned from
            # a specific tezis card); fall back to the parent's selection.
            idx = edge_data.get("tezis_index")
            if not isinstance(idx, int):
                idx = parent_data.get("selected_index")
            if idx is None or idx < 0 or idx >= len(tps):
                return {"error": "Не выбран тезис в источнике"}
            return {
                "talking_point": tps[idx].get("text", ""),
                "platform": (node.data or {}).get("platform", "telegram"),
                "parent_node_id": str(parent.id),
                "tezis_index": idx,
            }
        if parent.type == "source":
            content = parent_data.get("content")
            if not content:
                return {"error": "У источника пустой content"}
            # Рецензия читает материал целиком, поэтому не режем до 2000
            # символов и кладём в source_content, а не в talking_point.
            # Тезисов на этом пути нет — разбор пойдёт по тексту.
            if (node.data or {}).get("platform") == "review":
                return {
                    "source_content": content,
                    "talking_points": [],
                    "platform": "review",
                    "parent_node_id": str(parent.id),
                }
            return {
                "talking_point": content[:2000],
                "platform": (node.data or {}).get("platform", "telegram"),
                "parent_node_id": str(parent.id),
            }
        if parent.type == "llm":
            reply = _llm_last_reply(parent_data)
            if not reply:
                return {"error": "У LLM-ноды пока нет ответа для передачи"}
            # Третий вход в рецензию: разбор ответа ассистента целиком.
            if (node.data or {}).get("platform") == "review":
                return {
                    "source_content": reply,
                    "talking_points": [],
                    "platform": "review",
                    "parent_node_id": str(parent.id),
                }
            return {
                "talking_point": reply[:2000],
                "platform": (node.data or {}).get("platform", "telegram"),
                "parent_node_id": str(parent.id),
            }
        return {"error": f"Format не поддерживает вход {parent.type}"}

    return {"error": f"Неизвестный тип ноды {node.type}"}
