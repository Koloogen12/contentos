"""Format skill: talking_point → bank of 5–10 hooks (no body, no CTA)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.services import ai_client
from app.services.skills.base import register

SYSTEM_TEMPLATE = """\
{brand_context}

Ты создаёшь банк из 5–10 разных хуков (первых строк) на основе одного тезиса. Каждый хук:
- Самодостаточный — работает БЕЗ контекста, без «прочитайте дальше».
- Короткий (до 110 символов).
- Использует разные психологические триггеры: парадокс, конкретное число, контраст «было/стало», провокация, личная история, диссонанс, вопрос.

Тегируй каждый хук типом триггера.

ОТВЕТ СТРОГО как JSON:
{{
  "hooks": [
    {{"text": "...", "trigger": "paradox | number | contrast | provocation | story | dissonance | question"}},
    ...
  ]
}}"""

USER_TEMPLATE = """\
ТЕЗИС:
{talking_point}

Сделай банк хуков."""


@register("hooks_creator")
async def run(
    db: AsyncSession,
    node: Node,
    system_context: str,
    skill_input: dict[str, Any],
) -> dict[str, Any]:
    tp = (skill_input.get("talking_point") or "").strip()
    if not tp:
        raise ValueError("Нет входного тезиса")

    system = SYSTEM_TEMPLATE.format(brand_context=system_context or "Нет brand context.")
    user = USER_TEMPLATE.format(talking_point=tp)

    parsed = await ai_client.chat_json(system=system, user=user, temperature=0.9, max_tokens=1500)

    hooks_raw = parsed.get("hooks") or []
    hooks: list[dict[str, Any]] = []
    for h in hooks_raw:
        if not isinstance(h, dict):
            continue
        text = str(h.get("text", "")).strip()
        if not text:
            continue
        hooks.append(
            {
                "text": text,
                "trigger": str(h.get("trigger", "")).strip() or "other",
            }
        )
    if len(hooks) < 3:
        raise RuntimeError("AI вернул слишком мало хуков")

    new_data = dict(node.data or {})
    new_data.update(
        {
            "platform": "hooks",
            "talking_point_text": tp,
            "hooks_bank": hooks,
            "full_text": "\n\n".join(f"{i+1}. {h['text']} ({h['trigger']})" for i, h in enumerate(hooks)),
        }
    )
    return {"node_data": new_data, "meta": {"hooks_count": len(hooks)}}
