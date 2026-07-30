"""Arq worker entry."""
from __future__ import annotations

from arq.connections import RedisSettings, create_pool
from arq.cron import cron

from app.config import settings
from app.workers.tasks import (
    cleanup_expired_trials,
    publish_to_telegram,
    pull_telegram_metrics_all,
    pull_telegram_metrics_one,
    render_carousel,
    run_skill,
    slide_tweak,
)

_pool = None


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.REDIS_URL)


async def get_arq_pool():
    """Lazily-created Arq Redis pool used by API endpoints to enqueue jobs."""
    global _pool
    if _pool is None:
        _pool = await create_pool(_redis_settings())
    return _pool


async def startup(ctx: dict) -> None:
    # Playwright launches Chromium via `asyncio.create_subprocess_exec`,
    # which on Python 3.11 goes through the event-loop policy's
    # ChildWatcher API. Inside arq's worker the policy doesn't init a
    # watcher automatically (the stdlib `_init_watcher` only attaches in
    # the main thread under the right conditions, which arq doesn't
    # always satisfy). Without intervention the very first carousel
    # render crashes mid-`async_playwright().start()` with
    # `NotImplementedError` from `BaseDefaultEventLoopPolicy.get_child_watcher`.
    #
    # We attach a ThreadedChildWatcher to the running loop here. The
    # policy class on Linux has its own (working) `set_child_watcher`,
    # but if we ever end up with `BaseDefaultEventLoopPolicy` itself or
    # uvloop's policy, we fall back to a per-loop attach that's still
    # sufficient — Playwright reads the watcher via
    # `events.get_child_watcher()`, which uses the POLICY accessor, so
    # we also set a module-level singleton via the policy when possible.
    import asyncio
    import logging

    log = logging.getLogger(__name__)
    loop = asyncio.get_running_loop()
    policy = asyncio.get_event_loop_policy()
    log.info(
        "worker startup: loop=%s policy=%s",
        type(loop).__name__,
        type(policy).__name__,
    )
    try:
        watcher = asyncio.ThreadedChildWatcher()
        watcher.attach_loop(loop)
    except Exception as exc:
        log.warning("worker startup: ThreadedChildWatcher init failed: %r", exc)
        return
    # Install on the policy so `asyncio.get_child_watcher()` returns it.
    # Some policies (uvloop, base) raise NotImplementedError from
    # `set_child_watcher` — in that case the per-loop attach above still
    # works for the running loop, but `get_child_watcher()` won't find
    # the watcher and Playwright's subprocess_exec will still fail. Log
    # loudly so the failure mode is obvious in logs.
    try:
        policy.set_child_watcher(watcher)
        log.info(
            "worker startup: attached ThreadedChildWatcher via policy.%s",
            type(policy).__name__,
        )
    except NotImplementedError:
        log.error(
            "worker startup: policy %s does not implement set_child_watcher "
            "(Playwright subprocess calls WILL fail) — falling back to "
            "manual asyncio.events._set_running_loop()-style override",
            type(policy).__name__,
        )
        # Last-ditch: monkey-patch events.get_child_watcher to return ours.
        # This is grim but contained: only this worker process is affected.
        import asyncio.events as _events
        _events.get_child_watcher = lambda: watcher  # type: ignore[assignment]
        log.info("worker startup: monkey-patched asyncio.events.get_child_watcher")
    except Exception as exc:  # pragma: no cover — guard for non-{NIE,AE}
        log.warning(
            "worker startup: unexpected error setting child watcher: %r", exc
        )


async def shutdown(ctx: dict) -> None:
    pass


class WorkerSettings:
    functions = [
        run_skill,
        publish_to_telegram,
        render_carousel,
        slide_tweak,
        pull_telegram_metrics_one,
        pull_telegram_metrics_all,
        cleanup_expired_trials,
    ]
    cron_jobs = [
        # Telegram metrics sweep — 4× daily, see comments in task body.
        cron(
            pull_telegram_metrics_all,
            hour={0, 6, 12, 18},
            minute=0,
            run_at_startup=True,
            unique=True,
        ),
        # Trial cleanup — hourly. Each run scans for orgs whose
        # `trial_expires_at + GRACE_HOURS` is in the past and cascade-
        # deletes them. Fast (single indexed query), idempotent.
        cron(
            cleanup_expired_trials,
            minute=17,  # off-peak vs other crons, avoids minute-0 herd
            run_at_startup=True,
            unique=True,
        ),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    max_jobs = 10
    # Carousel renders run gpt-image-1 (~15s) + 5-10 Playwright screenshots
    # (~2s each) + S3 uploads (~1s each). Total worst-case is ~60s for
    # a 10-slide carousel, but we bump to 600s to absorb any retry/backoff
    # without losing the job.
    job_timeout = 600
