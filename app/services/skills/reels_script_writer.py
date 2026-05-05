"""Format skill: talking_point → Reels/Shorts script (15–60s) with hook + scenes + CTA."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.services import ai_client
from app.services.skills.base import register

SYSTEM_TEMPLATE = """\
{brand_context}

Ты пишешь сценарий короткого видео (Reels / TikTok / Shorts) на основе тезиса. Целевая длина 30–45 секунд.

Структура:
- HOOK (0–3 сек, до 10 слов) — одна строка которая удержит зрителя на 3 сек. Сделай 3 варианта.
- BEATS — 4–6 сцен по 3–8 секунд каждая. У каждой:
    - script: текст голоса/титра
    - visual: что в кадре (кратко, что снимать или какой видеоряд подобрать)
    - duration_sec: ориентир длительности
- CTA — одно предложение. Не «подписывайся».
- caption — пост-описание под видео для платформы (200–500 символов, без хэштегов).

ОТВЕТ СТРОГО как JSON:
{{
  "hooks": ["...", "...", "..."],
  "selected_hook_index": 0,
  "beats": [
    {{"script": "...", "visual": "...", "duration_sec": 5}}
  ],
  "cta": "...",
  "caption": "..."
}}"""

USER_TEMPLATE = """\
ТЕЗИС:
{talking_point}

Напиши сценарий."""


def _format_full_text(hook: str, beats: list[dict], cta: str, caption: str) -> str:
    lines: list[str] = []
    if hook:
        lines.append(f"HOOK: {hook}")
    for i, b in enumerate(beats, 1):
        line = f"[{i}] {b.get('script','')}"
        if b.get("visual"):
            line += f"\n    visual: {b['visual']}"
        if b.get("duration_sec"):
            line += f"\n    ~{b['duration_sec']}s"
        lines.append(line)
    if cta:
        lines.append(f"CTA: {cta}")
    if caption:
        lines.append(f"\n--- caption ---\n{caption}")
    return "\n\n".join(lines)


@register("reels_creator")
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

    parsed = await ai_client.chat_json(system=system, user=user, temperature=0.85, max_tokens=2500)

    hooks = [str(h).strip() for h in (parsed.get("hooks") or []) if str(h).strip()]
    if not hooks:
        raise RuntimeError("AI не вернул hooks")
    selected = parsed.get("selected_hook_index", 0)
    if not isinstance(selected, int) or selected < 0 or selected >= len(hooks):
        selected = 0

    beats_raw = parsed.get("beats") or []
    beats: list[dict[str, Any]] = []
    for b in beats_raw:
        if not isinstance(b, dict):
            continue
        beats.append(
            {
                "script": str(b.get("script", "")).strip(),
                "visual": str(b.get("visual", "")).strip(),
                "duration_sec": int(b.get("duration_sec") or 5),
            }
        )
    if len(beats) < 3:
        raise RuntimeError("AI вернул слишком мало сцен")

    cta = str(parsed.get("cta", "")).strip()
    caption = str(parsed.get("caption", "")).strip()
    total_sec = sum(b["duration_sec"] for b in beats)

    new_data = dict(node.data or {})
    new_data.update(
        {
            "platform": "reels",
            "talking_point_text": tp,
            "hooks": hooks,
            "selected_hook_index": selected,
            "beats": beats,
            "cta": cta,
            "caption": caption,
            "duration_sec": total_sec,
            "full_text": _format_full_text(hooks[selected], beats, cta, caption),
        }
    )
    return {"node_data": new_data, "meta": {"beats": len(beats), "duration_sec": total_sec}}
