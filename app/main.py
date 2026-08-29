import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.config import settings
from app.services import activity, alerts

logging.basicConfig(level=settings.LOG_LEVEL)

# httpx на уровне INFO печатает полный URL каждого исходящего запроса, а в
# адресе Telegram API токен бота стоит прямо в пути. Логи хранятся дольше и
# читаются шире, чем файл секретов, поэтому токен туда попадать не должен.
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(
    title="ContentOS API",
    version="0.1.0",
    debug=settings.APP_DEBUG,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def report_activity(request: Request, call_next):
    """Рассказать в Telegram о том, что сделал пользователь.

    Отчёт снимается после ответа и отправляется фоном: наблюдение за
    продуктом не должно ни ломать его, ни замедлять. Что считается
    действием — в `services/activity.py`.
    """
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # Необработанное исключение — самая важная новость из всех, поэтому
        # сообщаем и пробрасываем дальше, а не глотаем.
        took = int((time.perf_counter() - started) * 1000)
        alerts.send(activity.format_event(
            method=request.method, path=request.url.path, status=500,
            actor=_actor(request), duration_ms=took, detail="необработанное исключение",
        ))
        raise

    took = int((time.perf_counter() - started) * 1000)
    if activity.should_report(request.method, request.url.path, response.status_code):
        alerts.send(activity.format_event(
            method=request.method, path=request.url.path,
            status=response.status_code, actor=_actor(request), duration_ms=took,
        ))
    return response


def _actor(request: Request) -> str | None:
    """Кто сделал действие.

    Пользователь достаётся из состояния запроса, если его туда положила
    зависимость авторизации. Разбирать токен здесь заново не будем: это
    работа middleware по наблюдению, а не по аутентификации, и своя копия
    логики разбора рано или поздно разойдётся с настоящей.
    """
    user = getattr(request.state, "user", None)
    if user is not None:
        return getattr(user, "email", None) or str(getattr(user, "id", "")) or None
    return None


app.include_router(api_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": app.version, "env": settings.APP_ENV}
