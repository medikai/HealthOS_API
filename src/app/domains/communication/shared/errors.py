class CommunicationError(RuntimeError):
    """Base class for communication-domain failures with a stable code."""

    code = "COMMUNICATION_ERROR"


class ProviderUnavailable(CommunicationError):
    """Raised when an external provider is not configured or unreachable.

    Callers must surface this as an unavailable feature. Never substitute a
    dummy successful token or a fake delivery acknowledgement.
    """

    code = "PROVIDER_UNAVAILABLE"

    def __init__(self, reason: str, message: str = "Provider is unavailable.") -> None:
        super().__init__(message)
        self.reason = reason


class DeliveryError(CommunicationError):
    """Base delivery failure carrying a retry classification."""

    code = "DELIVERY_ERROR"
    retryable = True
    reason = "delivery_error"


class TransientDeliveryError(DeliveryError):
    """Retryable provider failure (5xx/429/network). Honours Retry-After."""

    code = "DELIVERY_TRANSIENT"
    retryable = True

    def __init__(
        self,
        reason: str,
        message: str = "Transient delivery failure.",
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


class PermanentDeliveryError(DeliveryError):
    """Non-retryable provider failure (validation/auth/rejected)."""

    code = "DELIVERY_PERMANENT"
    retryable = False

    def __init__(self, reason: str, message: str = "Permanent delivery failure.") -> None:
        super().__init__(message)
        self.reason = reason
