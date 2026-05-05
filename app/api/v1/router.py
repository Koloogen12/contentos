from fastapi import APIRouter

from app.api.v1 import (
    auth,
    brand_context,
    canvases,
    edges,
    knowledge,
    nodes,
    projects,
    publish,
    skill_runs,
    telegram_targets,
    transcription,
    voice,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(canvases.router)
api_router.include_router(nodes.router)
api_router.include_router(edges.router)
api_router.include_router(skill_runs.router)
api_router.include_router(projects.router)
api_router.include_router(brand_context.router)
api_router.include_router(knowledge.router)
api_router.include_router(transcription.router)
api_router.include_router(telegram_targets.router)
api_router.include_router(publish.router)
api_router.include_router(voice.router)
