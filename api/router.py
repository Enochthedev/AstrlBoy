"""
FastAPI router — mounts all route groups.
"""

from fastapi import APIRouter

from api.routes.applications import router as applications_router
from api.routes.content import router as content_router
from api.routes.contracts import router as contracts_router
from api.routes.experiments import router as experiments_router
from api.routes.health import router as health_router
from api.routes.skills import router as skills_router
from api.routes.trends import router as trends_router

api_router = APIRouter()

api_router.include_router(health_router)
api_router.include_router(contracts_router)
api_router.include_router(content_router)
api_router.include_router(experiments_router)
api_router.include_router(applications_router)
api_router.include_router(skills_router)
api_router.include_router(trends_router)
