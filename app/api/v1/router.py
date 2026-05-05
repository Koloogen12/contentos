from fastapi import APIRouter

from app.api.v1 import auth, canvases, edges, nodes

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(canvases.router)
api_router.include_router(nodes.router)
api_router.include_router(edges.router)
