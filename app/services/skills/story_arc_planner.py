"""Extract skill (mode=story_arc): big topic → multi-post warming arc.

Third extract mode (alongside `viral_talking_points` and
`content_summarizer`). Used when the founder wants a *campaign* — a
sequence of 7–15 posts that walks the audience from "this topic is
interesting" (TOFU) through "here are the specific pitfalls" (MOFU) to
"buy / subscribe / opt-in" (BOFU), ending at a lead magnet.

The skill produces a structured arc: each scene has a stage tag, a
target platform, a hook, a talking-point, an `order`, and `depends_on`
so the user can later spawn format nodes in sequence.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node
from app.services import ai_client
from app.services.skills.base import OUTPUT_LANGUAGE_DIRECTIVE, register

logger = logging.getLogger(__name__)

ALLOWED_PLATFORMS = {
    "telegram",
    "linkedin",
    "twitter",
    "instagram",
    "carousel",
    "reels",
    "hooks",
    "article",
}

ALLOWED_STAGES = {"TOFU", "MOFU", "BOFU"}

# Platform canonicalization — Claude through CometAPI often emits display
# names ("Telegram", "X") or Russian aliases ("телеграм", "рилз") instead
# of the exact enum literals. Normalize everything here.
_PLATFORM_ALIAS = {
    "tg": "telegram",
    "telegram": "telegram",
    "телеграм": "telegram",
    "linkedin": "linkedin",
    "ли": "linkedin",
    "линкедин": "linkedin",
    "x": "twitter",
    "twitter": "twitter",
    "твиттер": "twitter",
    "ig": "instagram",
    "instagram": "instagram",
    "инстаграм": "instagram",
    "carousel": "carousel",
    "карусель": "carousel",
    "ig_carousel": "carousel",
    "reels": "reels",
    "рилс": "reels",
    "рилз": "reels",
    "shorts": "reels",
    "tiktok": "reels",
    "youtube_short": "reels",
    "hooks": "hooks",
    "хуки": "hooks",
    "article": "article",
    "статья": "article",
    "blog": "article",
    "blog_post": "article",
}


SYSTEM_TEMPLATE = """\
Respond with raw JSON ARRAY ONLY. No preamble, no markdown fences, no \
<tool_calls>, no top-level object — your entire response must start with \
`[` and end with `]`. Each array element is one scene.

You are a content-campaign producer. Given a big topic, design 7-12 \
scenes that warm a reader from broad interest to clicking a lead magnet. \
Output language: Russian (for hook and talking_point string values).

{brand_context}

{language_directive}

FUNNEL STAGES (literals): TOFU (40-50% of scenes, awareness), MOFU \
(30-40%, specific mistakes/frameworks), BOFU (10-20%, lead-magnet CTA).

ALLOWED PLATFORMS (literals): telegram, linkedin, twitter, instagram, \
carousel, reels, hooks, article. Vary them; no two same in a row.

EACH SCENE has EXACTLY these six keys and nothing else:
- order: int (1, 2, 3, …)
- stage: "TOFU" | "MOFU" | "BOFU"
- platform: one allowed literal
- hook: ≤ 100 chars. The literal first-line preview. NOT a description, \
  NOT a question of where to find it — write the actual opener.
- talking_point: ≤ 150 chars. ONE short sentence describing the angle. \
  NOT the post body — the AI will expand it later when the user spawns \
  a format node. Be terse: "Где искать B2B-респондентов через ваших \
  клиентов" (good) vs full paragraph (bad).
- depends_on: int | null

DO NOT include: body, cta, topic, title, summary, tags, post_number, \
lead_magnet_tie_in, or any other key.

Last scene must be BOFU with a direct CTA to the lead magnet in its hook \
or talking_point.

EXAMPLE (shape only, your content will differ):
[
  {{"order": 1, "stage": "TOFU", "platform": "telegram", "hook": "...", "talking_point": "...", "depends_on": null}},
  {{"order": 2, "stage": "TOFU", "platform": "twitter", "hook": "...", "talking_point": "...", "depends_on": 1}},
  {{"order": 8, "stage": "BOFU", "platform": "article", "hook": "...", "talking_point": "...", "depends_on": 7}}
]

If input is sparse, fill reasonable defaults — DO NOT ask the user for \
clarification. Output ONLY the JSON array."""

USER_TEMPLATE = """\
big_topic: {big_topic}
goal: {goal}
lead_magnet ({lead_magnet_kind}): {lead_magnet}
platforms_mode: {platforms_mode}
{platforms_fixed_block}target_length: {total_posts_target}

Optional source material (use as grounding if non-empty):
{content}

Output the JSON array now."""


def _coerce_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _extract_scene_objects(raw: str) -> list[dict]:
    """Pull as many complete `{...}` scene objects out of `raw` as we can.

    CometAPI-proxied Claude routinely truncates long structured output
    mid-string when `max_tokens` is reached, and ignores schema details
    by wrapping the array in `{"posts": [...]}` etc. Instead of failing
    the whole run, walk the string and scan every brace-balanced
    `{...}` substring at depth 1 inside an array, parse each one
    independently, and drop the half-finished trailing one. Works for
    raw arrays AND for arrays nested inside a top-level object.
    """
    s = raw.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1 :]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()

    out: list[dict] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        start = i
        j = i
        complete = False
        while j < n:
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = s[start : j + 1]
                        try:
                            obj = json.loads(chunk)
                            if isinstance(obj, dict):
                                out.append(obj)
                        except json.JSONDecodeError:
                            pass
                        complete = True
                        j += 1
                        break
            j += 1
        if not complete:
            # Trailing partial object — we already captured everything
            # complete before it; bail.
            break
        i = j
    return out


@register("story_arc_planner")
async def run(
    db: AsyncSession,
    node: Node,
    system_context: str,
    skill_input: dict[str, Any],
) -> dict[str, Any]:
    data = node.data or {}
    config = data.get("arc_config") or {}
    big_topic = (config.get("big_topic") or "").strip()
    if not big_topic:
        raise ValueError("Не указана большая тема арки")

    goal = (config.get("goal") or "").strip() or "не указано"
    lead_magnet = (config.get("lead_magnet") or "").strip() or "не указан"
    lead_magnet_kind = config.get("lead_magnet_kind") or "external_url"
    if lead_magnet_kind not in ("external_url", "internal_article"):
        lead_magnet_kind = "external_url"
    platforms_mode = config.get("platforms_mode") or "ai"
    platforms_fixed = config.get("platforms_fixed") or []
    total_posts_target = config.get("total_posts_target")

    platforms_fixed_block = ""
    if platforms_mode == "fixed" and platforms_fixed:
        platforms_fixed_block = (
            f"fixed_platforms (use ONLY these, any order): {', '.join(platforms_fixed)}\n"
        )

    content = (skill_input.get("source_content") or "").strip()
    if not content:
        content = "(none)"

    system = SYSTEM_TEMPLATE.format(
        brand_context=system_context or "(no brand context)",
        language_directive=OUTPUT_LANGUAGE_DIRECTIVE,
    )
    user = USER_TEMPLATE.format(
        big_topic=big_topic,
        goal=goal,
        lead_magnet=lead_magnet,
        lead_magnet_kind=lead_magnet_kind,
        platforms_mode=platforms_mode,
        platforms_fixed_block=platforms_fixed_block,
        total_posts_target=total_posts_target or "AI chooses (7-12)",
        content=content[:6000],
    )

    # Long structured plan (7–12 scenes, strict schema) — route to the
    # structured model. Grok-4.3 here tends to fan into prose; the
    # default `grok-4.3` would also routinely truncate the array at
    # max_tokens. Per-skill override.
    from app.config import settings as _settings
    raw = await ai_client.chat_completion(
        system=system,
        user=user,
        temperature=0.3,
        max_tokens=12000,
        json_mode=False,
        model=_settings.COMETAPI_MODEL_STRUCTURED,
    )

    scene_objects = _extract_scene_objects(raw)
    if not scene_objects:
        logger.error("story_arc: no scenes parsed. Raw head: %s", raw[:600])
        raise RuntimeError(
            "AI не вернул валидный массив сцен — попробуй переформулировать "
            "тему или нажми «Собрать арку» ещё раз."
        )

    # Sort by AI-given order if present, else by encounter order. Then
    # renumber 1..N so the UI doesn't have gaps.
    def _key(d: dict) -> int:
        v = d.get("order") or d.get("index") or d.get("number")
        if isinstance(v, int):
            return v
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    scene_objects.sort(key=_key)

    cleaned_scenes: list[dict[str, Any]] = []
    total_pre = len(scene_objects)
    for i, s in enumerate(scene_objects):
        # Pull hook/talking_point under aliases the AI sometimes uses.
        hook = str(
            s.get("hook")
            or s.get("title")
            or s.get("headline")
            or ""
        ).strip()
        tp = str(
            s.get("talking_point")
            or s.get("body")
            or s.get("core_idea")
            or s.get("content")
            or s.get("description")
            or ""
        ).strip()
        if not hook or not tp:
            continue

        stage_raw = str(s.get("stage") or s.get("funnel_stage") or s.get("phase") or "").strip().upper()
        if stage_raw in ALLOWED_STAGES:
            stage = stage_raw
        else:
            # Position-based fallback: 40% TOFU, 40% MOFU, 20% BOFU.
            pos = i / max(total_pre, 1)
            if pos < 0.4:
                stage = "TOFU"
            elif pos < 0.8:
                stage = "MOFU"
            else:
                stage = "BOFU"

        platform_raw = str(
            s.get("platform")
            or s.get("channel")
            or s.get("format")
            or ""
        ).strip().lower()
        platform = _PLATFORM_ALIAS.get(platform_raw, platform_raw)
        if platform not in ALLOWED_PLATFORMS:
            # Sensible default depends on stage.
            platform = "article" if stage == "BOFU" else "telegram"

        depends_on = s.get("depends_on") or s.get("references") or s.get("after")
        if depends_on is not None:
            depends_on = _coerce_int(depends_on, 0)
            if depends_on <= 0:
                depends_on = None

        cleaned_scenes.append(
            {
                "order": i + 1,
                "stage": stage,
                "platform": platform,
                "hook": hook[:240],
                "talking_point": tp[:400],
                "depends_on": depends_on,
                "is_final": False,
                "spawned_node_id": None,
            }
        )

    if not cleaned_scenes:
        raise RuntimeError("Все сцены отброшены при нормализации — попробуй ещё раз.")

    # Last scene is final (CTA to lead magnet).
    cleaned_scenes[-1]["is_final"] = True

    # Infer arc summary from config + scene tallies (the AI tends to fight
    # us if we ask it to compose this; doing it deterministically is more
    # reliable and saves tokens).
    stages_breakdown: dict[str, int] = {"TOFU": 0, "MOFU": 0, "BOFU": 0}
    for s in cleaned_scenes:
        stages_breakdown[s["stage"]] += 1

    cleaned_arc = {
        "title": f"Контент-арка: {big_topic[:60]}",
        "narrative_summary": (
            f"{stages_breakdown['TOFU']} постов на прогрев темы, "
            f"{stages_breakdown['MOFU']} на углубление, "
            f"{stages_breakdown['BOFU']} с CTA на «{lead_magnet[:40]}»."
        ),
        "total_posts": len(cleaned_scenes),
        "stages_breakdown": stages_breakdown,
    }

    new_data = dict(data)
    new_data.update(
        {
            "extract_mode": "story_arc",
            "arc": cleaned_arc,
            "scenes": cleaned_scenes,
            "talking_points": [],
            "selected_index": None,
            "summary": "",
            "key_points": [],
            "actionable_takeaways": [],
        }
    )

    return {
        "node_data": new_data,
        "meta": {
            "scenes_count": len(cleaned_scenes),
            **{f"stage_{k.lower()}": v for k, v in stages_breakdown.items()},
        },
    }
