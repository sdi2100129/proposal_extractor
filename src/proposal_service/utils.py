"""Shared parsing and normalization helpers.

Functions here are deliberately small, side-effect free, and have no
dependency on FastAPI or pydantic. They are imported by the service layer
and by tests.
"""

from __future__ import annotations

import re
import unicodedata


_WHITESPACE_RE = re.compile(r"\s+")


def clean(text: str | None) -> str:
    """Strip and collapse whitespace; normalize Unicode to NFKC.

    Args:
        text: Raw cell or page text. ``None`` is treated as empty.

    Returns:
        A trimmed, single-spaced string.
    """
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def norm_cell(value: object) -> str:
    """Uppercase, trim, and collapse whitespace in a cell value."""
    if value is None:
        return ""
    return _WHITESPACE_RE.sub(" ", str(value).strip()).upper()


def company_in_partners(partners: list[str], company: str) -> bool:
    """Return whether ``company`` is listed in ``partners`` (case-insensitive).

    ``"ALL PARTNERS"`` and ``"ALL"`` count as a match.
    """
    target = company.upper().strip()
    normalized = [p.upper().strip() for p in partners]
    return target in normalized or "ALL PARTNERS" in normalized or "ALL" in normalized


def first_listed(partners: list[str]) -> str:
    """Return the first listed partner, or an empty string."""
    return partners[0] if partners else ""


def month_number(month: str) -> int:
    """Convert ``M01``/``M1``/``1`` to an integer month index.

    Returns ``0`` when the input does not contain a valid month token.
    """
    if not month:
        return 0
    match = re.search(r"\d+", month)
    return int(match.group()) if match else 0


def normalize_month_token(value: str) -> str:
    """Normalize month tokens such as ``"M1"`` or ``"01"`` to ``"M01"``."""
    cleaned = clean(value).replace("Μ", "M")  # Greek capital mu → Latin M
    match = re.fullmatch(r"M?0*(\d+)", cleaned, re.I)
    if not match:
        return ""
    return f"M{int(match.group(1)):02d}"

    