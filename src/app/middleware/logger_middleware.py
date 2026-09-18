# app/middleware/request_id.py
import uuid
from time import perf_counter

import structlog
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from ..core.request_metrics import (
    current_request_metrics,
    reset_request_metrics,
    start_request_metrics,
)

logger = structlog.get_logger(__name__)


class LoggerMiddleware(BaseHTTPMiddleware):
    """Middleware to add request ID to the context variables.

    Parameters
    ----------
    app: FastAPI
        The FastAPI application instance.
    """

    def __init__(self, app: FastAPI) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """
        Add request ID to the context variables.
        """
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            client_host=request.client.host if request.client else None,
            status_code=None,
            path=request.url.path,
            method=request.method,
        )
        metrics_token = start_request_metrics()
        started_at = perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            duration_ms = round((perf_counter() - started_at) * 1000, 2)
            metrics = current_request_metrics()
            auth_ms = metrics.auth_ms if metrics else 0.0
            handler_ms = metrics.handler_ms if metrics else 0.0
            query_ms = metrics.query_ms if metrics else []
            structlog.contextvars.bind_contextvars(
                status_code=response.status_code if response else 500
            )
            logger.info(
                "request_complete",
                duration_ms=duration_ms,
                auth_ms=auth_ms,
                db_acquire_ms=metrics.db_acquire_ms if metrics else 0.0,
                handler_ms=handler_ms,
                response_finalize_ms=round(
                    max(0.0, duration_ms - auth_ms - handler_ms), 2
                ),
                query_count=len(query_ms),
                query_total_ms=round(sum(query_ms), 2),
                query_ms=query_ms,
            )
            reset_request_metrics(metrics_token)
