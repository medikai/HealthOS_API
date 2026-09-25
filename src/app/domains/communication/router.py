"""Communication domain router composition.

Each subsystem contributes its own router; only implemented subsystems are
included. Chat/notifications/email/push routers land in later steps.
"""

from fastapi import APIRouter

from .chat.router import router as chat_router
from .notifications.router import router as notifications_router
from .push.router import router as push_router
from .realtime.router import router as realtime_router
from .status.router import router as status_router

router = APIRouter(prefix="/communication")
router.include_router(realtime_router)
router.include_router(notifications_router)
router.include_router(chat_router)
router.include_router(status_router)
router.include_router(push_router)
