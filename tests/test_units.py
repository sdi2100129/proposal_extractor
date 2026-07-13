"""Unit tests for proposal_service.utils helpers."""

from __future__ import annotations

import pytest

from proposal_service.utils import (
    clean,
    company_in_partners,
    first_listed,
    month_number,
    norm_cell,
    normalize_month_token,
)


class TestClean:
    def test_collapses_internal_whitespace(self) -> None:
        assert clean("a   b\n\tc") == "a b c"

    def test_handles_none(self) -> None:
        assert clean(None) == ""

    def test_strips_leading_trailing(self) -> None:
        assert clean("  hello  ") == "hello"


class TestNormCell:
    def test_uppercases(self) -> None:
        assert norm_cell("intra") == "INTRA"

    def test_handles_none(self) -> None:
        assert norm_cell(None) == ""


class TestCompanyInPartners:
    def test_direct_match_case_insensitive(self) -> None:
        assert company_in_partners(["UCD", "intra"], "INTRA") is True

    def test_all_partners_matches_any(self) -> None:
        assert company_in_partners(["All Partners"], "XYZ") is True

    def test_no_match(self) -> None:
        assert company_in_partners(["UCD", "NTU"], "INTRA") is False


class TestFirstListed:
    def test_returns_first(self) -> None:
        assert first_listed(["A", "B"]) == "A"

    def test_empty_list(self) -> None:
        assert first_listed([]) == ""


class TestMonthNumber:
    @pytest.mark.parametrize(
        "raw,expected",
        [("M01", 1), ("M1", 1), ("M12", 12), ("1", 1), ("", 0), ("none", 0)],
    )
    def test_parses(self, raw: str, expected: int) -> None:
        assert month_number(raw) == expected


class TestNormalizeMonthToken:
    #   Run the function below multiple times, each time with different values for raw and expected
    @pytest.mark.parametrize(
        "raw,expected",
        [("M1", "M01"), ("M01", "M01"), ("1", "M01"), ("12", "M12"), ("Μ5", "M05")],
    )
    def test_canonical_form(self, raw: str, expected: str) -> None:
        assert normalize_month_token(raw) == expected

    def test_invalid_returns_empty(self) -> None:
        assert normalize_month_token("abc") == ""

