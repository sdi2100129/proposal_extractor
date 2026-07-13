"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# Sample PDF fixture — checked in under tests/fixtures/.
# Used by test_api.py for happy-path file uploads. Tests that need a
# specific PDF body should read it themselves; this one is for cases
# where the test only needs *some* valid PDF to attach.
SAMPLE_PDF_PATH = Path(__file__).parent / "fixtures" / "sample_proposal.pdf"


#   Run this fixture automatically for every test
@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the settings cache between tests and set required env vars.

    Keycloak URLs are required by ``Settings.validate_required()``; we
    inject test values so the startup check passes.
    """

    #   Pytest's helper for setting env vars that are automatically undone at test end
    monkeypatch.setenv("KEYCLOAK_URL", "http://keycloak.test")
    monkeypatch.setenv("KEYCLOAK_PUBLIC_URL", "http://keycloak.public.test")

    from proposal_service.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

#   Yielding fixture behaves like an iterator: it gives control to the test, then continues after the test finishes.
@pytest.fixture()
def client() -> Iterator[TestClient]:
    """A FastAPI TestClient with auth bypassed."""
    from proposal_service.auth import verify_token
    from proposal_service.main import app

    #   Return fake user info for any token, so tests don't have to worry about auth unless they want to.
    app.dependency_overrides[verify_token] = lambda: {"sub": "test-user"}

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    #   Removethe fake authentication after the test
    app.dependency_overrides.clear()


@pytest.fixture()
def proposal_form() -> dict[str, str]:
    """Default form payload matching the config.ini ``[api]`` defaults.

    Tests that need different values can copy and override::

        data = {**proposal_form, "company": "OTHER"}
    """
    return {"company": "INTRA", "start_date": "2025-01-01"}


@pytest.fixture()
def pdf_upload() -> tuple[str, bytes, str]:
    """A real PDF read from ``tests/fixtures/sample_proposal.pdf``.

    Returned as the 3-tuple expected by ``TestClient.post(files={...})``::

        files = {"pdf": pdf_upload}

    Raises pytest's skip if the sample file is missing, so a missing
    fixture file shows up as a clear "skipped" message instead of a
    confusing FileNotFoundError mid-test.
    """
    if not SAMPLE_PDF_PATH.is_file():
        pytest.skip(
            f"Sample PDF not found at {SAMPLE_PDF_PATH}. "
            "Place a small Horizon Europe proposal there to enable upload tests."
        )
    return ("sample_proposal.pdf", SAMPLE_PDF_PATH.read_bytes(), "application/pdf")
