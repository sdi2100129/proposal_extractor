"""Tests for the HTTP layer.

Covers health, input validation, exception-handler behaviour, defaulting
of empty form fields to settings values, and middleware (request ID).
The pipeline itself is stubbed so these tests run in milliseconds.
"""

from __future__ import annotations

from typing import Any

import proposal_service.api.routes as routes_mod
from proposal_service.interfaces import ProposalTables
from proposal_service.services.task_extractor import OllamaUnavailable


from proposal_service.config import get_settings


def _api(path: str) -> str:
    """Prefix a proposal path with the configured API prefix (e.g. /api/v1)."""
    return f"{get_settings().api_prefix}{path}"


# Defaulting empty form fields to settings
def test_empty_form_fields_fall_back_to_settings(
    client, pdf_upload, monkeypatch
) -> None:
    monkeypatch.setattr(
        routes_mod,
        "run_stage1",
        lambda *a, **k: ProposalTables(wp_info={}, effort={}, raw_delivs=[]),
    )

    response = client.post(
        f"{_api('/tables')}",
        files={"pdf": pdf_upload},
        data={"company": "", "start_date": ""},
    )

    assert response.status_code == 200
    body = response.json()
    # Defaults come from config.ini ([api] section).
    assert body["company"] == "INTRA"
    assert body["start_date"] == "2025-01-01"


# Health
def test_health_returns_ok(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# Input validation
def test_rejects_non_pdf_content_type(client, proposal_form) -> None:
    response = client.post(
        f"{_api('/tables')}",
        files={"pdf": ("notes.txt", b"plain text", "text/plain")},
        data=proposal_form,
    )
    assert response.status_code == 400
    assert "Expected a PDF upload" in response.json()["detail"]


def test_rejects_missing_pdf_field(client, proposal_form) -> None:
    response = client.post(f"{_api('/tables')}", data=proposal_form)
    assert response.status_code == 422


def test_rejects_invalid_start_date(client, pdf_upload) -> None:
    response = client.post(
        f"{_api('/tables')}",
        files={"pdf": pdf_upload},
        data={"company": "INTRA", "start_date": "not-a-date"},
    )
    assert response.status_code == 400
    assert "Invalid proposal parameters" in response.json()["detail"]


# Exception handlers
def test_pipeline_value_error_returns_422(
    client, pdf_upload, proposal_form, monkeypatch
) -> None:
    def explode(*_: Any, **__: Any) -> None:
        raise ValueError("Could not locate Section 3 in the PDF.")

    monkeypatch.setattr(routes_mod, "run_stage1", explode)
    response = client.post(
        f"{_api('/tables')}", files={"pdf": pdf_upload}, data=proposal_form
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Could not locate Section 3 in the PDF."}


def test_ollama_unavailable_returns_503(
    client, pdf_upload, proposal_form, monkeypatch
) -> None:
    def unavailable(*_: Any, **__: Any) -> None:
        raise OllamaUnavailable("connection refused")

    monkeypatch.setattr(routes_mod, "run_stage2", unavailable)
    response = client.post(
        f"{_api('/tasks')}", files={"pdf": pdf_upload}, data=proposal_form
    )
    assert response.status_code == 503
    assert "LLM backend unavailable" in response.json()["detail"]


def test_unhandled_exception_returns_generic_500(
    client, pdf_upload, proposal_form, monkeypatch
) -> None:
    secret = "kaboom-secret-internal-detail"

    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(routes_mod, "run_stage2", boom)
    response = client.post(
        f"{_api('/tasks')}", files={"pdf": pdf_upload}, data=proposal_form
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert secret not in response.text, "internal detail must not leak"


# Planner endpoint guard
def test_planner_endpoint_requires_ms_credentials(
    client, pdf_upload, proposal_form
) -> None:
    response = client.post(
        f"{_api('/planner')}",
        files={"pdf": pdf_upload},
        data={**proposal_form, "owner_group_id": "abc", "plan_title": "Test"},
    )
    assert response.status_code == 503
    assert "Microsoft Graph credentials" in response.json()["detail"]


# Middleware
def test_request_id_header_present(client) -> None:
    response = client.get("/health")
    assert "x-request-id" in {h.lower() for h in response.headers}


# OpenAPI registration
def test_openapi_registers_all_routes(client) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = set(response.json()["paths"].keys())
    expected = {
        "/health",
        _api("/proposal"),
        f"{_api('/proposal')}/stage1",
        f"{_api('/proposal')}/stage2",
        f"{_api('/proposal')}/planner-payload",
        f"{_api('/proposal')}/planner",
        f"{_api('/proposal')}/github-payload",
        f"{_api('/proposal')}/github",
    }
    assert expected <= paths


# GitHub integration
def test_github_payload_returns_structure(
    client, pdf_upload, proposal_form, monkeypatch
) -> None:
    """The payload endpoint builds milestones/labels/issues without a token."""
    from proposal_service.interfaces import ProposalTables, ProposalTasks

    monkeypatch.setattr(
        routes_mod, "run_stage1",
        lambda *a, **k: ProposalTables(wp_info={}, effort={}, raw_delivs=[]),
    )
    monkeypatch.setattr(
        routes_mod, "run_stage2", lambda *a, **k: ProposalTasks(raw_tasks=[])
    )
    monkeypatch.setattr(routes_mod, "run_stage3", lambda *a, **k: [])

    response = client.post(
        _api("/proposal/github-payload"),
        files={"pdf": pdf_upload},
        data={**proposal_form, "owner": "me", "repo": "sandbox", "plan_title": "ARTEMIS"},
    )

    assert response.status_code == 200
    payload = response.json()["github_payload"]
    assert set(payload) == {"milestones", "labels", "issues"}
    assert any(lbl["name"] == "proposal:artemis" for lbl in payload["labels"])


def test_github_push_requires_token(client, pdf_upload, proposal_form) -> None:
    """With no GITHUB_TOKEN configured, the push endpoint returns 503."""
    response = client.post(
        _api("/proposal/github"),
        files={"pdf": pdf_upload},
        data={**proposal_form, "owner": "me", "repo": "sandbox"},
    )
    assert response.status_code == 503
    assert "GitHub token" in response.json()["detail"]


def test_github_push_requires_owner_and_repo(client, pdf_upload, proposal_form) -> None:
    """Missing owner/repo (and no config default) fails param validation as 400."""
    response = client.post(
        _api("/proposal/github"),
        files={"pdf": pdf_upload},
        data=proposal_form,  # no owner/repo
    )
    assert response.status_code == 400
    assert "Invalid GitHub parameters" in response.json()["detail"]


def test_github_push_succeeds_with_token(
    client, pdf_upload, proposal_form, monkeypatch
) -> None:
    """With a token set and the push stubbed, the endpoint returns the id maps."""
    from proposal_service.interfaces import ProposalTables, ProposalTasks

    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    get_settings.cache_clear()

    monkeypatch.setattr(
        routes_mod, "run_stage1",
        lambda *a, **k: ProposalTables(wp_info={}, effort={}, raw_delivs=[]),
    )
    monkeypatch.setattr(
        routes_mod, "run_stage2", lambda *a, **k: ProposalTasks(raw_tasks=[])
    )
    monkeypatch.setattr(routes_mod, "run_stage3", lambda *a, **k: [])
    monkeypatch.setattr(
        routes_mod, "push_to_github",
        lambda *a, **k: {"milestone_map": {}, "issue_map": {}, "skipped_issues": []},
    )

    response = client.post(
        _api("/proposal/github"),
        files={"pdf": pdf_upload},
        data={**proposal_form, "owner": "me", "repo": "sandbox", "plan_title": "ARTEMIS"},
    )

    assert response.status_code == 200
    assert response.json()["github"] == {
        "milestone_map": {},
        "issue_map": {},
        "skipped_issues": [],
    }

