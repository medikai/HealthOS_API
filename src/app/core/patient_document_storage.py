import hashlib
from datetime import timedelta
from functools import lru_cache
from typing import Any

import google.auth
from google.auth import impersonated_credentials
from google.auth.credentials import Signing
from google.cloud import storage

from .config import settings

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
UPLOAD_TTL = timedelta(minutes=10)
DOWNLOAD_TTL = timedelta(minutes=1)


class StorageNotConfigured(RuntimeError):
    pass


class StorageObjectTooLarge(ValueError):
    pass


def _bucket_name() -> str:
    if not settings.GCS_BUCKET_NAME:
        raise StorageNotConfigured("GCS bucket is not configured")
    return settings.GCS_BUCKET_NAME


@lru_cache(maxsize=1)
def _adc() -> tuple[Any, str | None]:
    return google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])


@lru_cache(maxsize=1)
def _client() -> storage.Client:
    credentials, project = _adc()
    return storage.Client(project=project, credentials=credentials)


@lru_cache(maxsize=1)
def _signer() -> Signing:
    target = settings.GCS_SIGNING_SERVICE_ACCOUNT
    if not target:
        raise StorageNotConfigured("GCS signing service account is not configured")
    source, _ = _adc()
    source_email = getattr(source, "signer_email", None) or getattr(
        source, "service_account_email", None
    )
    if isinstance(source, Signing) and source_email == target:
        return source
    return impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=target,
        target_scopes=[CLOUD_PLATFORM_SCOPE],
        lifetime=3600,
    )


def create_upload_url(
    object_name: str,
    *,
    document_uuid: str,
    content_type: str,
    sha256: str,
) -> tuple[str, dict[str, str]]:
    bucket_name = _bucket_name()
    signer = _signer()
    headers = {
        "Content-Type": content_type,
        "x-goog-if-generation-match": "0",
        "x-goog-meta-document-uuid": document_uuid,
        "x-goog-meta-sha256": sha256,
    }
    blob = _client().bucket(bucket_name).blob(object_name)
    url = blob.generate_signed_url(
        version="v4",
        expiration=UPLOAD_TTL,
        method="PUT",
        content_type=content_type,
        headers={key: value for key, value in headers.items() if key != "Content-Type"},
        credentials=signer,
        scheme="https",
    )
    return url, headers


def inspect_object(object_name: str) -> dict[str, Any]:
    bucket_name = _bucket_name()
    blob = _client().bucket(bucket_name).blob(object_name)
    blob.reload()
    return {
        "blob": blob,
        "generation": str(blob.generation),
        "size": int(blob.size or 0),
        "content_type": blob.content_type,
        "metadata": dict(blob.metadata or {}),
    }


def inspect_content(
    blob: storage.Blob, generation: str, *, max_bytes: int
) -> dict[str, Any]:
    digest = hashlib.sha256()
    prefix = bytearray()
    size = 0
    with blob.open("rb", if_generation_match=int(generation)) as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise StorageObjectTooLarge
            digest.update(chunk)
            if len(prefix) < 16:
                prefix.extend(chunk[: 16 - len(prefix)])
    return {"prefix": bytes(prefix), "size": size, "sha256": digest.hexdigest()}


def delete_object(object_name: str, generation: str | None = None) -> None:
    bucket_name = _bucket_name()
    blob = (
        _client()
        .bucket(bucket_name)
        .blob(object_name, generation=int(generation) if generation else None)
    )
    kwargs = {"if_generation_match": int(generation)} if generation else {}
    blob.delete(**kwargs)


def create_download_url(
    object_name: str,
    *,
    generation: str,
    filename: str,
    content_type: str,
) -> str:
    bucket_name = _bucket_name()
    signer = _signer()
    blob = _client().bucket(bucket_name).blob(object_name, generation=int(generation))
    safe_filename = filename.replace('"', "")
    return blob.generate_signed_url(
        version="v4",
        expiration=DOWNLOAD_TTL,
        method="GET",
        generation=int(generation),
        response_disposition=f'attachment; filename="{safe_filename}"',
        response_type=content_type,
        credentials=signer,
        scheme="https",
    )
