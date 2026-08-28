"""Arq task entry points: skill execution + Telegram publishing + visual render + tg metrics."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models.canvas import Canvas, Node, SkillRun
from app.models.knowledge import Project
from app.models.publish import PublishLog, TelegramTarget
from app.models.social import SocialAccount
from app.services import ai_client, events, publishing, telegram_bot, telegram_metrics
from app.services.brand_context import build_skill_context, collect_input_for_skill
from app.services.render.carousel import (
    BrandVisual,
    render_carousel_for_node,
    render_single_body_slide,
)
from app.services.render.slide_rewrite import rewrite_slide
from app.services.skills import get as get_skill
from app.services.skills.base import count_machine_tells

logger = logging.getLogger(__name__)


async def _publish(skill_run_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
    try:
        await events.publish(skill_run_id, event, data)
    except Exception:
        logger.exception("publish event failed")


def _brand_visual_from_project(project: Project | None) -> BrandVisual:
    """Build a `BrandVisual` from `project.brand_visual` JSONB.

    Falls back to defaults when fields are missing / project is None — so
    a brand-new canvas with no project still produces a sane-looking
    carousel using the founder's @kochnefff fallback.
    """
    data: dict[str, Any] = (project.brand_visual if project else None) or {}
    return BrandVisual(
        username=str(data.get("username") or "").strip() or None,
        show_numbering=bool(data.get("show_numbering", True)),
        prepend_slide_number=bool(data.get("prepend_number", True)),
        eyebrow_text=str(data.get("eyebrow_text") or "ОШИБКА"),
        style=str(data.get("style") or "editorial_dark"),
    )


async def run_skill(ctx: dict, skill_run_id_str: str) -> dict[str, Any]:
    """Pull the SkillRun, dispatch to the matching skill, persist node + status."""
    skill_run_id = uuid.UUID(skill_run_id_str)

    async with SessionLocal() as db:
        skill_run = await db.scalar(select(SkillRun).where(SkillRun.id == skill_run_id))
        if skill_run is None:
            return {"ok": False, "error": "SkillRun not found"}

        node = await db.scalar(select(Node).where(Node.id == skill_run.node_id))
        if node is None:
            skill_run.status = "failed"
            skill_run.error = "Node not found"
            skill_run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            await _publish(skill_run_id, "error", {"message": "Node not found"})
            return {"ok": False, "error": "Node not found"}

        canvas_id = node.canvas_id

        canvas = await db.scalar(select(Canvas).where(Canvas.id == canvas_id))
        organization_id = canvas.organization_id if canvas else None

        skill_run.status = "running"
        node.status = "running"
        await db.commit()
        await _publish(skill_run_id, "status", {"status": "running"})

        started = time.perf_counter()
        try:
            # If the API endpoint already pre-loaded input_snapshot (e.g. for
            # transcription, where there's no upstream edge), use it as-is.
            # Otherwise walk the incoming edge to collect input from upstream.
            if skill_run.input_snapshot:
                skill_input = dict(skill_run.input_snapshot)
            else:
                skill_input = await collect_input_for_skill(db, node)
                if "error" in skill_input:
                    raise ValueError(skill_input["error"])
                skill_run.input_snapshot = skill_input

            # Образцы голоса (few-shot из voice_samples) нужны каждому
            # скиллу, который пишет связный текст от лица автора, а не
            # только телеграму с линкедином. Из-за старого списка из двух
            # имён рецензия и статья писались вообще без единого примера
            # авторской манеры — отсюда и «не от моего лица».
            #
            # Извлечения и планировщики сюда не входят намеренно: они
            # разбирают чужой материал, и подмешивать туда авторский стиль
            # значит переписывать источник его словами.
            VOICE_AWARE_SKILLS = {
                "telegram_creator",
                "linkedin_creator",
                "twitter_creator",
                "instagram_creator",
                "article_creator",
                "review_creator",
                "vc_creator",
                "carousel_creator",
                "reels_script_writer",
                "hooks_creator",
            }
            voice_query = None
            if skill_run.skill in VOICE_AWARE_SKILLS:
                # Чем ищем похожие образцы: тезис, если он есть, иначе тема
                # материала — у рецензии отдельного тезиса нет.
                voice_query = (
                    skill_input.get("talking_point")
                    or (node.data or {}).get("talking_point_text")
                    or (node.data or {}).get("title")
                    or (skill_input.get("source_content") or "")[:500]
                    or None
                )
            elif skill_run.skill == "tweak" and node.type == "format":
                voice_query = (
                    (node.data or {}).get("talking_point_text")
                    or skill_input.get("talking_point")
                    or (node.data or {}).get("title")
                )

            system_context = await build_skill_context(
                db,
                organization_id=organization_id,
                canvas_id=canvas_id,
                node_id=node.id,
                voice_query=voice_query,
            )

            skill_fn = get_skill(skill_run.skill)
            await _publish(skill_run_id, "progress", {"step": "calling-ai"})
            # Расход собираем вокруг всего вызова: скилл может сходить в
            # модель несколько раз (повтор при отказе прокси, второй проход
            # у tweak), и считать надо все обращения, а не последнее.
            with ai_client.collect_usage() as usage:
                result = await skill_fn(db, node, system_context, skill_input)

            new_data = result.get("node_data") or {}
            node.data = new_data
            node.status = "done"

            # Считаем приметы машинного текста в том, что реально увидит
            # пользователь. Единая точка для всех навыков — считать в каждом
            # по отдельности значило бы девять одинаковых правок и девять
            # мест, где про это забудут.
            tells = count_machine_tells(
                " ".join(
                    str(new_data.get(field) or "")
                    for field in ("full_text", "body", "hook", "cta", "afterword")
                )
            )
            run_meta = dict(result.get("meta") or {})
            if tells:
                run_meta["machine_tells"] = tells

            duration_ms = int((time.perf_counter() - started) * 1000)
            skill_run.status = "completed"
            skill_run.duration_ms = duration_ms
            skill_run.completed_at = datetime.now(timezone.utc)
            skill_run.output = None
            # Ноль вызовов бывает у скиллов без обращения к модели
            # (например, извлечение текста по ссылке) — им пишем NULL,
            # чтобы не путать «бесплатно» с «не считали».
            if usage["calls"]:
                skill_run.input_tokens = usage["input_tokens"]
                skill_run.output_tokens = usage["output_tokens"]
                skill_run.cached_input_tokens = usage["cached_input_tokens"]
                skill_run.model = usage["model"]
            await db.commit()

            await _publish(
                skill_run_id,
                "complete",
                {
                    "node_id": str(node.id),
                    "node_data": new_data,
                    "node_status": "done",
                    "duration_ms": duration_ms,
                    "meta": run_meta,
                },
            )
            return {"ok": True}
        except Exception as exc:
            logger.exception("skill run failed")
            duration_ms = int((time.perf_counter() - started) * 1000)
            await db.rollback()

            skill_run = await db.scalar(select(SkillRun).where(SkillRun.id == skill_run_id))
            node = await db.scalar(select(Node).where(Node.id == skill_run.node_id)) if skill_run else None
            if skill_run is not None:
                skill_run.status = "failed"
                skill_run.error = str(exc)[:2000]
                skill_run.duration_ms = duration_ms
                skill_run.completed_at = datetime.now(timezone.utc)
            if node is not None:
                node.status = "error"
            await db.commit()
            await _publish(skill_run_id, "error", {"message": str(exc)[:500]})
            return {"ok": False, "error": str(exc)}


async def render_carousel(ctx: dict, skill_run_id_str: str) -> dict[str, Any]:
    """Render visual JPEGs for a carousel format-node and persist URLs.

    Reuses the SkillRun envelope so the existing `/skill-runs/{id}/stream`
    SSE pipeline works for render progress without a new endpoint. The
    SkillRun.skill field is `"render_carousel"` (not a registered Skill
    callable — we dispatch on it directly here).

    Result is written to `node.data["rendered_slides"]`:
        {
          "render_id": "<hex>",
          "style": "editorial_dark",
          "duration_seconds": 87.4,
          "cover_prompt": "...",
          "generated_at": "2026-05-15T12:34:56Z",
          "slides": [
            {"index": 1, "is_cover": true,  "url": "https://...", "w": 1080, "h": 1350},
            {"index": 2, "is_cover": false, "url": "https://...", "w": 1080, "h": 1350},
            ...
          ]
        }
    """
    skill_run_id = uuid.UUID(skill_run_id_str)

    async with SessionLocal() as db:
        skill_run = await db.scalar(select(SkillRun).where(SkillRun.id == skill_run_id))
        if skill_run is None:
            return {"ok": False, "error": "SkillRun not found"}

        node = await db.scalar(select(Node).where(Node.id == skill_run.node_id))
        if node is None:
            skill_run.status = "failed"
            skill_run.error = "Node not found"
            skill_run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            await _publish(skill_run_id, "error", {"message": "Node not found"})
            return {"ok": False, "error": "Node not found"}

        node_data = dict(node.data or {})
        slides = node_data.get("slides") or []
        if (node_data.get("platform") or "") != "carousel":
            await _fail_render(
                db, skill_run, node, "Render доступен только для платформы 'carousel'"
            )
            await _publish(skill_run_id, "error", {"message": "Not a carousel node"})
            return {"ok": False, "error": "Not a carousel node"}
        if not slides:
            await _fail_render(
                db, skill_run, node, "Слайды отсутствуют — сначала запусти карусель"
            )
            await _publish(skill_run_id, "error", {"message": "No slides to render"})
            return {"ok": False, "error": "No slides to render"}

        canvas = await db.scalar(select(Canvas).where(Canvas.id == node.canvas_id))
        project = None
        if canvas and canvas.project_id:
            project = await db.scalar(
                select(Project).where(Project.id == canvas.project_id)
            )
        brand_visual = _brand_visual_from_project(project)

        skill_run.status = "running"
        node.status = "running"
        await db.commit()
        await _publish(skill_run_id, "status", {"status": "running"})
        await _publish(
            skill_run_id,
            "progress",
            {"step": "image-gen", "slides_count": len(slides)},
        )

        started = time.perf_counter()
        try:
            result = await render_carousel_for_node(
                node_id=str(node.id),
                slides_data=slides,
                talking_point=str(node_data.get("talking_point_text") or "").strip(),
                brand_visual=brand_visual,
            )

            rendered = {
                "render_id": result.render_id,
                "style": result.style,
                "duration_seconds": result.duration_seconds,
                "cover_prompt": result.cover_prompt,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "slides": [
                    {
                        "index": s.index,
                        "is_cover": s.is_cover,
                        "url": s.url,
                        "w": s.width,
                        "h": s.height,
                    }
                    for s in result.slides
                ],
            }
            # `cover_variants` already lives in node.data (set by tweak
            # rehook) — leave it alone so the alternative-cover chips
            # keep working even after a visual render.
            node_data["rendered_slides"] = rendered
            node.data = node_data
            node.status = "done"

            duration_ms = int((time.perf_counter() - started) * 1000)
            skill_run.status = "completed"
            skill_run.duration_ms = duration_ms
            skill_run.completed_at = datetime.now(timezone.utc)
            await db.commit()

            await _publish(
                skill_run_id,
                "complete",
                {
                    "node_id": str(node.id),
                    "node_data": node_data,
                    "node_status": "done",
                    "duration_ms": duration_ms,
                    "meta": {**(result.meta or {}), "rendered_slides": rendered},
                },
            )
            return {"ok": True, "render_id": result.render_id}
        except Exception as exc:
            logger.exception("carousel render failed")
            duration_ms = int((time.perf_counter() - started) * 1000)
            await db.rollback()
            skill_run = await db.scalar(select(SkillRun).where(SkillRun.id == skill_run_id))
            node = (
                await db.scalar(select(Node).where(Node.id == skill_run.node_id))
                if skill_run
                else None
            )
            if skill_run is not None:
                skill_run.status = "failed"
                skill_run.error = str(exc)[:2000]
                skill_run.duration_ms = duration_ms
                skill_run.completed_at = datetime.now(timezone.utc)
            if node is not None:
                node.status = "error"
            await db.commit()
            await _publish(skill_run_id, "error", {"message": str(exc)[:500]})
            return {"ok": False, "error": str(exc)}


async def slide_tweak(ctx: dict, skill_run_id_str: str) -> dict[str, Any]:
    """Rewrite one carousel slide via AI + re-render it via Playwright.

    Input snapshot (set by the API handler before enqueue):
      - slide_index: int  (1-based, matches `rendered_slides.slides[*].index`)
      - user_prompt: str  (free-form: "сократи", "сделай ярче", ...)

    Cover slides are rejected here — they need a separate flow because
    their AI-image background isn't stored as a re-composable asset.
    """
    skill_run_id = uuid.UUID(skill_run_id_str)

    async with SessionLocal() as db:
        skill_run = await db.scalar(select(SkillRun).where(SkillRun.id == skill_run_id))
        if skill_run is None:
            return {"ok": False, "error": "SkillRun not found"}
        node = await db.scalar(select(Node).where(Node.id == skill_run.node_id))
        if node is None:
            await _fail_render(db, skill_run, node, "Node not found")
            await _publish(skill_run_id, "error", {"message": "Node not found"})
            return {"ok": False, "error": "Node not found"}

        snap = skill_run.input_snapshot or {}
        slide_index = snap.get("slide_index")
        user_prompt = (snap.get("user_prompt") or "").strip()
        if not isinstance(slide_index, int) or slide_index < 1:
            await _fail_render(db, skill_run, node, "Некорректный slide_index")
            await _publish(skill_run_id, "error", {"message": "Bad slide_index"})
            return {"ok": False, "error": "Bad slide_index"}
        if not user_prompt:
            await _fail_render(db, skill_run, node, "Пустой user_prompt")
            await _publish(skill_run_id, "error", {"message": "Empty prompt"})
            return {"ok": False, "error": "Empty prompt"}

        node_data = dict(node.data or {})
        slides_list = node_data.get("slides") or []
        rendered = node_data.get("rendered_slides") or {}
        rendered_slides = rendered.get("slides") or []

        if (node_data.get("platform") or "") != "carousel":
            await _fail_render(db, skill_run, node, "Slide-tweak только для carousel")
            await _publish(skill_run_id, "error", {"message": "Not a carousel"})
            return {"ok": False, "error": "Not a carousel"}

        if slide_index > len(slides_list):
            await _fail_render(
                db, skill_run, node,
                f"slide_index={slide_index} вне диапазона (всего {len(slides_list)})",
            )
            await _publish(skill_run_id, "error", {"message": "Out of range"})
            return {"ok": False, "error": "Out of range"}

        # 1-based → 0-based for array access
        slide_data = dict(slides_list[slide_index - 1])
        if slide_data.get("is_cover"):
            await _fail_render(
                db, skill_run, node,
                "Обложку через slide-tweak редактировать нельзя — используй «Другой хук»",
            )
            await _publish(skill_run_id, "error", {"message": "Cover not editable here"})
            return {"ok": False, "error": "Cover not editable"}

        canvas = await db.scalar(select(Canvas).where(Canvas.id == node.canvas_id))
        organization_id = canvas.organization_id if canvas else None
        project = None
        if canvas and canvas.project_id:
            project = await db.scalar(
                select(Project).where(Project.id == canvas.project_id)
            )
        brand_visual = _brand_visual_from_project(project)

        # System context = same brand voice that produced the slide, so the
        # rewrite stays consistent with the deck.
        from app.services.brand_context import build_skill_context  # local to avoid heavy import on cold start
        voice_query = (
            slide_data.get("title") or slide_data.get("body") or
            node_data.get("talking_point_text") or ""
        )
        system_context = await build_skill_context(
            db,
            organization_id=organization_id,
            canvas_id=node.canvas_id,
            node_id=node.id,
            voice_query=voice_query,
        )

        skill_run.status = "running"
        node.status = "running"
        await db.commit()
        await _publish(skill_run_id, "status", {"status": "running"})
        await _publish(skill_run_id, "progress", {"step": "ai-rewrite"})

        started = time.perf_counter()
        try:
            # 1. AI rewrite — single short call (~2-5s)
            position = (
                f"слайд {slide_index} из {len(slides_list)}"
                if len(slides_list) > 1 else "одиночный слайд"
            )
            new_title, new_body = await rewrite_slide(
                system_context=system_context,
                current_title=str(slide_data.get("title") or ""),
                current_body=str(slide_data.get("body") or ""),
                user_prompt=user_prompt,
                slide_position=position,
            )
            if not new_title and not new_body:
                raise RuntimeError("AI вернул пустой слайд")

            slide_data["title"] = new_title or slide_data.get("title", "")
            slide_data["body"] = new_body or slide_data.get("body", "")
            slides_list[slide_index - 1] = slide_data

            # 2. Re-render that slide via Playwright (~3-5s)
            await _publish(skill_run_id, "progress", {"step": "re-render"})
            render_id = rendered.get("render_id") or "live"
            sr = await render_single_body_slide(
                node_id=str(node.id),
                render_id=render_id,
                slide_index_1based=slide_index,
                slide_data=slide_data,
                total_slides=len(slides_list),
                brand_visual=brand_visual,
            )

            # 3. Patch rendered_slides.slides[i].url
            updated_rendered = list(rendered_slides)
            replaced = False
            for j, rs in enumerate(updated_rendered):
                if isinstance(rs, dict) and rs.get("index") == slide_index:
                    updated_rendered[j] = {
                        **rs,
                        "url": sr.url,
                        "w": sr.width,
                        "h": sr.height,
                    }
                    replaced = True
                    break
            if not replaced:
                # First time touching this index? Append — keeps the UX
                # consistent if rendered_slides was somehow partial.
                updated_rendered.append(
                    {
                        "index": slide_index,
                        "is_cover": False,
                        "url": sr.url,
                        "w": sr.width,
                        "h": sr.height,
                    }
                )

            node_data["slides"] = slides_list
            node_data["rendered_slides"] = {
                **rendered,
                "slides": sorted(updated_rendered, key=lambda r: r.get("index", 0)),
            }
            node.data = node_data
            node.status = "done"

            duration_ms = int((time.perf_counter() - started) * 1000)
            skill_run.status = "completed"
            skill_run.duration_ms = duration_ms
            skill_run.completed_at = datetime.now(timezone.utc)
            await db.commit()

            await _publish(
                skill_run_id,
                "complete",
                {
                    "node_id": str(node.id),
                    "node_data": node_data,
                    "node_status": "done",
                    "duration_ms": duration_ms,
                    "meta": {
                        "slide_index": slide_index,
                        "new_title": new_title,
                        "new_url": sr.url,
                    },
                },
            )
            return {"ok": True, "slide_index": slide_index, "url": sr.url}
        except Exception as exc:
            logger.exception("slide_tweak failed")
            duration_ms = int((time.perf_counter() - started) * 1000)
            await db.rollback()
            skill_run = await db.scalar(
                select(SkillRun).where(SkillRun.id == skill_run_id)
            )
            node = (
                await db.scalar(select(Node).where(Node.id == skill_run.node_id))
                if skill_run else None
            )
            if skill_run is not None:
                skill_run.status = "failed"
                skill_run.error = str(exc)[:2000]
                skill_run.duration_ms = duration_ms
                skill_run.completed_at = datetime.now(timezone.utc)
            if node is not None:
                node.status = "error"
            await db.commit()
            await _publish(skill_run_id, "error", {"message": str(exc)[:500]})
            return {"ok": False, "error": str(exc)}


async def _fail_render(db, skill_run: SkillRun, node: Node, message: str) -> None:
    """Shared early-exit path when the render preconditions don't hold."""
    skill_run.status = "failed"
    skill_run.error = message
    skill_run.completed_at = datetime.now(timezone.utc)
    node.status = "error"
    await db.commit()


async def pull_telegram_metrics_one(
    ctx: dict, publish_log_id_str: str
) -> dict[str, Any]:
    """Refresh `publish_logs.metrics` for a single sent post.

    Called both from the cron sweep (`pull_telegram_metrics_all`) and from
    the manual `POST /publish-logs/{id}/refresh-metrics` endpoint. The
    distinction matters only for logging; the body is identical.

    Best-effort: returns `{"ok": True, "skipped": "..."}` instead of raising
    when the post can't be measured (private channel, deleted message,
    transient fetch failure). The cron task keeps moving — we don't want
    a single broken post to poison the whole sweep.
    """
    publish_log_id = uuid.UUID(publish_log_id_str)

    async with SessionLocal() as db:
        log = await db.scalar(
            select(PublishLog).where(PublishLog.id == publish_log_id)
        )
        if log is None:
            return {"ok": False, "error": "PublishLog not found"}
        if log.status != "sent":
            return {"ok": True, "skipped": f"status={log.status}"}

        msg_id = (log.response or {}).get("message_id")
        if not isinstance(msg_id, int):
            return {"ok": True, "skipped": "no message_id in response"}

        target = await db.scalar(
            select(TelegramTarget).where(TelegramTarget.id == log.target_id)
        )
        if target is None:
            return {"ok": True, "skipped": "target deleted"}

        handle = telegram_metrics.derive_handle(
            chat_id=target.chat_id,
            public_handle=target.public_handle,
        )
        if not handle:
            return {"ok": True, "skipped": "no public_handle"}

        metrics = await telegram_metrics.fetch_post_metrics(
            channel=handle,
            message_id=msg_id,
        )
        if metrics is None:
            # Don't wipe existing data — a transient fetch failure or a
            # deleted post shouldn't erase historical counts. The frontend
            # shows `fetched_at` so the user can see staleness.
            return {"ok": True, "skipped": "fetch returned None"}

        log.metrics = metrics
        await db.commit()
        return {
            "ok": True,
            "views": metrics.get("views"),
            "forwards": metrics.get("forwards"),
            "reactions_count": len(metrics.get("reactions") or {}),
        }


# Look-back window for the cron sweep. Posts older than this are dropped
# from the refresh loop — TG views plateau after ~7 days and refreshing
# 30-day-old posts every 6h is wasted bandwidth + proxy budget.
_METRICS_LOOKBACK_DAYS = 14
# Number of posts we refresh per cron run. Keeps the run under ~5 minutes
# even on a busy org. If we ever exceed this, the oldest posts in the
# window slide off (which is fine — they're stable by then anyway).
_METRICS_BATCH_LIMIT = 200


async def cleanup_expired_trials(ctx: dict) -> dict[str, Any]:
    """Delete abandoned PREVIEW orgs (never converted to registered).

    A preview org is an anonymous visitor's sandbox. If it sits for
    `PREVIEW_CLEANUP_DAYS` without conversion, it's safe to nuke —
    the visitor isn't coming back. Cascade-deletes through
    Organization → User / Canvas / Project / KnowledgeItem / etc.

    Note: NEVER deletes `kind='trial'` orgs. Trial = registered user
    with real email. After trial expires, the user keeps their data;
    they just lose feature access. The Phase 2 plan layer handles
    that downgrade — not this cron.
    """
    from datetime import timedelta as _td
    from sqlalchemy import delete as _delete

    from app.models.auth import Organization
    from app.models.launch import Launch
    from app.services.trial import PREVIEW_CLEANUP_DAYS

    cutoff = datetime.now(timezone.utc) - _td(days=PREVIEW_CLEANUP_DAYS)
    async with SessionLocal() as db:
        candidates = list(
            (
                await db.scalars(
                    select(Organization.id).where(
                        Organization.kind == "preview",
                        Organization.created_at < cutoff,
                        # Песочница с запуском — не брошенная песочница.
                        # Прогрев строят неделями и до даты продаж в него
                        # не возвращаются каждый день; снести такую
                        # организацию по таймеру значит стереть работу
                        # человека прямо перед запуском.
                        ~select(Launch.id)
                        .where(Launch.organization_id == Organization.id)
                        .exists(),
                    )
                )
            ).all()
        )
        if not candidates:
            return {"ok": True, "deleted": 0}

        await db.execute(
            _delete(Organization).where(Organization.id.in_(candidates))
        )
        await db.commit()

    logger.info(
        "cleanup_expired_trials: deleted %d abandoned preview orgs",
        len(candidates),
    )
    return {"ok": True, "deleted": len(candidates)}


async def pull_telegram_metrics_all(ctx: dict) -> dict[str, Any]:
    """Sweep all recent sent Telegram posts and refresh their metrics.

    Scheduled by Arq cron in `WorkerSettings.cron_jobs` — runs every 6
    hours. Iterates publish_logs with `status='sent'` from the last
    `_METRICS_LOOKBACK_DAYS` days and enqueues `pull_telegram_metrics_one`
    for each. We fan out via the pool rather than awaiting in-line because
    a single sweep can touch hundreds of posts and we'd rather have many
    short jobs than one ~5-minute task that blocks the worker.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_METRICS_LOOKBACK_DAYS)
    started = time.perf_counter()

    async with SessionLocal() as db:
        rows = list(
            (
                await db.scalars(
                    select(PublishLog.id)
                    .where(
                        PublishLog.status == "sent",
                        PublishLog.completed_at.is_not(None),
                        PublishLog.completed_at >= cutoff,
                    )
                    .order_by(PublishLog.completed_at.desc())
                    .limit(_METRICS_BATCH_LIMIT)
                )
            ).all()
        )

    if not rows:
        logger.info("metrics sweep: nothing to refresh")
        return {"ok": True, "scanned": 0, "enqueued": 0}

    # Fan-out via the Arq pool so the sweep itself stays fast and any
    # individual log's fetch can fail without blocking the others. Each
    # child job picks up the same `_METRICS_LOOKBACK_DAYS` window the
    # parent considered, but resolves the row freshly from DB to absorb
    # any updates between sweep-scan and execution.
    from app.workers.queue import get_arq_pool

    pool = await get_arq_pool()
    enqueued = 0
    for log_id in rows:
        try:
            await pool.enqueue_job("pull_telegram_metrics_one", str(log_id))
            enqueued += 1
        except Exception:
            logger.exception("metrics sweep: failed to enqueue %s", log_id)

    elapsed = round(time.perf_counter() - started, 2)
    logger.info(
        "metrics sweep enqueued %d/%d in %.2fs", enqueued, len(rows), elapsed
    )
    return {"ok": True, "scanned": len(rows), "enqueued": enqueued, "elapsed": elapsed}


async def publish_to_telegram(ctx: dict, publish_log_id_str: str) -> dict[str, Any]:
    """Send the format node's full_text to a TelegramTarget. Updates PublishLog."""
    publish_log_id = uuid.UUID(publish_log_id_str)

    async with SessionLocal() as db:
        log = await db.scalar(select(PublishLog).where(PublishLog.id == publish_log_id))
        if log is None:
            return {"ok": False, "error": "PublishLog not found"}

        target = await db.scalar(select(TelegramTarget).where(TelegramTarget.id == log.target_id))
        if target is None:
            log.status = "failed"
            log.error = "Target not found"
            log.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return {"ok": False, "error": "Target not found"}

        log.status = "sending"
        await db.commit()

        try:
            response = await telegram_bot.send_message(target, log.text)
            log.status = "sent"
            log.response = response
            log.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return {"ok": True}
        except Exception as exc:
            logger.exception("telegram publish failed")
            log.status = "failed"
            log.error = str(exc)[:2000]
            log.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return {"ok": False, "error": str(exc)}


async def publish_via_gateway(ctx: dict, publish_log_id_str: str) -> dict[str, Any]:
    """Опубликовать пост через внешний шлюз (Instagram / Threads / X).

    Отдельная задача, а не ветка внутри публикации в Telegram: у них
    разные режимы отказа. Телеграм-бот падает мгновенно и по нашей вине,
    шлюз — по вине площадки и с текстом, который стоит показать целиком
    («Instagram требует Business-аккаунт»).
    """
    publish_log_id = uuid.UUID(publish_log_id_str)

    async with SessionLocal() as db:
        log = await db.scalar(select(PublishLog).where(PublishLog.id == publish_log_id))
        if log is None:
            return {"ok": False, "error": "PublishLog not found"}

        account = await db.scalar(
            select(SocialAccount).where(SocialAccount.id == log.social_account_id)
        )
        if account is None:
            log.status = "failed"
            log.error = "Аккаунт отключён"
            log.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return {"ok": False, "error": "account not found"}

        log.status = "sending"
        await db.commit()

        try:
            response = await publishing.publish_via_gateway(
                platform=account.platform,
                account_external_id=account.external_id,
                text=log.text,
            )
            log.status = "sent"
            log.response = response
            log.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return {"ok": True}
        except Exception as exc:
            logger.exception("gateway publish failed")
            log.status = "failed"
            log.error = str(exc)[:2000]
            log.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return {"ok": False, "error": str(exc)}

