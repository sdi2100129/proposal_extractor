# syntax=docker/dockerfile:1.6
#
# proposal-service container image.
#
# Build:  docker compose build api
# Run:    docker compose up api

FROM python:3.12-slim

# System packages commonly required by pdfplumber/docling/OCR.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        poppler-utils \
        tesseract-ocr \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Create an unprivileged user (fixed UID/GID for predictable file ownership
# on mounted volumes). --create-home gives it a writable $HOME for caches.
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

# Install Python dependencies first so the layer caches well. (Done as root:
# system site-packages stay root-owned and read-only for appuser, which is fine.)
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project (src/ layout, config.ini, pyproject.toml, ...).
COPY pyproject.toml config.ini README.md gunicorn_conf.py ./
COPY src ./src

# Install the package itself so 'proposal_service' is importable from anywhere.
RUN pip install --no-cache-dir --no-deps -e .

# Writable cache dir for docling / HuggingFace model downloads at runtime,
# owned by the unprivileged user. Without this, model loading fails on a
# read-only home when running non-root.
ENV HF_HOME=/app/.cache/huggingface \
    XDG_CACHE_HOME=/app/.cache
RUN mkdir -p /app/.cache/huggingface \
    # rapidocr writes downloaded OCR model weights here at first use;
    # site-packages is root-owned/read-only otherwise, so appuser needs
    # explicit write access or docling's OCR fallback fails at runtime.
    && mkdir -p /data/uploads \
    && chown -R appuser:appuser /app /usr/local/lib/python3.12/site-packages/rapidocr/models /data/uploads

# Drop privileges for everything from here on.
USER appuser

EXPOSE 8000

# Default command. docker-compose overrides this for the api service so the
# bind address is explicit; this CMD covers ad-hoc `docker run` use.
CMD ["gunicorn", "proposal_service.main:app", "-c", "gunicorn_conf.py"]

