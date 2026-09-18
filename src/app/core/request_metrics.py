from contextvars import ContextVar, Token
from dataclasses import dataclass, field


@dataclass(slots=True)
class RequestMetrics:
    auth_ms: float = 0.0
    db_acquire_ms: float = 0.0
    handler_ms: float = 0.0
    query_ms: list[float] = field(default_factory=list)


_metrics: ContextVar[RequestMetrics | None] = ContextVar(
    "request_metrics", default=None
)


def start_request_metrics() -> Token[RequestMetrics | None]:
    return _metrics.set(RequestMetrics())


def current_request_metrics() -> RequestMetrics | None:
    return _metrics.get()


def reset_request_metrics(token: Token[RequestMetrics | None]) -> None:
    _metrics.reset(token)
