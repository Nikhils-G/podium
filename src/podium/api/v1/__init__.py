from fastapi import APIRouter

from podium.api.v1 import community, events, integrations, judging, projects, teams

router = APIRouter(prefix="/api/v1")
router.include_router(events.router)
router.include_router(teams.router)
router.include_router(projects.router)
router.include_router(judging.router)
router.include_router(community.router)
router.include_router(integrations.router)
