"""Phone normalization for the patient portal identity.

One canonical form is stored and hashed; presentation variants (spaces, dashes,
parentheses, a leading 0 or +91) all resolve to the same canonical digits. The
canonical value is the digits-only E.164 body without a leading ``+``. Ten-digit
numbers are treated as Indian national numbers and prefixed with ``91``.
"""

from __future__ import annotations

import re

_PRESENTATION = re.compile(r"[\s\-\(\)\.\,\/]+")
_NON_DIGIT = re.compile(r"\D+")


class InvalidPhoneNumber(ValueError):
    """Raised when a phone value cannot be normalized into a usable identity."""


def normalize_phone(raw: str | None) -> str:
    if raw is None:
        raise InvalidPhoneNumber("Phone number is required.")
    cleaned = _PRESENTATION.sub("", raw.strip())
    if not cleaned:
        raise InvalidPhoneNumber("Phone number is required.")
    if any(character.isalpha() for character in cleaned):
        raise InvalidPhoneNumber("Phone number contains invalid characters.")
    digits = _NON_DIGIT.sub("", cleaned)
    digits = digits.removeprefix("00")
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    if not 8 <= len(digits) <= 15:
        raise InvalidPhoneNumber("Phone number must contain 8 to 15 digits.")
    return digits


def mask_phone(canonical: str) -> str:
    """Return a non-reversible presentation mask (never the full phone)."""
    if len(canonical) <= 4:
        return "*" * len(canonical)
    visible = canonical[-4:]
    return "*" * (len(canonical) - 4) + visible
