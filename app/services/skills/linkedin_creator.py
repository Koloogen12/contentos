"""Format skill: talking_point → publishable LinkedIn post."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.services import ai_client
from app.services.skills.base import register

SYSTEM_TEMPLATE = """\
{brand_context}

Ты пишешь пост для LinkedIn на основе одного тезиса. Формат:
1. Хук-первая строка (до 100 символов, до «...подробнее»). Сделай 3 варианта.
2. Тело: 100–300 слов, разбей на короткие абзацы (1–2 строки), используй переводы строк \
   щедро — на LinkedIn это критично для читаемости. Никаких эмодзи. Никаких хэштегов в теле.
3. CTA — вопрос аудитории или конкретный action. Не «follow me».

Стиль LinkedIn: чуть формальнее Telegram, но без корпоратива. Конкретика, числа, личный опыт. \
Не пиши «I want to share» / «In this post». Сразу к делу.

ОТВЕТ СТРОГО как JSON:
{{
  "hooks": ["вариант 1", "вариант 2", "вариант 3"],
  "selected_hook_index": 0,
  "body": "основной текст без хука и без CTA",
  "cta": "одно предложение, обычно вопрос"
}}"""

USER_TEMPLATE = """\
ТЕЗИС:
{talking_point}

Напиши пост."""


def _assemble_full_text(hook: str, body: str, cta: str) -> str:
    parts = [p.strip() for p in (hook, body, cta) if p and p.strip()]
    return "\n\n".join(parts)


@register("linkedin_creator")
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

    parsed = await ai_client.chat_json(system=system, user=user, temperature=0.8, max_tokens=2500)

    hooks_raw = parsed.get("hooks") or []
    hooks = [str(h).strip() for h in hooks_raw if str(h).strip()]
    if not hooks:
        raise RuntimeError("AI не вернул hooks")

    selected = parsed.get("selected_hook_index", 0)
    if not isinstance(selected, int) or selected < 0 or selected >= len(hooks):
        selected = 0

    body = str(parsed.get("body", "")).strip()
    cta = str(parsed.get("cta", "")).strip()
    if not body:
        raise RuntimeError("AI не вернул body")

    full_text = _assemble_full_text(hooks[selected], body, cta)

    new_data = dict(node.data or {})
    new_data.update(
        {
            "platform": "linkedin",
            "talking_point_text": tp,
            "hooks": hooks,
            "selected_hook_index": selected,
            "body": body,
            "cta": cta,
            "full_text": full_text,
        }
    )
    return {"node_data": new_data, "meta": {"hooks_count": len(hooks)}}
