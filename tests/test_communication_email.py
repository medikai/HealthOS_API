"""Email subsystem tests with fake providers.

Covers template escaping/injection, secret-context encryption, ZeptoMail
payload/header/base URL and error mapping, delivery metadata transitions and
the internal enqueue contract. No real mail is sent and no DB is touched.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from src.app.domains.communication.email.diagnostic import build_diagnostic_request
from src.app.domains.communication.email.providers.zeptomail import (
    EmailDeliveryProvider,
    ZeptoMailClient,
)
from src.app.domains.communication.email.repository import EmailDeliveryRepository
from src.app.domains.communication.email.schemas import (
    EmailProviderRequest,
    EmailProviderResult,
)
from src.app.domains.communication.email.service import EmailService
from src.app.domains.communication.email.templates import (
    InvalidTemplateContext,
    UnsupportedTemplate,
    render_template,
)
from src.app.domains.communication.shared.errors import (
    PermanentDeliveryError,
    ProviderUnavailable,
    TransientDeliveryError,
)
from src.app.domains.communication.shared.secrets import (
    SecretContextError,
    decrypt_secret_context,
    encrypt_secret_context,
)
from src.app.models.communication import (
    EMAIL_STATUS_ACCEPTED,
    EMAIL_STATUS_FAILED,
    EMAIL_STATUS_UNKNOWN,
    EmailMessage,
)


def run(coro):
    return asyncio.run(coro)


def _sample_request() -> EmailProviderRequest:
    return EmailProviderRequest(
        to_email="test@example.com",
        to_name="Test user",
        from_email="noreply@medikai.in",
        from_name="Medikai Infodesk",
        subject="HealthOS email test",
        html_body="<p>hi</p>",
        text_body="hi",
    )


def _client(handler, *, token="test-token", base="https://api.zeptomail.test") -> ZeptoMailClient:
    return ZeptoMailClient(
        base_url=base, token=token, timeout=1.0, transport=httpx.MockTransport(handler)
    )


# ---------------------------------------------------------------- templates


def test_template_escapes_html_but_text_is_plain():
    rendered = render_template("email_test", {"note": "<script>alert(1)</script>"})
    assert "&lt;script&gt;" in rendered.html_body
    assert "<script>" not in rendered.html_body
    assert "<script>alert(1)</script>" in rendered.text_body


def test_template_rejects_unknown_and_missing_fields():
    with pytest.raises(InvalidTemplateContext):
        render_template("email_test", {"note": "x", "extra": "y"})
    with pytest.raises(InvalidTemplateContext):
        render_template("email_test", {})
    with pytest.raises(UnsupportedTemplate):
        render_template("not_approved", {"note": "x"})


def test_template_rejects_header_injection_in_subject():
    with pytest.raises(ValueError):
        render_template(
            "staff_invitation",
            {"organization_name": "Acme\r\nBcc: evil@example.com", "invite_url": "https://x/y"},
        )


def test_diagnostic_request_is_escaped_and_labelled():
    request = build_diagnostic_request(
        to_email="test@example.com",
        to_name="Test user",
        note="<b>note</b>",
        from_email="noreply@medikai.in",
        from_name="Medikai Infodesk",
    )
    assert request.subject == "HealthOS email test"
    assert "&lt;b&gt;note&lt;/b&gt;" in request.html_body
    assert request.provider == "zeptomail"


# ---------------------------------------------------------------- secrets


def test_secret_context_roundtrip_and_tamper_rejection():
    token = encrypt_secret_context({"code": "123456"})
    assert "123456" not in token
    assert decrypt_secret_context(token) == {"code": "123456"}
    with pytest.raises(SecretContextError):
        decrypt_secret_context("not-a-valid-token")


# ---------------------------------------------------------------- ZeptoMail client


def test_client_posts_json_to_expected_url_with_auth_header():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(202, json={"request_id": "req-1"})

    result = run(_client(handler).send(_sample_request()))

    assert captured["url"] == "https://api.zeptomail.test/v1.1/email"
    assert captured["headers"]["authorization"] == "Zoho-enczapikey test-token"
    body = captured["body"]
    assert body["from"]["address"] == "noreply@medikai.in"
    assert body["to"][0]["email_address"]["address"] == "test@example.com"
    assert body["htmlbody"] == "<p>hi</p>"
    assert body["textbody"] == "hi"
    assert "test-token" not in json.dumps(body)
    assert result.accepted is True
    assert result.provider_request_id == "req-1"


def test_client_trims_trailing_slash_from_base_url():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={})

    run(_client(handler, base="https://api.zeptomail.com/").send(_sample_request()))
    assert captured["url"] == "https://api.zeptomail.com/v1.1/email"


def test_client_rate_limit_honours_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"})

    with pytest.raises(TransientDeliveryError) as excinfo:
        run(_client(handler).send(_sample_request()))
    assert excinfo.value.retry_after_seconds == 7
    assert "test-token" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(500, TransientDeliveryError), (503, TransientDeliveryError), (400, PermanentDeliveryError), (401, PermanentDeliveryError)],
)
def test_client_error_classification(status, error_type):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"})

    with pytest.raises(error_type):
        run(_client(handler).send(_sample_request()))


def test_client_timeout_and_network_are_transient():
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    def network_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(TransientDeliveryError) as timeout_exc:
        run(_client(timeout_handler).send(_sample_request()))
    assert timeout_exc.value.reason == "zeptomail_timeout"

    with pytest.raises(TransientDeliveryError) as network_exc:
        run(_client(network_handler).send(_sample_request()))
    assert network_exc.value.reason == "zeptomail_network"


def test_client_repr_redacts_token_and_unconfigured_raises():
    client = _client(lambda request: httpx.Response(200), token="super-secret-token")
    assert "super-secret-token" not in repr(client)
    unconfigured = ZeptoMailClient(base_url="https://api.zeptomail.test", token=None)
    assert unconfigured.configured is False
    with pytest.raises(ProviderUnavailable):
        run(unconfigured.send(_sample_request()))


# ---------------------------------------------------------------- delivery provider


class _FakeDb:
    def __init__(self, message: EmailMessage) -> None:
        self.message = message
        self.commits = 0

    async def get(self, model, pk):
        return self.message if pk == self.message.id else None

    async def commit(self) -> None:
        self.commits += 1


class _FakeSessionContext:
    def __init__(self, message: EmailMessage) -> None:
        self.db = _FakeDb(message)

    async def __aenter__(self) -> _FakeDb:
        return self.db

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _FakeSessionFactory:
    def __init__(self, message: EmailMessage) -> None:
        self.message = message

    def __call__(self) -> _FakeSessionContext:
        return _FakeSessionContext(self.message)


class _FakeClient:
    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.requests: list[EmailProviderRequest] = []

    @property
    def configured(self) -> bool:
        return True

    async def send(self, request: EmailProviderRequest) -> EmailProviderResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _message(**overrides) -> EmailMessage:
    values = {
        "dedup_key": "dedup-1",
        "template_code": "password_reset_code",
        "recipient_email": "test@example.com",
        "recipient_name": "Test user",
        "from_email": "noreply@medikai.in",
        "from_name": "Medikai Infodesk",
        "context": {"recipient_name": "Test user", "expires_minutes": "10"},
        "encrypted_context": encrypt_secret_context({"code": "123456"}),
        "is_secret": True,
    }
    values.update(overrides)
    return EmailMessage(**values)


def _provider(client: _FakeClient, message: EmailMessage) -> EmailDeliveryProvider:
    return EmailDeliveryProvider(
        client=client,
        repository=EmailDeliveryRepository(),
        session_factory=_FakeSessionFactory(message),
    )


def _job(message: EmailMessage):
    return SimpleNamespace(payload={"email_message_id": str(message.id)})


def test_provider_marks_accepted_and_erases_secret():
    message = _message()
    client = _FakeClient(
        result=EmailProviderResult(
            provider="zeptomail", accepted=True, status_code=202, provider_request_id="r1"
        )
    )
    result = run(_provider(client, message).deliver(_job(message)))
    assert result["accepted"] is True
    assert message.status == EMAIL_STATUS_ACCEPTED
    assert message.encrypted_context is None
    assert message.accepted_at is not None
    assert "123456" in client.requests[0].html_body  # secret rendered only at send


def test_provider_transient_failure_keeps_secret_for_retry():
    message = _message()
    client = _FakeClient(error=TransientDeliveryError("zeptomail_server_error"))
    with pytest.raises(TransientDeliveryError):
        run(_provider(client, message).deliver(_job(message)))
    assert message.status == EMAIL_STATUS_FAILED
    assert message.encrypted_context is not None
    assert message.last_error == "DELIVERY_TRANSIENT:zeptomail_server_error"


def test_provider_timeout_marks_unknown_and_keeps_secret():
    message = _message()
    client = _FakeClient(error=TransientDeliveryError("zeptomail_timeout"))
    with pytest.raises(TransientDeliveryError):
        run(_provider(client, message).deliver(_job(message)))
    assert message.status == EMAIL_STATUS_UNKNOWN
    assert message.encrypted_context is not None


def test_provider_permanent_failure_clears_secret():
    message = _message()
    client = _FakeClient(error=PermanentDeliveryError("zeptomail_rejected_400"))
    with pytest.raises(PermanentDeliveryError):
        run(_provider(client, message).deliver(_job(message)))
    assert message.status == EMAIL_STATUS_FAILED
    assert message.encrypted_context is None


def test_provider_rejects_expired_secret_context_without_sending():
    message = _message(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    client = _FakeClient(result=EmailProviderResult(provider="zeptomail", accepted=True))
    with pytest.raises(PermanentDeliveryError) as excinfo:
        run(_provider(client, message).deliver(_job(message)))
    assert excinfo.value.reason == "email_context_expired"
    assert client.requests == []


def test_provider_rejects_job_without_message_id():
    client = _FakeClient(result=EmailProviderResult(provider="zeptomail", accepted=True))
    provider = EmailDeliveryProvider(
        client=client, session_factory=_FakeSessionFactory(_message())
    )
    with pytest.raises(PermanentDeliveryError) as excinfo:
        run(provider.deliver(SimpleNamespace(payload={})))
    assert excinfo.value.reason == "email_message_missing"


# ---------------------------------------------------------------- EmailService


class _RecordingEmailRepository:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_message(self, db, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id=uuid4(), **kwargs), True


class _RecordingDeliveryRepository:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    async def enqueue(self, db, **kwargs):
        self.enqueued.append(kwargs)
        return SimpleNamespace(), True


def _service(*, configured: bool = True) -> EmailService:
    return EmailService(
        repository=_RecordingEmailRepository(),
        delivery_repository=_RecordingDeliveryRepository(),
        configured=configured,
        from_email="noreply@medikai.in",
        from_name="Medikai Infodesk",
        max_attempts=5,
    )


def test_service_unconfigured_fails_closed():
    with pytest.raises(ProviderUnavailable):
        run(
            _service(configured=False).enqueue_template(
                None,
                template_code="email_test",
                to_email="test@example.com",
                dedup_key="d1",
                context={"note": "x"},
            )
        )


def test_service_splits_secret_context_and_keeps_outbox_minimal():
    service = _service()
    _, created = run(
        service.enqueue_template(
            None,
            template_code="password_reset_code",
            to_email="test@example.com",
            to_name="Test user",
            dedup_key="pw-1",
            context={"code": "123456", "expires_minutes": "10", "recipient_name": "Test user"},
        )
    )
    assert created is True
    stored = service.repository.created[0]
    assert "code" not in stored["context"]
    assert stored["context"] == {"expires_minutes": "10", "recipient_name": "Test user"}
    assert stored["is_secret"] is True
    assert "123456" not in stored["encrypted_context"]

    payload = service.delivery_repository.enqueued[0]["payload"]
    assert set(payload) == {"email_message_id"}
    assert isinstance(payload["email_message_id"], str)
    assert "123456" not in json.dumps(payload)


def test_service_rejects_invalid_template_context():
    service = _service()
    with pytest.raises(InvalidTemplateContext):
        run(
            service.enqueue_template(
                None,
                template_code="email_test",
                to_email="test@example.com",
                dedup_key="d1",
                context={"note": "x", "unknown": "y"},
            )
        )
    with pytest.raises(UnsupportedTemplate):
        run(
            service.enqueue_template(
                None,
                template_code="not_approved",
                to_email="test@example.com",
                dedup_key="d2",
                context={},
            )
        )
