"""
FastAPI application entry point.

Exports ``app`` for uvicorn::

    uvicorn proposal_service.main:app --host 0.0.0.0 --port 8000

Responsibilities of this module:

* Configure logging at startup.
* Wire up routers from :mod:`proposal_service.api.routes`.
* Install request-logging middleware.
* Install global exception handlers so route handlers can stay thin.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from proposal_service import __version__
from proposal_service.config import get_settings
from proposal_service.logging_setup import configure_logging
from proposal_service.api import routes_extractions
from proposal_service.api.routes import health_router, proposal_router, proposal_v2_router
from proposal_service.api.routes_extractions import extraction_router, extraction_v2_router
from proposal_service.services.task_extractor import OllamaUnavailable



logger = logging.getLogger(__name__)


# Application lifespan
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Configure logging on startup and log shutdown."""
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.validate_required()
    logger.info(
        "proposal_service starting up: version=%s company_default=%s ollama=%s",
        __version__,
        settings.company,
        settings.ollama_url,
    )
    yield
    logger.info("proposal_service shutting down")
    if routes_extractions._arq_pool is not None:
        await routes_extractions._arq_pool.aclose()


# App construction
def create_app() -> FastAPI:
    """Build the FastAPI application.

    Kept as a factory so tests can instantiate fresh apps with overridden
    settings.

    Returns:
        A fully wired :class:`fastapi.FastAPI` instance.
    """
    app = FastAPI(
        title="Proposal Extractor",
        description=(
            "Extract structured work-package data from Horizon Europe "
            "proposals using a three-stage pipeline (table parsing, LLM "
            "task extraction, assembly)."
        ),
        version=__version__,
        lifespan=lifespan,
    )

    app.include_router(health_router)
    app.include_router(proposal_router)
    app.include_router(extraction_router)
    app.include_router(proposal_v2_router)
    app.include_router(extraction_v2_router)


    _install_middleware(app)
    _install_exception_handlers(app)

    return app


# Middleware
def _install_middleware(app: FastAPI) -> None:
    """Install request-logging middleware.

    Each incoming request is tagged with a UUID, the method/path and final
    status code are logged, and the request id is echoed back in the
    response header so clients can correlate.
    """

    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_methods=["*"],
        allow_headers=["*"],  # must permit Authorization for the bearer header
        allow_credentials=False,  # bearer token, not cookies
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = str(uuid.uuid4())
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - start) * 1000
            logger.exception(
                "request crashed method=%s path=%s elapsed_ms=%.1f request_id=%s",
                request.method,
                request.url.path,
                elapsed,
                request_id,
            )
            raise
        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s -> %d (%.1f ms) request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
            request_id,
        )
        response.headers["X-Request-ID"] = request_id
        return response


# Exception handlers
def _install_exception_handlers(app: FastAPI) -> None:
    """Centralised handlers for expected and unexpected failures.

    The skills guide requires a consistent error envelope and forbids
    leaking stack traces or implementation details to clients. We:

    * Pass through ``HTTPException`` — these are intentional.
    * Map ``ValueError`` from the pipeline (e.g. "Section 3 not found")
      to ``422 Unprocessable Entity``.
    * Map ``OllamaUnavailable`` to ``503 Service Unavailable``.
    * Convert anything else into a generic ``500`` with the traceback
      logged but not returned to the client.
    """

    @app.exception_handler(HTTPException)
    async def _http_exception(_req: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("HTTP %d: %s", exc.status_code, exc.detail)
        else:
            logger.warning("HTTP %d: %s", exc.status_code, exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _req: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.warning("Validation error: %s", exc.errors())
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": exc.errors()},
        )

    @app.exception_handler(ValueError)
    async def _value_error(_req: Request, exc: ValueError) -> JSONResponse:
        logger.warning("Pipeline rejected input: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": str(exc)},
        )

    @app.exception_handler(OllamaUnavailable)
    async def _ollama_unavailable(
        _req: Request, exc: OllamaUnavailable
    ) -> JSONResponse:
        logger.error("Ollama backend unavailable: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": f"LLM backend unavailable: {exc}"},
        )

    @app.exception_handler(Exception)
    async def _unhandled(_req: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )


# Module-level app for uvicorn
app = create_app()


