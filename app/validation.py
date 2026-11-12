"""Normalisation and validation of recall notice fields."""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
_GTIN_SEPARATORS = re.compile(r"[ \t \-]")
_BATCH = re.compile(r"[0-9]{6}")


def normalize_text(value: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value or "")).strip()


def normalize_notice_number(value: str) -> str:
    """Canonical notice number used to compare list entries with details."""
    return normalize_text(value).upper()


def normalize_gtin(value: str) -> str | None:
    """Return a bare GTIN digit string, or ``None`` if it is not a GTIN."""
    digits = _GTIN_SEPARATORS.sub("", unicodedata.normalize("NFKC", value or ""))
    if not digits.isdigit() or len(digits) not in (8, 12, 13, 14):
        return None
    return digits


def gtin_check_digit_ok(gtin: str) -> bool:
    """Validate the mod-10 check digit of an 8/12/13/14 digit GTIN."""
    payload, check = gtin[:-1], int(gtin[-1])
    total = 0
    for position, char in enumerate(reversed(payload)):
        total += int(char) * (3 if position % 2 == 0 else 1)
    return (10 - total % 10) % 10 == check


def validate_gtin(value: str) -> tuple[str | None, bool]:
    """Return ``(normalised_gtin, checksum_ok)``.

    ``normalised_gtin`` is ``None`` when the value is not GTIN-shaped.
    """
    digits = normalize_gtin(value)
    if digits is None:
        return None, False
    return digits, gtin_check_digit_ok(digits)


def normalize_batch(value: str) -> str | None:
    """Return the batch number when it is exactly six digits."""
    batch = normalize_text(value)
    if _BATCH.fullmatch(batch):
        return batch
    return None


def notice_number_plausible(value: str) -> bool:
    """Loose sanity check so arbitrary page text is not accepted as a number."""
    value = normalize_notice_number(value)
    if not (2 <= len(value) <= 60):
        return False
    if not any(char.isdigit() for char in value):
        return False
    if any(char in value for char in "<>\"'`"):
        return False
    return True
