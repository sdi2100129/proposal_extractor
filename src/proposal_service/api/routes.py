"""
HTTP routes for the proposal service.

Routes are deliberately thin: they validate input via Pydantic-backed
``Depends`` injections, persist the upload to a temp file, hand the
heavy work off to the pipeline (running each stage in a threadpool so the
event loop stays responsive), and return a typed response model.

Business logic does not live here.
"""

from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
import time
from time import time
from typing import Any, Iterator

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from proposal_service.auth import verify_token
from proposal_service.config import get_settings
from proposal_service.schemas import (
    HealthResponse,
    PlannerParams,
    PlannerPayloadResponse,
    PlannerPushResponse,
    ProposalParams,
    WorkPackagesResponse,
    TablesExtractionResponse,
    TasksExtractionResponse,
    GitHubParams,
    GitHubPayloadResponse,
    GitHubPushResponse,
)
from proposal_service.services.implementations import (
    DefaultAssembler,
    OllamaTaskExtractor,
    PdfplumberTableParser,
    RegexTaskExtractor,
)
from proposal_service.services.pipeline import run_stage1, run_stage2, run_stage3
from proposal_service.services.planner_adapter import build_planner_payload
from proposal_service.services.planner_client import get_token, push_to_planner

from proposal_service.services.github_adapter import build_github_payload
from proposal_service.services.github_client import push_to_github

import requests
import time 

logger = logging.getLogger(__name__)

# Routers — the proposal router's prefix is loaded from settings so we
# can bump to /api/v2 without touching this file.
_settings = get_settings()
health_router = APIRouter(tags=["health"])
proposal_router = APIRouter(
    prefix=f"{_settings.api_prefix}/proposal",
    tags=[f"proposal {_settings.api_prefix.lstrip('/')}"],
)

proposal_v2_router = APIRouter(
    prefix="/api/v2/proposal",
    tags=["proposal api/v2 regex"],
)

# Helpers
@contextmanager
def _saved_upload(upload: UploadFile) -> Iterator[str]:
    """Persist an uploaded file to disk and yield its path.

    The temp file is deleted when the context exits, regardless of how the
    block returns.

    Args:
        upload: The incoming :class:`fastapi.UploadFile`.

    Yields:
        The absolute path of the temp file holding the upload's bytes.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp.write(upload.file.read())
        tmp.flush()
        tmp.close()
        yield tmp.name
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def _validate_pdf(pdf: UploadFile) -> None:
    """Reject uploads that are not PDFs.

    Accepts either an explicit PDF content type or a ``.pdf`` filename.
    """
    is_pdf_type = pdf.content_type in {"application/pdf", "application/octet-stream"}
    is_pdf_name = (pdf.filename or "").lower().endswith(".pdf")
    if not (is_pdf_type or is_pdf_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Expected a PDF upload, got content_type={pdf.content_type!r}",
        )


def _build_components(
    params: ProposalParams,
) -> tuple[PdfplumberTableParser, OllamaTaskExtractor, DefaultAssembler]:
    """Build the three pipeline implementations for one request."""
    company = params.company
    start_date_iso = params.start_date.isoformat()
    return (
        PdfplumberTableParser(company=company),
        OllamaTaskExtractor(company=company),
        DefaultAssembler(company=company, proposal_start_date=start_date_iso),
    )


# Routes
@health_router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
)
def health() -> HealthResponse:
    """Return service liveness.

    A trivial endpoint that returns `{"status": "ok"}` when the process
    is running. Intended for Kubernetes / Docker liveness probes and
    upstream load balancers — it deliberately does **not** check
    downstream dependencies (Ollama, Keycloak, Microsoft Graph), so a
    200 here does not imply the service can complete real work. Use the
    individual stage endpoints to exercise the full dependency chain.

    Unversioned on purpose: this endpoint has no contract beyond
    returning a 200, and probes should not need to track API versions.

    **Response**: `HealthResponse`.
    """
    return HealthResponse(status="ok")


@proposal_router.post(
    "/tables",
    response_model=TablesExtractionResponse,
    summary="Extract structured tables only (no LLM)",
)
async def proposal_stage1(
    pdf: UploadFile = File(..., description="Horizon Europe proposal PDF."),
    params: ProposalParams = Depends(ProposalParams.as_form),
    _user: dict[str, Any] = Depends(verify_token),
) -> TablesExtractionResponse:
    """Parse the WP list, effort, and deliverable tables from Horizon Europe proposal PDF.

    Useful for debugging table extraction in isolation, without paying the
    cost of LLM task extraction.

        Runs **stage 1 only**: locates Section 3 of the proposal and parses
    the WP list, the per-partner effort matrix (filtered for the
    configured company), and the deliverables table(s). The LLM is not
    invoked, so this endpoint is fast and cheap — useful for debugging
    table extraction in isolation or when only structural metadata is
    required.

    **Form fields**
    - `pdf` (required): multipart upload of the proposal PDF.
    - `company` (optional): company acronym used to filter the effort
      matrix. Defaults to `COMPANY` from settings.
    - `start_date` (optional): proposal start date in `YYYY-MM-DD`.
      Defaults to `PROPOSAL_START_DATE` from settings.

    **Response**: `TablesExtractionResponse` containing `wp_info`,
    `effort`, `raw_delivs`, and any `warnings` accumulated during
    parsing (e.g. when a table could not be located).

    **Errors**
    - `400` — upload is not a PDF or form fields are malformed.
    - `401` — missing or invalid bearer token.
    - `422` — Section 3 could not be located in the PDF.

    See also: `/api/v1/proposal/stage2` for LLM task extraction and
    `/api/v1/proposal` for the full pipeline.
    """

    _validate_pdf(pdf)
    parser, _, _ = _build_components(params)

    with _saved_upload(pdf) as pdf_path:
        stage1 = await run_in_threadpool(run_stage1, pdf_path, parser)

    return TablesExtractionResponse(
        company=params.company,
        start_date=params.start_date,
        result=stage1.to_dict(),
    )


@proposal_router.post(
    "/tasks",
    response_model=TasksExtractionResponse,
    summary="Extract tasks from WP prose via the LLM",
)
async def proposal_stage2(
    pdf: UploadFile = File(..., description="Horizon Europe proposal PDF."),
    params: ProposalParams = Depends(ProposalParams.as_form),
    _user: dict[str, Any] = Depends(verify_token),
) -> TasksExtractionResponse:
    """Extract tasks from each WP's prose section via the LLM backend.

    Runs **stage 2 only**: locates the WP description sections inside
    Section 3 and asks the configured Ollama model to return one
    structured record per task (id, title, start/end month, partners,
    description). WPs are processed in parallel up to
    `OLLAMA_MAX_WORKERS`. Tables from stage 1 are **not** consulted, so
    effort and deliverable data is absent from the response.

    This endpoint is useful for diagnosing LLM extraction quality
    without paying the cost of a full pipeline run.

    **Form fields**: identical to `/api/v1/proposal/stage1`. The
    `company` value is currently informational at this stage; task
    filtering by company happens in stage 3.

    **Response**: `TasksExtractionResponse` containing `raw_tasks` and
    `failed_wps` (WPs whose LLM extraction failed; the rest of the
    response is still returned).

    **Errors**
    - `400` — upload is not a PDF or form fields are malformed.
    - `401` — missing or invalid bearer token.
    - `422` — no WP task sections could be located in the PDF.
    - `503` — the Ollama backend is unreachable or timed out.
    """


    _validate_pdf(pdf)
    _, extractor, _ = _build_components(params)

    with _saved_upload(pdf) as pdf_path:
        stage2 = await run_in_threadpool(run_stage2, pdf_path, extractor)

    return TasksExtractionResponse(
        company=params.company,
        start_date=params.start_date,
        result=stage2.to_dict(),
    )


@proposal_router.post(
    "",
    response_model=WorkPackagesResponse,
    summary="Run the full three-stage pipeline",
)
async def proposal_full(
    pdf: UploadFile = File(..., description="Horizon Europe proposal PDF."),
    params: ProposalParams = Depends(ProposalParams.as_form),
    _user: dict[str, Any] = Depends(verify_token),
) -> WorkPackagesResponse:
    """Run all three stages and return the assembled work packages.

    Executes stage 1 (table extraction), stage 2 (LLM task extraction)
    and stage 3 (assembly) end-to-end against a single PDF upload, then
    returns the final `WorkPackage` list filtered to the configured
    company. Each returned WP includes only the tasks the company
    participates in (or leads), with deliverables attached to the
    closest matching task and roles (`leader` vs `participant`) resolved
    against the effort matrix.

    This is the endpoint to call for "give me the structured plan for my
    company"; the per-stage endpoints exist for debugging and partial
    re-runs.

    **Form fields**: identical to `/api/v1/proposal/stage1`.

    **Response**: `WorkPackagesResponse` with `company`, `start_date`
    and the `work_packages` list. WPs the company has no effort in are
    omitted.

    **Errors**
    - `400` — upload is not a PDF or form fields are malformed.
    - `401` — missing or invalid bearer token.
    - `422` — Section 3 or WP task sections could not be located.
    - `503` — the Ollama backend is unreachable or timed out.
    """
    
    _validate_pdf(pdf)
    parser, extractor, assembler = _build_components(params)

    with _saved_upload(pdf) as pdf_path:
        stage1 = await run_in_threadpool(run_stage1, pdf_path, parser)
        stage2 = await run_in_threadpool(run_stage2, pdf_path, extractor)
        work_packages = await run_in_threadpool(
            run_stage3, assembler, stage1, stage2
        )

    return WorkPackagesResponse(
        company=params.company,
        start_date=params.start_date,
        work_packages=work_packages,
    )


@proposal_router.post(
    "/planner-payload",
    response_model=PlannerPayloadResponse,
    summary="Build a Microsoft Planner payload without pushing it",
)
async def proposal_planner_payload(
    pdf: UploadFile = File(..., description="Horizon Europe proposal PDF."),
    params: ProposalParams = Depends(ProposalParams.as_form),
    _user: dict[str, Any] = Depends(verify_token),
) -> PlannerPayloadResponse:
    """Build the Microsoft Planner payload **without** pushing it to Graph.

    Runs the full pipeline and then transforms the resulting
    `WorkPackage` list into the structure expected by Microsoft Graph's
    Planner API: one bucket per WP, one task per company task, ISO due
    dates resolved from the proposal start date, and a checklist item
    per deliverable due month. Useful for inspecting what would be
    created in Planner before granting Graph credentials, or for
    feeding the payload into another planning tool.

    Requires no Microsoft Graph credentials.

    **Form fields**: identical to `/api/v1/proposal/stage1`.

    **Response**: `PlannerPayloadResponse` with the assembled
    `work_packages` and the `planner_payload` (`buckets`, `tasks`).

    **Errors**
    - `400` — upload is not a PDF or form fields are malformed.
    - `401` — missing or invalid bearer token.
    - `422` — Section 3 or WP task sections could not be located.
    - `503` — the Ollama backend is unreachable or timed out.
    """
    _validate_pdf(pdf)
    parser, extractor, assembler = _build_components(params)

    with _saved_upload(pdf) as pdf_path:
        stage1 = await run_in_threadpool(run_stage1, pdf_path, parser)
        stage2 = await run_in_threadpool(run_stage2, pdf_path, extractor)
        work_packages = await run_in_threadpool(
            run_stage3, assembler, stage1, stage2
        )

    payload = build_planner_payload(
        work_packages,
        proposal_start_date=params.start_date.isoformat(),
    )
    return PlannerPayloadResponse(
        company=params.company,
        start_date=params.start_date,
        work_packages=[wp.model_dump() for wp in work_packages],
        planner_payload=payload,
    )


@proposal_router.post(
    "/planner",
    response_model=PlannerPushResponse,
    summary="Push the proposal as a new Microsoft Planner plan",
)
async def proposal_planner(
    pdf: UploadFile = File(..., description="Horizon Europe proposal PDF."),
    params: PlannerParams = Depends(PlannerParams.as_form),
    _user: dict[str, Any] = Depends(verify_token),
) -> PlannerPushResponse:
    """Build the Planner payload and push it to Microsoft Graph.


    Builds the Planner payload exactly as
    `/api/v1/proposal/planner-payload` would, then authenticates against
    Microsoft Graph using app-only OAuth client credentials and creates:

    1. A new plan owned by `owner_group_id`, titled `plan_title`.
    2. One bucket per work package the company participates in.
    3. One task per company task, with start/due dates derived from the
       proposal start date and task months.
    4. One checklist item per deliverable due month.

    **This is a destructive operation**: each call creates a brand-new
    plan. There is no idempotency guard, so calling it twice produces
    two plans. Preview with `/api/v1/proposal/planner-payload` first.

    **Form fields**
    - `pdf`, `company`, `start_date` — as for the other endpoints.
    - `owner_group_id` (required): the Microsoft 365 group that will
      own the new plan.
    - `plan_title` (optional): display title; defaults to "Proposal Plan".

    **Response**: `PlannerPlanCreatedResponse` containing the Graph-
    assigned `plan_id`, `bucket_map` and `task_map` so the caller can
    correlate internal IDs with Planner IDs.

    **Errors**
    - `400` — upload or form fields malformed.
    - `401` — missing or invalid bearer token.
    - `422` — Section 3 or WP task sections could not be located.
    - `503` — Ollama unreachable, **or** the server is missing the
      `MS_TENANT_ID` / `MS_CLIENT_ID` / `MS_CLIENT_SECRET` environment
      variables required to talk to Microsoft Graph.
    """
    
    _validate_pdf(pdf)
    settings = get_settings()
    if not (settings.ms_tenant_id and settings.ms_client_id and settings.ms_client_secret):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Microsoft Graph credentials are not configured on this server.",
        )

    parser, extractor, assembler = _build_components(params)

    with _saved_upload(pdf) as pdf_path:
        stage1 = await run_in_threadpool(run_stage1, pdf_path, parser)
        stage2 = await run_in_threadpool(run_stage2, pdf_path, extractor)
        work_packages = await run_in_threadpool(
            run_stage3, assembler, stage1, stage2
        )

    planner_payload = build_planner_payload(
        work_packages,
        proposal_start_date=params.start_date.isoformat(),
    )

    token = await run_in_threadpool(
        get_token,
        settings.ms_client_id,
        settings.ms_client_secret,
        settings.ms_tenant_id,
    )
    planner_result = await run_in_threadpool(
        push_to_planner,
        planner_payload,
        token=token,
        owner_group_id=params.owner_group_id,
        plan_title=params.plan_title,
    )

    return PlannerPushResponse(
        company=params.company,
        start_date=params.start_date,
        planner=planner_result,
    )


@proposal_router.post(
    "/github-payload",
    response_model=GitHubPayloadResponse,
    summary="Build a GitHub Issues payload without pushing it",
)
async def proposal_github_payload(
    pdf: UploadFile = File(..., description="Horizon Europe proposal PDF."),
    params: GitHubParams = Depends(GitHubParams.as_form),
    _user: dict[str, Any] = Depends(verify_token),
) -> GitHubPayloadResponse:
    """Return the milestones/labels/issues that would be created on GitHub."""
    _validate_pdf(pdf)
    parser, extractor, assembler = _build_components(params)

    with _saved_upload(pdf) as pdf_path:
        stage1 = await run_in_threadpool(run_stage1, pdf_path, parser)
        stage2 = await run_in_threadpool(run_stage2, pdf_path, extractor)
        work_packages = await run_in_threadpool(run_stage3, assembler, stage1, stage2)

    payload = build_github_payload(
        work_packages,
        proposal_start_date=params.start_date.isoformat(),
        plan_title=params.plan_title,
    )
    return GitHubPayloadResponse(
        company=params.company,
        start_date=params.start_date,
        work_packages=[wp.model_dump() for wp in work_packages],
        github_payload=payload,
    )


@proposal_router.post(
    "/github",
    response_model=GitHubPushResponse,
    summary="Push the proposal as GitHub milestones and issues",
)
async def proposal_github(
    pdf: UploadFile = File(..., description="Horizon Europe proposal PDF."),
    params: GitHubParams = Depends(GitHubParams.as_form),
    _user: dict[str, Any] = Depends(verify_token),
) -> GitHubPushResponse:
    """Build the GitHub payload and create milestones/labels/issues in the repo.

    Requires ``GITHUB_TOKEN`` to be configured. The target repo comes from the
    ``owner``/``repo`` form fields (or their config defaults).
    """
    _validate_pdf(pdf)
    settings = get_settings()
    if not settings.github_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub token is not configured on this server.",
        )

    parser, extractor, assembler = _build_components(params)

    with _saved_upload(pdf) as pdf_path:
        stage1 = await run_in_threadpool(run_stage1, pdf_path, parser)
        stage2 = await run_in_threadpool(run_stage2, pdf_path, extractor)
        work_packages = await run_in_threadpool(run_stage3, assembler, stage1, stage2)

    payload = build_github_payload(
        work_packages,
        proposal_start_date=params.start_date.isoformat(),
        plan_title=params.plan_title,
    )

    github_result = await run_in_threadpool(
        push_to_github,
        payload,
        token=settings.github_token,
        owner=params.owner,
        repo=params.repo,
        base_url=settings.github_api_url,
    )

    return GitHubPushResponse(
        company=params.company,
        start_date=params.start_date,
        github=github_result,
    )


def _build_components_v2(
    params: ProposalParams,
) -> tuple[PdfplumberTableParser, RegexTaskExtractor, DefaultAssembler]:
    """Like _build_components, but Stage 2 is deterministic (regex, no LLM)."""
    company = params.company
    start_date_iso = params.start_date.isoformat()
    return (
        PdfplumberTableParser(company=company),
        RegexTaskExtractor(company=company),
        DefaultAssembler(company=company, proposal_start_date=start_date_iso),
    )


@proposal_v2_router.post(
    "/tasks",
    response_model=TasksExtractionResponse,
    summary="Extract tasks via deterministic regex (no LLM)",
)
async def proposal_stage2_v2(
    pdf: UploadFile = File(..., description="Horizon Europe proposal PDF."),
    params: ProposalParams = Depends(ProposalParams.as_form),
    _user: dict[str, Any] = Depends(verify_token),
) -> TasksExtractionResponse:
    started = time.monotonic()

    try:
        _validate_pdf(pdf)
        _, extractor, _ = _build_components_v2(params)

        with _saved_upload(pdf) as pdf_path:
            stage2 = await run_in_threadpool(
                run_stage2,
                pdf_path,
                extractor,
            )

        return TasksExtractionResponse(
            company=params.company,
            start_date=params.start_date,
            result=stage2.to_dict(),
        )
    finally:
        logger.info(
            "POST /api/v2/proposal/tasks completed in %.2fs",
            time.monotonic() - started,
        )



@proposal_v2_router.post(
    "",
    response_model=WorkPackagesResponse,
    summary="Run the full pipeline with deterministic task extraction (no LLM)",
)
async def proposal_full_v2(
    pdf: UploadFile = File(..., description="Horizon Europe proposal PDF."),
    params: ProposalParams = Depends(ProposalParams.as_form),
    _user: dict[str, Any] = Depends(verify_token),
) -> WorkPackagesResponse:
    total_started = time.monotonic()

    _validate_pdf(pdf)
    parser, extractor, assembler = _build_components_v2(params)

    with _saved_upload(pdf) as pdf_path:
        stage_started = time.monotonic()
        stage1 = await run_in_threadpool(
            run_stage1,
            pdf_path,
            parser,
        )
        stage1_seconds = time.monotonic() - stage_started

        stage_started = time.monotonic()
        stage2 = await run_in_threadpool(
            run_stage2,
            pdf_path,
            extractor,
        )
        stage2_seconds = time.monotonic() - stage_started

        stage_started = time.monotonic()
        work_packages = await run_in_threadpool(
            run_stage3,
            assembler,
            stage1,
            stage2,
        )
        stage3_seconds = time.monotonic() - stage_started

    total_seconds = time.monotonic() - total_started

    logger.info(
        "POST /api/v2/proposal completed: "
        "stage1=%.2fs stage2_regex=%.2fs stage3=%.2fs total=%.2fs",
        stage1_seconds,
        stage2_seconds,
        stage3_seconds,
        total_seconds,
    )

    return WorkPackagesResponse(
        company=params.company,
        start_date=params.start_date,
        work_packages=work_packages,
    )

