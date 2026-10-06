import pytest

from src.app.domains.patient_auth.phone import (
    InvalidPhoneNumber,
    mask_phone,
    normalize_phone,
)


def test_normalize_supported_variants_to_one_identity():
    assert normalize_phone("98765 43210") == "919876543210"
    assert normalize_phone("+91 98765-43210") == "919876543210"
    assert normalize_phone("09876543210") == "919876543210"
    assert normalize_phone("(98765) 43210") == "919876543210"
    assert normalize_phone("919876543210") == "919876543210"


def test_normalize_rejects_invalid_values():
    for value in (None, "", "abcdefghij", "123", "12345678901234567890"):
        with pytest.raises(InvalidPhoneNumber):
            normalize_phone(value)


def test_mask_phone_never_reveals_more_than_last_four():
    assert mask_phone("919876543210") == "********3210"
    assert mask_phone("1234") == "****"
