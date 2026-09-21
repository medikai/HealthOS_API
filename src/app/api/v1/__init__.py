from fastapi import APIRouter

from .auth import router as auth_router
from .health import router as health_router
from .organizations import router as organizations_router
from .bootstrap import router as bootstrap_router
from .patients import router as patients_router
from .scheduling import router as scheduling_router
from .queue import router as queue_router
from .encounters import router as encounters_router
from .clinical import router as clinical_router
from .dashboard import router as dashboard_router
from .calendar import router as calendar_router
from .staff import router as staff_router
from .frontend_compat import router as frontend_compat_router
from .facility_schedule import router as facility_schedule_router
from .events import router as events_router
from .masters import router as masters_router
from .documents import router as documents_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(auth_router)
router.include_router(organizations_router)
router.include_router(bootstrap_router)
router.include_router(patients_router)
router.include_router(scheduling_router)
router.include_router(queue_router)
router.include_router(encounters_router)
router.include_router(clinical_router)
router.include_router(dashboard_router)
router.include_router(calendar_router)
router.include_router(staff_router)
router.include_router(frontend_compat_router)
router.include_router(facility_schedule_router)
router.include_router(events_router)
router.include_router(masters_router)
router.include_router(documents_router)
