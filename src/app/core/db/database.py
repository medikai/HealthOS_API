from collections.abc import AsyncGenerator
from hashlib import sha256
from time import perf_counter

import structlog
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.asyncio.session import AsyncSession
from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass

from ..config import settings
from ..request_metrics import current_request_metrics

logger = structlog.get_logger(__name__)


class Base(DeclarativeBase, MappedAsDataclass):
    pass


DATABASE_URL = settings.POSTGRES_ASYNC_URL


async_engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_timeout=10,
    connect_args={
        "command_timeout": 20,
        "server_settings": {"statement_timeout": "15000"},
    },
)


@event.listens_for(async_engine.sync_engine, "before_cursor_execute")
def _query_started(
    _conn, _cursor, _statement, _parameters, context, _executemany
) -> None:
    context._healthos_started_at = perf_counter()


@event.listens_for(async_engine.sync_engine, "after_cursor_execute")
def _query_finished(
    _conn, _cursor, statement, _parameters, context, _executemany
) -> None:
    metrics = current_request_metrics()
    if metrics is None:
        return
    duration_ms = round((perf_counter() - context._healthos_started_at) * 1000, 2)
    metrics.query_ms.append(duration_ms)
    logger.info(
        "db_query_complete",
        query_number=len(metrics.query_ms),
        query_ms=duration_ms,
        operation=statement.lstrip().partition(" ")[0].upper(),
        statement_id=sha256(statement.encode()).hexdigest()[:12],
    )


local_session = async_sessionmaker(
    bind=async_engine, class_=AsyncSession, expire_on_commit=False
)


async def async_get_db() -> AsyncGenerator[AsyncSession, None]:
    async with local_session() as db:
        yield db
