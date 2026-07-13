"""Gunicorn config for the proposal service.

Used in production via:

    gunicorn proposal_service.main:app -c gunicorn_conf.py

`uvicorn` is still kept as a dev/local-reload runner (see Makefile's
`run-dev` target); gunicorn-with-uvicorn-workers is the production path.
"""

from __future__ import annotations

import multiprocessing
import os


# Bind on all interfaces so containers / reverse proxies can reach us.
bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")

# One worker per CPU core, with an env override so deployments can dial it
# down on small instances or up when LLM calls aren't the bottleneck.
workers = int(os.getenv("GUNICORN_WORKERS", str(multiprocessing.cpu_count())))

# UvicornWorker gives ASGI support while keeping gunicorn's process
# supervision, graceful reloads, and worker-recycling behaviour.
worker_class = "uvicorn.workers.UvicornWorker"

# Stage-2 LLM calls can easily exceed 60s on a cold model; align the
# worker timeout with OLLAMA_READ_TIMEOUT_SECONDS (default 600s).
timeout = int(os.getenv("GUNICORN_TIMEOUT", "600"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "5"))

# Recycle workers periodically to bound memory growth from pdfplumber /
# docling, which can hold sizable caches.
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "100"))
max_requests_jitter = int(os.getenv("GUNICORN_MAX_REQUESTS_JITTER", "20"))

# Log to stdout/stderr so the container runtime captures everything;
# the app's own logging_setup handles formatting.
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()