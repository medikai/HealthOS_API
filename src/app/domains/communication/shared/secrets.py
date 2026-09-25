"""App-level encryption for short-lived secret email context.

OTPs and reset URLs must never land in ordinary outbox payloads, audit rows or
logs. They are stored only as an encrypted ``email_message.encrypted_context``
and erased once a terminal delivery outcome is reached.
"""

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ....core.config import settings

SENSITIVE_FIELD_PARTS = ("otp", "code", "token", "url", "secret", "password")


class SecretContextError(ValueError):
    code = "SECRET_CONTEXT_INVALID"


def _key_material() -> bytes:
    explicit = settings.COMMUNICATION_SECRET_CONTEXT_KEY
    raw = (
        explicit.get_secret_value()
        if explicit is not None
        else settings.SECRET_KEY.get_secret_value()
    )
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())


def _fernet() -> Fernet:
    return Fernet(_key_material())


def encrypt_secret_context(data: dict[str, Any]) -> str:
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _fernet().encrypt(payload).decode("ascii")


def decrypt_secret_context(token: str) -> dict[str, Any]:
    try:
        raw = _fernet().decrypt(token.encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise SecretContextError("Secret context could not be decrypted.") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SecretContextError("Secret context is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise SecretContextError("Secret context must be an object.")
    return value


def is_sensitive_field(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in SENSITIVE_FIELD_PARTS)
