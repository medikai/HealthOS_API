import hashlib
import inspect
import io
import unittest
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from fastapi import HTTPException
from google.api_core import exceptions as google_exceptions
from google.auth import exceptions as google_auth_exceptions
from google.auth.credentials import Credentials, Signing
from google.cloud import storage as google_storage
from pydantic import ValidationError

from src.app.api.v1 import documents
from src.app.api.v1.clinical import sign_soap
from src.app.core import patient_document_storage as storage
from src.app.models.care import PatientDocument
from src.app.schemas.documents import PatientDocumentUploadInput


def entity(number: int, **values):
    return SimpleNamespace(id=UUID(int=number), **values)


def request():
    return SimpleNamespace(
        url_for=lambda name, **values: (
            f"http://localhost:8000/api/v1/documents/{values['document_uuid']}/download"
        )
    )


def upload_payload(**changes):
    values = {
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "size_bytes": 12,
        "sha256": "a" * 64,
        "category": "lab_report",
        "title": "Historical report",
        "document_date": datetime.now(UTC).date(),
    }
    values.update(changes)
    return PatientDocumentUploadInput.model_validate(values)


def encounter():
    return entity(
        1,
        organization_id=UUID(int=10),
        facility_id=UUID(int=20),
        patient_id=UUID(int=30),
        practitioner_id=UUID(int=40),
        started_at=datetime(2026, 9, 20, tzinfo=UTC),
    )


def document(**changes):
    values = {
        "id": UUID(int=2),
        "organization_id": UUID(int=10),
        "patient_id": UUID(int=30),
        "encounter_id": UUID(int=1),
        "uploaded_by_user_id": UUID(int=50),
        "category": "lab_report",
        "title": "Historical report",
        "document_date": date(2026, 9, 19),
        "original_filename": "report.pdf",
        "declared_mime_type": "application/pdf",
        "expected_size_bytes": 12,
        "expected_sha256": "a" * 64,
        "storage_key": "patient-documents/random-object",
        "status": "uploading",
        "mime_type": None,
        "size_bytes": None,
        "sha256": None,
        "storage_generation": None,
        "rejection_reason": None,
        "uploaded_at": datetime(2026, 9, 20, tzinfo=UTC),
        "confirmed_at": None,
        "scanned_at": None,
        "updated_at": None,
        "deleted_at": None,
        "deleted_by_user_id": None,
        "storage_deleted_at": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def document_row(doc, enc=None):
    enc = enc or encounter()
    return (
        doc,
        entity(50, display_name="Clinician", email="clinician@example.test"),
        enc,
        entity(40, person_name="Dr Test"),
        entity(20, name="Test Facility"),
    )


class PatientDocumentValidationTests(unittest.TestCase):
    def test_metadata_validation_and_spoofed_patient_are_rejected(self):
        invalid = [
            {"content_type": "text/html"},
            {"category": "clinical_note"},
            {"sha256": "not-a-hash"},
            {"title": " "},
            {"patient_uuid": str(UUID(int=99))},
        ]
        for changes in invalid:
            values = upload_payload().model_dump()
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                PatientDocumentUploadInput.model_validate(values)

    def test_magic_byte_detection_rejects_executable_content(self):
        self.assertEqual(documents._detected_mime(b"%PDF-1.7"), "application/pdf")
        self.assertEqual(documents._detected_mime(b"\x89PNG\r\n\x1a\n"), "image/png")
        self.assertEqual(documents._detected_mime(b"\xff\xd8\xff"), "image/jpeg")
        self.assertIsNone(documents._detected_mime(b"MZ\x90\x00<script>"))

    def test_filename_is_sanitized_and_normalized_to_detected_type(self):
        self.assertEqual(
            documents._safe_filename("../unsafe<script>.exe", "application/pdf"),
            "unsafe_script.pdf",
        )

    def test_model_has_required_foreign_keys_constraints_and_indexes(self):
        table = PatientDocument.__table__
        foreign_keys = {fk.target_fullname for fk in table.foreign_keys}
        self.assertTrue(
            {
                "organization.organization.id",
                "identity.patient.id",
                "care.encounter.id",
                "identity.user_account.id",
            }.issubset(foreign_keys)
        )
        indexes = {
            index.name: tuple(column.name for column in index.columns)
            for index in table.indexes
        }
        self.assertEqual(
            indexes["ix_care_patient_document_patient_date"],
            ("patient_id", "document_date"),
        )
        self.assertIn("ix_care_patient_document_encounter_id", indexes)


class PatientDocumentStorageTests(unittest.TestCase):
    def tearDown(self):
        storage._adc.cache_clear()
        storage._client.cache_clear()
        storage._signer.cache_clear()

    def test_signed_upload_has_exact_headers_and_ten_minute_expiry(self):
        blob = Mock()
        blob.generate_signed_url.return_value = "https://storage.test/upload"
        client = Mock()
        client.bucket.return_value.blob.return_value = blob
        with (
            patch.object(storage.settings, "GCS_BUCKET_NAME", "private-bucket"),
            patch.object(storage, "_client", return_value=client),
            patch.object(storage, "_signer", return_value=object()),
        ):
            url, headers = storage.create_upload_url(
                "patient-documents/random",
                document_uuid="document-id",
                content_type="application/pdf",
                sha256="a" * 64,
            )

        self.assertEqual(url, "https://storage.test/upload")
        self.assertEqual(
            headers,
            {
                "Content-Type": "application/pdf",
                "x-goog-if-generation-match": "0",
                "x-goog-meta-document-uuid": "document-id",
                "x-goog-meta-sha256": "a" * 64,
            },
        )
        kwargs = blob.generate_signed_url.call_args.kwargs
        self.assertEqual(kwargs["method"], "PUT")
        self.assertEqual(kwargs["expiration"], timedelta(minutes=10))
        self.assertEqual(kwargs["content_type"], "application/pdf")
        self.assertNotIn("Authorization", headers)

    def test_v4_upload_uses_iam_style_signing_without_a_private_key(self):
        class IAMSigner(Credentials, Signing):
            def __init__(self):
                super().__init__()
                self.messages = []

            @property
            def signer(self):
                return self

            @property
            def signer_email(self):
                return "signer@example.test"

            def sign_bytes(self, message):
                self.messages.append(message)
                return b"iam-sign-blob-result"

            def refresh(self, request):
                self.token = "unused"

        signer = IAMSigner()
        client = google_storage.Client(project="test-project", credentials=signer)
        with (
            patch.object(storage.settings, "GCS_BUCKET_NAME", "private-bucket"),
            patch.object(storage, "_client", return_value=client),
            patch.object(storage, "_signer", return_value=signer),
        ):
            url, _ = storage.create_upload_url(
                "patient-documents/random",
                document_uuid="document-id",
                content_type="application/pdf",
                sha256="a" * 64,
            )
        self.assertIn("X-Goog-Algorithm=GOOG4-RSA-SHA256", url)
        self.assertIn("X-Goog-Expires=600", url)
        self.assertEqual(len(signer.messages), 1)

    def test_confirmation_reader_streams_with_generation_and_size_limit(self):
        blob = Mock()
        blob.open.return_value = io.BytesIO(b"%PDF-streamed")
        result = storage.inspect_content(blob, "42", max_bytes=20)
        self.assertEqual(result["size"], 13)
        self.assertEqual(result["sha256"], hashlib.sha256(b"%PDF-streamed").hexdigest())
        blob.open.assert_called_once_with("rb", if_generation_match=42)

        blob.open.return_value = io.BytesIO(b"too large")
        with self.assertRaises(storage.StorageObjectTooLarge):
            storage.inspect_content(blob, "42", max_bytes=2)

    def test_signed_download_is_generation_bound_and_expires_in_one_minute(self):
        blob = Mock()
        blob.generate_signed_url.return_value = "https://storage.test/download"
        client = Mock()
        client.bucket.return_value.blob.return_value = blob
        with (
            patch.object(storage.settings, "GCS_BUCKET_NAME", "private-bucket"),
            patch.object(storage, "_client", return_value=client),
            patch.object(storage, "_signer", return_value=object()),
        ):
            url = storage.create_download_url(
                "patient-documents/random",
                generation="42",
                filename='safe"name.pdf',
                content_type="application/pdf",
            )
        self.assertEqual(url, "https://storage.test/download")
        client.bucket.return_value.blob.assert_called_once_with(
            "patient-documents/random", generation=42
        )
        kwargs = blob.generate_signed_url.call_args.kwargs
        self.assertEqual(kwargs["expiration"], timedelta(minutes=1))
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["generation"], 42)
        self.assertEqual(
            kwargs["response_disposition"], 'attachment; filename="safename.pdf"'
        )

    def test_local_impersonated_adc_signer_is_reused_without_private_key(self):
        class AlreadySigning:
            signer_email = "signer@example.test"

        source = AlreadySigning()
        with (
            patch.object(
                storage.settings, "GCS_SIGNING_SERVICE_ACCOUNT", source.signer_email
            ),
            patch.object(storage, "Signing", AlreadySigning),
            patch.object(storage, "_adc", return_value=(source, None)),
        ):
            self.assertIs(storage._signer(), source)

    def test_workload_adc_is_wrapped_for_iam_sign_blob(self):
        source = object()
        wrapped = object()
        with (
            patch.object(
                storage.settings, "GCS_SIGNING_SERVICE_ACCOUNT", "signer@example.test"
            ),
            patch.object(storage, "_adc", return_value=(source, "project")),
            patch.object(
                storage.impersonated_credentials,
                "Credentials",
                return_value=wrapped,
            ) as credentials,
        ):
            self.assertIs(storage._signer(), wrapped)
        credentials.assert_called_once_with(
            source_credentials=source,
            target_principal="signer@example.test",
            target_scopes=[storage.CLOUD_PLATFORM_SCOPE],
            lifetime=3600,
        )

    def test_missing_configuration_credentials_and_signing_map_to_safe_503(self):
        with (
            patch.object(storage.settings, "GCS_BUCKET_NAME", None),
            self.assertRaises(storage.StorageNotConfigured),
        ):
            storage._bucket_name()
        for exc, code in (
            (
                google_auth_exceptions.DefaultCredentialsError("missing"),
                "GCS_CREDENTIALS_UNAVAILABLE",
            ),
            (google_exceptions.Forbidden("denied"), "GCS_SIGNING_UNAVAILABLE"),
        ):
            mapped = documents._storage_error(
                exc, signing=isinstance(exc, google_exceptions.Forbidden)
            )
            self.assertEqual(mapped.status_code, 503)
            self.assertEqual(mapped.detail["code"], code)


class PatientDocumentUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_initiation_derives_patient_and_returns_random_non_phi_key(self):
        enc = encounter()
        db = SimpleNamespace(
            scalar=AsyncMock(return_value="UTC"),
            add=Mock(),
            flush=AsyncMock(),
            commit=AsyncMock(),
            rollback=AsyncMock(),
        )
        headers = {"Content-Type": "application/pdf"}
        with (
            patch.object(documents, "_encounter", AsyncMock(return_value=enc)),
            patch.object(documents, "_require_draft_soap", AsyncMock()),
            patch.object(
                documents,
                "run_in_threadpool",
                AsyncMock(return_value=("https://storage.test/upload", headers)),
            ),
        ):
            response = await documents.initiate_document_upload(
                enc.id,
                upload_payload(filename="Patient Name 999.pdf"),
                entity(50),
                db,
            )

        created = db.add.call_args.args[0]
        self.assertEqual(created.patient_id, enc.patient_id)
        self.assertEqual(created.organization_id, enc.organization_id)
        self.assertRegex(created.storage_key, r"^patient-documents/[0-9a-f]{32}$")
        self.assertNotIn("Patient", created.storage_key)
        self.assertEqual(response["data"]["status"], "uploading")
        self.assertEqual(response["data"]["upload"]["headers"], headers)

    async def test_initiation_authorization_and_limits_fail_before_signing(self):
        db = SimpleNamespace()
        with self.assertRaises(HTTPException) as too_large:
            await documents.initiate_document_upload(
                UUID(int=1),
                upload_payload(size_bytes=documents.MAX_DOCUMENT_BYTES + 1),
                entity(50),
                db,
            )
        self.assertEqual(too_large.exception.status_code, 413)

        with (
            patch.object(
                documents,
                "_encounter",
                AsyncMock(
                    side_effect=documents._error(
                        404, "NOT_FOUND", "Encounter not found."
                    )
                ),
            ),
            self.assertRaises(HTTPException) as unauthorized,
        ):
            await documents.initiate_document_upload(
                UUID(int=99), upload_payload(), entity(50), db
            )
        self.assertEqual(unauthorized.exception.status_code, 404)

    async def test_future_document_date_is_rejected(self):
        enc = encounter()
        db = SimpleNamespace(scalar=AsyncMock(return_value="UTC"))
        with (
            patch.object(documents, "_encounter", AsyncMock(return_value=enc)),
            patch.object(documents, "_require_draft_soap", AsyncMock()),
            self.assertRaises(HTTPException) as raised,
        ):
            await documents.initiate_document_upload(
                enc.id,
                upload_payload(
                    document_date=datetime.now(UTC).date() + timedelta(days=1)
                ),
                entity(50),
                db,
            )
        self.assertEqual(raised.exception.status_code, 422)

    async def test_confirmation_verifies_bytes_and_becomes_available(self):
        enc = encounter()
        data = b"%PDF-1.7data"
        doc = document(
            expected_size_bytes=len(data),
            expected_sha256=hashlib.sha256(data).hexdigest(),
        )
        db = SimpleNamespace(scalar=AsyncMock(return_value=doc), commit=AsyncMock())

        async def threadpool(function, *args, **kwargs):
            if function is documents.inspect_object:
                return {
                    "blob": object(),
                    "generation": "7",
                    "size": len(data),
                    "content_type": "application/pdf",
                    "metadata": {
                        "document-uuid": str(doc.id),
                        "sha256": doc.expected_sha256,
                    },
                }
            if function is documents.inspect_content:
                return {
                    "prefix": data[:16],
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            raise AssertionError(function)

        with (
            patch.object(documents, "_encounter", AsyncMock(return_value=enc)),
            patch.object(documents, "_soap_is_signed", AsyncMock(return_value=False)),
            patch.object(documents, "run_in_threadpool", threadpool),
            patch.object(documents, "record_audit", AsyncMock()) as audit,
            patch.object(
                documents,
                "_document_row",
                AsyncMock(return_value=document_row(doc, enc)),
            ),
        ):
            response = await documents.complete_document_upload(
                enc.id, doc.id, request(), entity(50), db
            )

        self.assertEqual(doc.status, "available")
        self.assertEqual(doc.storage_generation, "7")
        self.assertEqual(doc.sha256, hashlib.sha256(data).hexdigest())
        self.assertIsNotNone(response["data"]["download_url"])
        audit.assert_awaited_once()

    async def test_hash_or_magic_mismatch_rejects_and_removes_object(self):
        enc = encounter()
        data = b"MZ executable"
        doc = document(
            expected_size_bytes=len(data),
            expected_sha256=hashlib.sha256(data).hexdigest(),
        )
        db = SimpleNamespace(scalar=AsyncMock(return_value=doc), commit=AsyncMock())

        async def threadpool(function, *args, **kwargs):
            if function is documents.inspect_object:
                return {
                    "blob": object(),
                    "generation": "8",
                    "size": len(data),
                    "content_type": "application/pdf",
                    "metadata": {
                        "document-uuid": str(doc.id),
                        "sha256": doc.expected_sha256,
                    },
                }
            if function is documents.inspect_content:
                return {
                    "prefix": data[:16],
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            if function is documents.delete_object:
                return None
            raise AssertionError(function)

        with (
            patch.object(documents, "_encounter", AsyncMock(return_value=enc)),
            patch.object(documents, "_soap_is_signed", AsyncMock(return_value=False)),
            patch.object(documents, "run_in_threadpool", threadpool),
            patch.object(documents, "record_audit", AsyncMock()),
            self.assertRaises(HTTPException) as raised,
        ):
            await documents.complete_document_upload(
                enc.id, doc.id, request(), entity(50), db
            )
        self.assertEqual(raised.exception.status_code, 415)
        self.assertEqual(doc.status, "rejected")
        self.assertIsNotNone(doc.storage_deleted_at)

    async def test_confirmation_rejects_unrelated_gcs_object_metadata(self):
        enc = encounter()
        doc = document()
        db = SimpleNamespace(scalar=AsyncMock(return_value=doc), commit=AsyncMock())

        async def threadpool(function, *args, **kwargs):
            if function is documents.inspect_object:
                return {
                    "blob": object(),
                    "generation": "10",
                    "size": doc.expected_size_bytes,
                    "content_type": doc.declared_mime_type,
                    "metadata": {
                        "document-uuid": str(UUID(int=99)),
                        "sha256": doc.expected_sha256,
                    },
                }
            if function is documents.delete_object:
                return None
            raise AssertionError(function)

        with (
            patch.object(documents, "_encounter", AsyncMock(return_value=enc)),
            patch.object(documents, "_soap_is_signed", AsyncMock(return_value=False)),
            patch.object(documents, "run_in_threadpool", threadpool),
            patch.object(documents, "record_audit", AsyncMock()),
            self.assertRaises(HTTPException) as raised,
        ):
            await documents.complete_document_upload(
                enc.id, doc.id, request(), entity(50), db
            )
        self.assertEqual(raised.exception.detail["code"], "UPLOAD_OBJECT_MISMATCH")
        self.assertEqual(doc.status, "rejected")

    async def test_repeated_confirmation_promotes_legacy_processing_document(self):
        enc = encounter()
        doc = document(status="processing", storage_generation="7")
        db = SimpleNamespace(scalar=AsyncMock(return_value=doc), commit=AsyncMock())
        with (
            patch.object(documents, "_encounter", AsyncMock(return_value=enc)),
            patch.object(documents, "_soap_is_signed", AsyncMock(return_value=False)),
            patch.object(
                documents,
                "_document_row",
                AsyncMock(return_value=document_row(doc, enc)),
            ),
            patch.object(documents, "run_in_threadpool", AsyncMock()) as threadpool,
        ):
            response = await documents.complete_document_upload(
                enc.id, doc.id, request(), entity(50), db
            )
        self.assertEqual(response["data"]["status"], "available")
        db.commit.assert_awaited_once()
        threadpool.assert_not_awaited()

    async def test_confirmation_after_soap_signing_deletes_pending_object(self):
        enc = encounter()
        doc = document()
        db = SimpleNamespace(scalar=AsyncMock(return_value=doc), commit=AsyncMock())

        async def threadpool(function, *args, **kwargs):
            return {"generation": "9"} if function is documents.inspect_object else None

        with (
            patch.object(documents, "_encounter", AsyncMock(return_value=enc)),
            patch.object(documents, "_soap_is_signed", AsyncMock(return_value=True)),
            patch.object(documents, "run_in_threadpool", threadpool),
            patch.object(documents, "record_audit", AsyncMock()),
            self.assertRaises(HTTPException) as raised,
        ):
            await documents.complete_document_upload(
                enc.id, doc.id, request(), entity(50), db
            )
        self.assertEqual(raised.exception.detail["code"], "SOAP_ALREADY_SIGNED")
        self.assertIsNotNone(doc.deleted_at)
        self.assertEqual(doc.storage_generation, "9")


class PatientDocumentDownloadDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_processing_document_has_no_url_and_download_is_denied(self):
        doc = document(status="processing", storage_generation="13")
        self.assertIsNone(
            documents._document_item(document_row(doc), request(), UUID(int=50))[
                "download_url"
            ]
        )
        expires = int(datetime.now(UTC).timestamp()) + 60
        db = SimpleNamespace(get=AsyncMock(return_value=entity(50, is_active=True)))
        with (
            patch.object(
                documents, "_document_row", AsyncMock(return_value=document_row(doc))
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            await documents.download_patient_document(
                doc.id, UUID(int=50), expires, "invalid", db
            )
        self.assertEqual(raised.exception.detail["code"], "DOCUMENT_NOT_AVAILABLE")

    async def test_available_download_rechecks_access_audits_and_redirects(self):
        doc = document(
            status="available",
            storage_generation="13",
            mime_type="application/pdf",
        )
        actor = UUID(int=50)
        expires = int(datetime.now(UTC).timestamp()) + 60
        signature = documents._capability_signature(doc.id, actor, "13", expires)
        db = SimpleNamespace(
            get=AsyncMock(return_value=entity(50, is_active=True)), commit=AsyncMock()
        )
        with (
            patch.object(
                documents, "_document_row", AsyncMock(return_value=document_row(doc))
            ),
            patch.object(
                documents,
                "run_in_threadpool",
                AsyncMock(return_value="https://storage.test/download"),
            ) as signer,
            patch.object(documents, "record_audit", AsyncMock()) as audit,
        ):
            response = await documents.download_patient_document(
                doc.id, actor, expires, signature, db
            )
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "https://storage.test/download")
        signer.assert_awaited_once()
        audit.assert_awaited_once()

    async def test_expired_tampered_and_cross_scope_downloads_are_denied(self):
        actor = UUID(int=50)
        doc = document(status="available", storage_generation="13")
        db = SimpleNamespace(get=AsyncMock(return_value=entity(50, is_active=True)))
        with self.assertRaises(HTTPException) as expired:
            await documents.download_patient_document(doc.id, actor, 1, "x", db)
        self.assertEqual(expired.exception.detail["code"], "DOWNLOAD_URL_EXPIRED")

        expires = int(datetime.now(UTC).timestamp()) + 60
        with (
            patch.object(
                documents, "_document_row", AsyncMock(return_value=document_row(doc))
            ),
            self.assertRaises(HTTPException) as tampered,
        ):
            await documents.download_patient_document(doc.id, actor, expires, "x", db)
        self.assertEqual(tampered.exception.detail["code"], "INVALID_DOWNLOAD_URL")

        with (
            patch.object(
                documents,
                "_document_row",
                AsyncMock(
                    side_effect=documents._error(
                        404, "NOT_FOUND", "Document not found."
                    )
                ),
            ),
            self.assertRaises(HTTPException) as scoped,
        ):
            await documents.download_patient_document(doc.id, actor, expires, "x", db)
        self.assertEqual(scoped.exception.status_code, 404)

    async def test_delete_soft_deletes_then_removes_exact_generation(self):
        enc = encounter()
        doc = document(status="available", storage_generation="14")
        db = SimpleNamespace(scalar=AsyncMock(return_value=doc), commit=AsyncMock())
        with (
            patch.object(documents, "_encounter", AsyncMock(return_value=enc)),
            patch.object(documents, "_require_draft_soap", AsyncMock()),
            patch.object(documents, "record_audit", AsyncMock()),
            patch.object(
                documents, "run_in_threadpool", AsyncMock(return_value=None)
            ) as threadpool,
        ):
            response = await documents.delete_document(enc.id, doc.id, entity(50), db)
        self.assertTrue(response["success"])
        self.assertIsNotNone(doc.deleted_at)
        self.assertIsNotNone(doc.storage_deleted_at)
        self.assertEqual(db.commit.await_count, 2)
        self.assertEqual(threadpool.await_args.args[0], documents.delete_object)
        self.assertEqual(threadpool.await_args.kwargs["generation"], "14")

    async def test_repeated_delete_is_idempotent_and_cleanup_failure_retryable(self):
        enc = encounter()
        deleted = document(
            deleted_at=datetime.now(UTC),
            storage_deleted_at=datetime.now(UTC),
            storage_generation="15",
        )
        db = SimpleNamespace(scalar=AsyncMock(return_value=deleted), commit=AsyncMock())
        with (
            patch.object(documents, "_encounter", AsyncMock(return_value=enc)),
            patch.object(documents, "run_in_threadpool", AsyncMock()) as threadpool,
        ):
            response = await documents.delete_document(
                enc.id, deleted.id, entity(50), db
            )
        self.assertTrue(response["success"])
        threadpool.assert_not_awaited()

        pending_cleanup = document(status="processing", storage_generation="16")
        db = SimpleNamespace(
            scalar=AsyncMock(return_value=pending_cleanup), commit=AsyncMock()
        )
        with (
            patch.object(documents, "_encounter", AsyncMock(return_value=enc)),
            patch.object(documents, "_require_draft_soap", AsyncMock()),
            patch.object(documents, "record_audit", AsyncMock()),
            patch.object(
                documents,
                "run_in_threadpool",
                AsyncMock(side_effect=RuntimeError("gcs unavailable")),
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            await documents.delete_document(enc.id, pending_cleanup.id, entity(50), db)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertIsNotNone(pending_cleanup.deleted_at)
        self.assertIsNone(pending_cleanup.storage_deleted_at)
        db.commit.assert_awaited_once()

    async def test_soap_signing_uses_the_same_encounter_row_lock(self):
        enc = encounter()
        note = entity(
            70,
            encounter_id=enc.id,
            status="draft",
            signed_by_user_id=None,
            signed_at=None,
            subjective="",
            objective="",
            assessment="",
            plan="",
            custom_fields={},
        )
        db = SimpleNamespace(scalar=AsyncMock(return_value=note), commit=AsyncMock())
        with (
            patch(
                "src.app.api.v1.clinical._encounter", AsyncMock(return_value=enc)
            ) as get_encounter,
            patch("src.app.api.v1.clinical._practitioner", AsyncMock()),
            patch("src.app.api.v1.clinical.record_audit", AsyncMock()),
        ):
            await sign_soap(enc.id, entity(50), db)
        self.assertTrue(get_encounter.await_args.kwargs["lock"])


class PatientDocumentContractTests(unittest.TestCase):
    def test_router_contract_and_list_sort_are_declared(self):
        routes = {
            (route.path, method)
            for route in documents.router.routes
            for method in route.methods
        }
        expected = {
            ("/encounters/{encounter_uuid}/documents", "POST"),
            ("/encounters/{encounter_uuid}/documents", "GET"),
            ("/patients/{patient_uuid}/documents", "GET"),
            ("/encounters/{encounter_uuid}/documents/{document_uuid}", "GET"),
            ("/encounters/{encounter_uuid}/documents/{document_uuid}/complete", "POST"),
            ("/documents/{document_uuid}/download", "GET"),
            ("/encounters/{encounter_uuid}/documents/{document_uuid}", "DELETE"),
        }
        self.assertTrue(expected.issubset(routes))
        source = inspect.getsource(documents.encounter_documents)
        self.assertIn("document_date.desc()", source)
        self.assertIn("uploaded_at.desc()", source)
        patient_source = inspect.getsource(documents.patient_documents)
        self.assertIn(
            "Encounter.facility_id == StaffAssignment.facility_id", patient_source
        )
        self.assertIn("PatientDocument.deleted_at.is_(None)", patient_source)


if __name__ == "__main__":
    unittest.main()
