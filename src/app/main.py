import asyncio
import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from .admin.initialize import create_admin_interface
from .api import router
from .core import events
from .core.config import EnvironmentOption, settings
from .core.setup import create_application, lifespan_factory

logger = structlog.get_logger(__name__)

admin = create_admin_interface()


async def _run_inprocess_worker(stop_event: asyncio.Event) -> None:
    """Local-only outbox dispatcher. See COMMUNICATION_DEV_INPROCESS_DISPATCH."""
    from .domains.communication.delivery.worker import run_forever, worker_owner

    await run_forever(
        owner=f"inprocess-{worker_owner()}",
        poll_seconds=settings.COMMUNICATION_WORKER_POLL_SECONDS,
        stop_event=stop_event,
    )


@asynccontextmanager
async def lifespan_with_admin(app: FastAPI) -> AsyncGenerator[None, None]:
    """Custom lifespan that includes admin initialization."""
    # Get the default lifespan
    # PostgreSQL schemas and application tables are managed exclusively by Alembic.
    # SQLAlchemy's create_all() cannot create the architecture schemas first.
    default_lifespan = lifespan_factory(settings, create_tables_on_start=False)

    # Run the default lifespan initialization and our admin initialization
    async with default_lifespan(app):
        # Initialize admin interface if it exists
        if admin:
            # Initialize admin database and setup
            await admin.initialize()

        events.configure()

        # Development convenience: run the durable delivery outbox in-process so
        # email/OTP works without a second process. Never in production: the
        # standalone worker (python -m ...delivery.worker) owns dispatch there.
        dispatch_stop: asyncio.Event | None = None
        dispatch_task: asyncio.Task[None] | None = None
        if settings.COMMUNICATION_DEV_INPROCESS_DISPATCH:
            if settings.ENVIRONMENT == EnvironmentOption.PRODUCTION:
                logger.warning("communication_inprocess_dispatch_refused_in_production")
            else:
                dispatch_stop = asyncio.Event()
                dispatch_task = asyncio.create_task(_run_inprocess_worker(dispatch_stop))
                logger.info(
                    "communication_inprocess_dispatch_started",
                    poll_seconds=settings.COMMUNICATION_WORKER_POLL_SECONDS,
                )

        try:
            yield
        finally:
            if dispatch_stop is not None:
                dispatch_stop.set()
            if dispatch_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await dispatch_task
            await events.close()


app = create_application(router=router, settings=settings, lifespan=lifespan_with_admin)

# Mount admin interface if enabled
if admin:
    app.mount(settings.CRUD_ADMIN_MOUNT_PATH, admin.app)
