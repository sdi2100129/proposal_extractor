"""arq worker that runs the extraction pipeline off the request path.

Run it alongside the API (see ``docker-compose.yml``)::

    arq proposal_service.worker.WorkerSettings

The blocking pipeline (pdfplumber, docling, Ollama HTTP) runs in a worker
thread via :func:`asyncio.to_thread`, so a single worker process can handle
several jobs concurrently without starving the event loop that drives arq's
own bookkeeping. Progress is published through the synchronous
:class:`~proposal_service.services.jobs.JobStore` from inside that thread.

docling's ML models load lazily on first use and are cached per process for
the life of the worker — unlike the per-Gunicorn-worker loading on the
synchronous request path, this amortizes the cost across all jobs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from arq.connections import RedisSettings

from proposal_service.config import get_settings
from proposal_service.logging_setup import configure_logging
from proposal_service.services.implementations import (
    DefaultAssembler,
    OllamaTaskExtractor,
    PdfplumberTableParser,
    RegexTaskExtractor
)
from proposal_service.services.jobs import JobStore
from proposal_service.services.pipeline import run_stage1, run_stage2, run_stage3
from proposal_service.services.task_extractor import OllamaUnavailable


logger = logging.getLogger(__name__)


#   Synchronous pipeline runner. Instead of rewriting the whole pipeline as async, this file wraps it in a worker thread.
def _run_pipeline_sync(
    job_id: str, pdf_path: str, company: str, start_date: str, extractor_kind: str = "regex",
) -> list[dict[str, Any]]:
    """Run all three stages synchronously, emitting progress as it goes.

    Percent budget: Stage 1 ~10%, Stage 2 spans 40–85% (per work package),
    Stage 3 ~90%, completion 100%.
    """
    store = JobStore()
    parser = PdfplumberTableParser(company=company)
    assembler = DefaultAssembler(company=company, proposal_start_date=start_date)

    store.report(
        job_id, stage="tables", message="Parsing structured tables", percent=10
    )
    stage1 = run_stage1(pdf_path, parser)

    if extractor_kind == "regex":
        # Deterministic Stage 2 — it runs in well under a second, so there's
        # nothing to stream per-WP. Bracket it with one coarse start/end
        # update so the rail advances honestly.
        store.report(
            job_id, stage="stage_2_tasks", message="Extracting tasks (regex)", percent=40
        )
        extractor = RegexTaskExtractor(company=company)
        stage2 = run_stage2(pdf_path, extractor)
        store.report(
            job_id, stage="stage_2_tasks", message="Task extraction complete", percent=85
        )
    else:
        def on_wp(done: int, total: int, wp_id: str) -> None:
            #   done: how many work packages have been processed so far
            #   total: how many work packages are expected in total
            percent = 40 + int(45 * done / max(total, 1))
            store.report(
                job_id,
                stage="tasks",
                message=f"Extracted {wp_id} ({done}/{total})",
                #   Never report more than 85% during Stage 2. Even if calculation somehow gives 86 or 90, it caps it at 85.
                percent=min(percent, 85),
            )

        extractor = OllamaTaskExtractor(company=company, on_progress=on_wp)
        store.report(
            job_id, stage="tasks", message="Extracting tasks via LLM", percent=40
        )
        stage2 = run_stage2(pdf_path, extractor)

    store.report(
        job_id, stage="assembly", message="Assembling work packages", percent=90
    )
    work_packages = run_stage3(assembler, stage1, stage2)
    #   Return a list of dicts, each representing a work package, suitable for JSON serialization.
    return [wp.model_dump() for wp in work_packages]


async def extract_work_packages(
    ctx: dict[str, Any],
    job_id: str,
    *,
    company: str,
    start_date: str,
    extractor: str = "llm",
) -> None:
    """arq task: run the pipeline for one queued job and record the outcome.

    The uploaded PDF is read from the shared upload volume and deleted once the
    job ends, regardless of outcome. Errors are mapped to client-safe messages;
    unexpected failures are logged with a traceback but never surfaced verbatim.
    """

    #   It can contain shared resources, Redis connections, startup state, etc.
    del ctx  # arq context unused
    settings = get_settings()
    store = JobStore()

    #   API and worker must share the same upload volume, so the worker can read the PDF that was uploaded via the API.
    pdf_path = str(Path(settings.job_upload_dir) / f"{job_id}.pdf")
    logger.info("Job %s started company=%s", job_id, company)

    try:
        #   Wrap the synchronous pipeline in a thread so it doesn't block the event loop. The pipeline itself reports progress to the JobStore, which is thread-safe.
        result = await asyncio.to_thread(
            _run_pipeline_sync, job_id, pdf_path, company, start_date, extractor
        )
        store.complete(job_id, result)
        logger.info("Job %s completed: %d work package(s)", job_id, len(result))
    except OllamaUnavailable as exc:
        store.fail(job_id, f"LLM backend unavailable: {exc}")
        logger.warning("Job %s failed (Ollama): %s", job_id, exc)
    except ValueError as exc:
        # e.g. "Could not locate Section 3 in the PDF." — safe to surface.
        store.fail(job_id, str(exc))
        logger.warning("Job %s rejected input: %s", job_id, exc)
    except Exception:  # noqa: BLE001 — last-resort catch for the queue
        store.fail(job_id, "Internal extraction error")
        logger.exception("Job %s crashed", job_id)
    finally:
        Path(pdf_path).unlink(missing_ok=True)




def _warm_ollama() -> None:
    """Force a one-time model load so no job pays the cold-load penalty.

    Long read timeout on purpose: a contended-disk cold load can take minutes,
    and if the client disconnects first Ollama ABORTS the load — so a short
    timeout here would sabotage the very thing we're warming.
    """
    from proposal_service.services.task_extractor import _ollama_chat, OllamaUnavailable
    try:
        _ollama_chat(
            "ping",
            options={"temperature": 0, "num_predict": 1},
            connect_timeout=10,
            read_timeout=600,
        )
        logger.info("Ollama model warmed and resident")
    except OllamaUnavailable as exc:
        logger.warning("Ollama warmup failed (jobs may hit a cold load): %s", exc)


async def _on_startup(ctx: dict[str, Any]) -> None:
    del ctx
    configure_logging(get_settings().log_level)
    await asyncio.to_thread(_warm_ollama)
    logger.info("extraction worker online")

    


class WorkerSettings:
    """arq entrypoint. Referenced as ``proposal_service.worker.WorkerSettings``."""

    #   The list of tasks that this worker can run. Each task is a coroutine function
    #   If the API enqueued a function that is not listed here, this worker would not run it.
    functions = [extract_work_packages]
    on_startup = _on_startup

    #   Connect to Redis using this Redis URL.
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = get_settings().worker_max_jobs
    job_timeout = get_settings().worker_job_timeout_seconds
    max_tries = 1  # extraction isn't idempotent; don't auto-retry on failure
