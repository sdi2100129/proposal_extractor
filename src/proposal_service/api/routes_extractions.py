"""Async extraction API (job-queue based), mounted at ``/api/v1/extractions``.

These endpoints complement the synchronous ``/proposal`` routes for large
PDFs, where holding an HTTP connection open for the full pipeline is
impractical. The flow is:

1. ``POST /work-packages`` persists the upload to the shared volume, records a
   queued job, enqueues an arq task, and returns ``202 Accepted``.
2. ``GET /{job_id}`` reports current status (and a ``result_url`` once done).
3. ``GET /{job_id}/events`` streams Server-Sent Events for live progress.
4. ``GET /{job_id}/result`` returns the assembled work packages.

Routes stay thin: validation, persistence, enqueue, and response shaping only.
All heavy work lives in the worker.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from proposal_service.auth import verify_token
from proposal_service.config import get_settings
from proposal_service.schemas import (
    JobCreatedResponse,
    JobStatus,
    JobStatusResponse,
    ProposalParams,
    ProposalResponse,
)
from proposal_service.services.jobs import JobStore, stream_events


logger = logging.getLogger(__name__)

extraction_router = APIRouter(prefix="/api/v1/extractions", tags=["extractions"])


# arq connection pool (lazy, process-wide)
_arq_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    """Return a shared arq Redis pool used to enqueue jobs."""
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(
            RedisSettings.from_dsn(get_settings().redis_url)
        )
    return _arq_pool


# Helpers
def _validate_pdf(pdf: UploadFile) -> None:
    """Reject uploads that are not PDFs (by content type or ``.pdf`` name)."""
    is_pdf_type = pdf.content_type in {"application/pdf", "application/octet-stream"}
    is_pdf_name = (pdf.filename or "").lower().endswith(".pdf")
    if not (is_pdf_type or is_pdf_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Expected a PDF upload, got content_type={pdf.content_type!r}",
        )


def _status_response(job_id: str, data: dict[str, str]) -> JobStatusResponse:
    job_status = data.get("status", JobStatus.QUEUED.value)
    return JobStatusResponse(
        job_id=job_id,
        status=JobStatus(job_status),
        stage=data.get("stage", ""),
        message=data.get("message", ""),
        percent=int(data.get("percent") or 0),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        result_url=(
            f"/api/v1/extractions/{job_id}/result"
            if job_status == JobStatus.COMPLETED.value
            else None
        ),
        error=data.get("error") or None,
    )


# Routes
@extraction_router.post(
    "/work-packages",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobCreatedResponse,
    summary="Queue a work-package extraction job",
)
async def create_extraction(
    pdf: UploadFile = File(..., description="Horizon Europe proposal PDF."),
    params: ProposalParams = Depends(ProposalParams.as_form),
    _user: dict[str, Any] = Depends(verify_token),
    pool: ArqRedis = Depends(get_arq_pool),
) -> JobCreatedResponse:
    """Accept a proposal PDF, queue extraction, and return ``202`` with URLs."""
    _validate_pdf(pdf)
    settings = get_settings()

    #   Create a unique job ID and save the PDF to the shared volume with that name. The worker will read it from there.
    job_id = uuid.uuid4().hex
    upload_dir = Path(settings.job_upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / f"{job_id}.pdf").write_bytes(await pdf.read())

    store = JobStore()
    start_date_iso = params.start_date.isoformat()
    await run_in_threadpool(
        store.create, job_id, company=params.company, start_date=start_date_iso
    )
    await pool.enqueue_job(
        "extract_work_packages",
        job_id,
        company=params.company,
        start_date=start_date_iso,
    )
    logger.info("Queued extraction job %s company=%s", job_id, params.company)

    base = f"/api/v1/extractions/{job_id}"
    return JobCreatedResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        status_url=base,
        events_url=f"{base}/events",
    )


@extraction_router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    summary="Get current job status",
)
async def get_job_status(
    job_id: str,
    _user: dict[str, Any] = Depends(verify_token),
) -> JobStatusResponse:
    """Return the job's current status, progress, and (when done) result URL."""
    data = await run_in_threadpool(JobStore().get, job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job_id")
    return _status_response(job_id, data)


@extraction_router.get(
    "/{job_id}/events",
    summary="Stream job progress as Server-Sent Events",
)
async def get_job_events(
    job_id: str,
    _user: dict[str, Any] = Depends(verify_token),
) -> StreamingResponse:
    """Stream ``progress`` events until a terminal ``completed``/``failed``.

    ``X-Accel-Buffering: no`` disables nginx response buffering for this
    endpoint so events arrive promptly without a sidewide config change.
    """
    return StreamingResponse(
        stream_events(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@extraction_router.get(
    "/{job_id}/result",
    response_model=ProposalResponse,
    summary="Get the assembled work packages for a finished job",
)
async def get_job_result(
    job_id: str,
    _user: dict[str, Any] = Depends(verify_token),
) -> ProposalResponse:
    """Return the result for a COMPLETED job, or an explanatory error code."""
    store = JobStore()
    data = await run_in_threadpool(store.get, job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job_id")

    job_status = data.get("status")
    if job_status == JobStatus.FAILED.value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=data.get("error") or "Extraction failed",
        )
    if job_status != JobStatus.COMPLETED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Result not ready (status={job_status})",
        )

    result = await run_in_threadpool(store.get_result, job_id)
    if result is None:
        raise HTTPException(status_code=410, detail="Result expired or unavailable")

    return ProposalResponse(
        company=data["company"],
        start_date=data["start_date"],
        work_packages=result,
    )



extraction_v2_router = APIRouter(
    prefix="/api/v2/extractions", tags=["extractions api/v2 regex"]
)


@extraction_v2_router.post(
    "/work-packages",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobCreatedResponse,
    summary="Queue a work-package extraction job (deterministic regex Stage 2)",
)
async def create_extraction_v2(
    pdf: UploadFile = File(..., description="Horizon Europe proposal PDF."),
    params: ProposalParams = Depends(ProposalParams.as_form),
    _user: dict[str, Any] = Depends(verify_token),
    pool: ArqRedis = Depends(get_arq_pool),
) -> JobCreatedResponse:
    """Queue extraction with the regex extractor and return 202.

    Status, events (SSE), and result are served by the shared
    ``/api/v1/extractions/{job_id}`` endpoints — a job is identified only by
    its id and those readers are extractor-agnostic. Only the enqueue differs:
    ``extractor="regex"`` selects the deterministic Stage 2.
    """
    _validate_pdf(pdf)
    settings = get_settings()

    job_id = uuid.uuid4().hex
    upload_dir = Path(settings.job_upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / f"{job_id}.pdf").write_bytes(await pdf.read())

    store = JobStore()
    start_date_iso = params.start_date.isoformat()
    await run_in_threadpool(
        store.create, job_id, company=params.company, start_date=start_date_iso
    )
    await pool.enqueue_job(
        "extract_work_packages",
        job_id,
        company=params.company,
        start_date=start_date_iso,
        extractor="regex",
    )
    logger.info("Queued regex extraction job %s company=%s", job_id, params.company)

    base = f"/api/v1/extractions/{job_id}"
    return JobCreatedResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        status_url=base,
        events_url=f"{base}/events",
    )

