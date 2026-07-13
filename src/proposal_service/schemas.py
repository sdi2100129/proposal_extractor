"""
API request and response schemas.

These Pydantic models define the HTTP contract of the service. They are
distinct from :mod:`proposal_service.models`, which holds the internal
domain types.

Form parameters are bundled into :class:`ProposalParams` so they can be
injected with ``Depends()`` rather than spelt out as individual ``Form(...)``
arguments in every route handler.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import Form, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from proposal_service.config import get_settings
from proposal_service.models import WorkPackage


from enum import Enum

class JobStatus(str, Enum):
    """Lifecycle states for an async extraction job."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class JobCreatedResponse(BaseModel):
    """202 body returned when an extraction job is queued."""
    job_id: str
    status: JobStatus
    status_url: str = Field(..., description="Poll this for current status.")
    events_url: str = Field(..., description="SSE stream of progress updates.")

class JobStatusResponse(BaseModel):
    """Current state of an extraction job."""
    job_id: str
    status: JobStatus
    stage: str
    message: str
    percent: int = Field(ge=0, le=100)
    created_at: str
    updated_at: str
    result_url: str | None = None
    error: str | None = None




from proposal_service.models import WorkPackage


class ProposalResponse(BaseModel):
    company: str
    start_date: date
    work_packages: list[WorkPackage]


class ErrorResponse(BaseModel):
    """Generic error envelope returned by the global exception handlers."""

    detail: str
    

# Form-param model
class ProposalParams(BaseModel):
    """Common form parameters shared by every ``/proposal`` endpoint.

    Injected via ``Depends(ProposalParams.as_form)`` so the resulting
    ``params`` object can be passed around as a single typed value.

    Attributes:
        company: Acronym of the company to filter work packages by.
        start_date: Proposal start date in ``YYYY-MM-DD`` format.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    company: str = Field(
        ...,
        min_length=1,
        max_length=32,
        description="Company acronym, e.g. 'INTRA'. Case-insensitive.",
        examples=["INTRA"],
    )
    start_date: date = Field(
        ...,
        description="Proposal start date in YYYY-MM-DD format.",
        examples=["2025-01-01"],
    )

    @field_validator("company")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @classmethod
    def as_form(
        cls,
        company: str = Form(default=""),
        start_date: str = Form(default=""),
    ) -> "ProposalParams":
        """FastAPI dependency that builds the model from multipart form fields.

        Empty strings fall back to the service-wide defaults from
        :func:`proposal_service.config.get_settings`. Validation errors are
        surfaced as ``HTTP 400`` so the response stays consistent with the
        pipeline's other input-validation responses.
        """
        settings = get_settings()
        try:
            return cls(
                company=company or settings.company,
                start_date=start_date or settings.proposal_start_date,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid proposal parameters: {exc}",
            ) from exc


# Planner-specific extension
class PlannerParams(ProposalParams):
    """Form parameters for the ``/proposal/planner`` endpoint.

    Adds the Microsoft 365 group ID that owns the new plan and the plan
    title to display in Planner.
    """

    owner_group_id: str = Field(
        ...,
        min_length=1,
        description="Microsoft 365 group ID that will own the Planner plan.",
    )
    plan_title: str = Field(
        "Proposal Plan",
        max_length=255,
        description="Title shown in Microsoft Planner for the created plan.",
    )

    @classmethod
    def as_form(  # type: ignore[override]
        cls,
        company: str = Form(default=""),
        start_date: str = Form(default=""),
        owner_group_id: str = Form(...),
        plan_title: str = Form("Proposal Plan"),
    ) -> "PlannerParams":
        settings = get_settings()
        try:
            return cls(
                company=company or settings.company,
                start_date=start_date or settings.proposal_start_date,
                owner_group_id=owner_group_id,
                plan_title=plan_title,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid planner parameters: {exc}",
            ) from exc



class GitHubParams(ProposalParams):
    """Form parameters for the ``/proposal/github`` endpoints.

    Adds the target repository (owner/repo) and the proposal title used to
    prefix milestone and issue names in the shared repo.
    """

    owner: str = Field(..., min_length=1, description="Repository owner (user or org login).")
    repo: str = Field(..., min_length=1, description="Repository name.")
    plan_title: str = Field(
        "Proposal Plan",
        max_length=255,
        description="Proposal title; prefixes milestone/issue names.",
    )

    @classmethod
    def as_form(  # type: ignore[override]
        cls,
        company: str = Form(default=""),
        start_date: str = Form(default=""),
        owner: str = Form(default=""),
        repo: str = Form(default=""),
        plan_title: str = Form("Proposal Plan"),
    ) -> "GitHubParams":
        settings = get_settings()
        try:
            return cls(
                company=company or settings.company,
                start_date=start_date or settings.proposal_start_date,
                owner=owner or settings.github_owner or "",
                repo=repo or settings.github_repo or "",
                plan_title=plan_title,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid GitHub parameters: {exc}",
            ) from exc
        

# Response envelopes
class HealthResponse(BaseModel):
    """Response body for ``GET /health``."""

    status: str = "ok"



# ---------------------------------------------------------------------------
# Inner result schemas (typed equivalents of the dataclasses in interfaces.py)
# ---------------------------------------------------------------------------
class WorkPackageSummary(BaseModel):
    """One row of the WP-list summary table extracted from Section 3."""

    title: str = Field(default="", description="Work-package title.")
    leader: str = Field(default="", description="Leader partner acronym.")
    pms: float = Field(default=0.0, description="Total person-months for the WP.")
    start: str = Field(default="", description="Start month, 'Mnn' format.")
    end: str = Field(default="", description="End month, 'Mnn' format.")


class RawDeliverable(BaseModel):
    """A deliverable row as parsed before being attached to its task."""

    ids: list[str] = Field(default_factory=list, description="Deliverable IDs.")
    name: str = Field(default="", description="Short deliverable title.")
    description: str = Field(default="", description="Longer description text.")
    wp: str = Field(default="", description="Parent WP identifier.")
    lead: str = Field(default="", description="Lead partner acronym.")
    type: str = Field(default="", description="Type code (R, DEM, OTHER...).")
    dissemination: str = Field(default="", description="Dissemination level.")
    months: list[str] = Field(default_factory=list, description="Due months.")


class TablesExtractionResult(BaseModel):
    """Stage 1 payload: parsed tables from Section 3."""

    wp_info: dict[str, WorkPackageSummary] = Field(
        default_factory=dict,
        description="WP id -> summary row.",
    )
    effort: dict[str, float] = Field(
        default_factory=dict,
        description="WP id -> person-months for the configured company.",
    )
    raw_delivs: list[RawDeliverable] = Field(
        default_factory=list,
        description="Deliverable rows, unattached to tasks at this stage.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Soft errors collected during parsing.",
    )


class RawTask(BaseModel):
    """A task as returned by the LLM, before merging with deliverables."""

    id: str = Field(..., description="Task identifier, e.g. 'T1.1'.")
    wp_id: str = Field(..., description="Parent WP identifier.")
    title: str = ""
    start_month: str = ""
    end_month: str = ""
    partners: list[str] = Field(default_factory=list)
    description: str = ""


class FailedWorkPackage(BaseModel):
    """A WP whose LLM extraction failed; included so the client can retry."""

    wp_id: str
    error: str


class TasksExtractionResult(BaseModel):
    """Stage 2 payload: tasks extracted from WP prose sections."""

    raw_tasks: list[RawTask] = Field(default_factory=list)
    failed_wps: list[FailedWorkPackage] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------
class HealthResponse(BaseModel):
    """Response body for ``GET /health``."""

    status: str = "ok"


class TablesExtractionResponse(BaseModel):
    """Response body for stage 1 — table extraction only."""

    company: str
    start_date: date
    result: TablesExtractionResult


class TasksExtractionResponse(BaseModel):
    """Response body for stage 2 — LLM task extraction only."""

    company: str
    start_date: date
    result: TasksExtractionResult


class WorkPackagesResponse(BaseModel):
    """Response body for the full pipeline — assembled WorkPackage objects."""

    company: str
    start_date: date
    work_packages: list[WorkPackage]


class PlannerPayloadResponse(BaseModel):
    """Response body for ``POST /proposal/planner-payload``."""

    company: str
    start_date: date
    work_packages: list[WorkPackage]
    planner_payload: dict[str, Any]


class GitHubPayloadResponse(BaseModel):
    """Response body for ``POST /proposal/github-payload``."""

    company: str
    start_date: date
    work_packages: list[WorkPackage]
    github_payload: dict[str, Any]

    
class PlannerPushResponse(BaseModel):
    """Response body for ``POST /proposal/planner``."""

    company: str
    start_date: date
    planner: dict[str, Any]


class GitHubPushResponse(BaseModel):
    """Response body for ``POST /proposal/github``."""

    company: str
    start_date: date
    github: dict[str, Any]


class ErrorResponse(BaseModel):
    """Generic error envelope returned by the global exception handlers."""

    detail: str
