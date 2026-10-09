"""Unit checks for PA-BE02 domain helpers (no database)."""

import pytest

from src.app.core.simple_pdf import render_text_pdf
from src.app.domains.patient_portal.appointments import (
    patient_appointment_label,
    request_fingerprint,
)
from src.app.domains.patient_portal.billing import (
    InvalidDiscount,
    compute_discount,
)


def test_patient_status_mapping_is_explicit():
    assert patient_appointment_label("booked") == "confirmed"
    assert patient_appointment_label("confirmed") == "confirmed"
    assert patient_appointment_label("checked_in") == "in_progress"
    assert patient_appointment_label("in_consultation") == "in_progress"
    assert patient_appointment_label("completed") == "completed"
    assert patient_appointment_label("cancelled") == "cancelled"
    assert patient_appointment_label("no_show") == "missed"
    assert patient_appointment_label("unknown_status") == "unknown"


def test_request_fingerprint_is_order_insensitive_and_stable():
    first = request_fingerprint({"a": 1, "b": "x"})
    second = request_fingerprint({"b": "x", "a": 1})
    assert first == second
    assert first != request_fingerprint({"a": 1, "b": "y"})


def test_percentage_discount_half_up_rounding():
    assert compute_discount(10000, kind="percentage", bp=1000) == (1000, 9000)
    # 335 * 10% = 33.5 -> half up 34
    assert compute_discount(335, kind="percentage", bp=1000) == (34, 301)
    assert compute_discount(333, kind="percentage", bp=1000) == (33, 300)


def test_fixed_discount_cannot_exceed_gross_or_go_negative():
    assert compute_discount(5000, kind="fixed", fixed_minor=2000) == (2000, 3000)
    assert compute_discount(5000, kind="fixed", fixed_minor=9000) == (5000, 0)


def test_invalid_discount_inputs_are_rejected():
    with pytest.raises(InvalidDiscount):
        compute_discount(1000, kind="percentage", bp=0)
    with pytest.raises(InvalidDiscount):
        compute_discount(1000, kind="percentage", bp=10001)
    with pytest.raises(InvalidDiscount):
        compute_discount(1000, kind="fixed", fixed_minor=0)
    with pytest.raises(InvalidDiscount):
        compute_discount(1000, kind="other", fixed_minor=10)


def test_pdf_renderer_produces_valid_pdf_bytes():
    pdf = render_text_pdf(
        title="Prescription",
        subtitle="Synthetic test document",
        sections=[("Items", ["1. Paracetamol 500mg | 1 tablet | twice daily | 3 days"])],
        footer_note="Clinical signing metadata is recorded in MedikAI; not a cryptographic signature.",
    )
    assert pdf.startswith(b"%PDF-1.4")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert b"Paracetamol" in pdf
    assert b"xref" in pdf
