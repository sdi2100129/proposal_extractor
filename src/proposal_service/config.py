"""Typed application settings.

All environment reads and configuration parsing live in this module so the
rest of the codebase never calls ``os.getenv`` directly.

Loading precedence (highest first):

1. Environment variables.
2. ``.env`` file in the working directory (developer machines only).
3. ``config.ini`` defaults shipped with the service.
4. Hard-coded fallbacks in this module.

The module exposes a single ``get_settings`` accessor that returns a cached
``Settings`` instance. Required settings are validated at startup via
``Settings.validate_required``; failing fast prevents broken deployments from
serving traffic.
"""

from __future__ import annotations

import configparser
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def load_ini_defaults() -> dict[str, str]:
    """Read ``config.ini`` from the project root if present.

    Returns a flat mapping where keys are the uppercased option name
    (no section prefix). Sections are informational only — option names
    must therefore be unique across sections.
    """
    ini_path = Path("config.ini")
    if not ini_path.exists():
        return {}

    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")

    defaults: dict[str, str] = {}
    for section in parser.sections():
        for key, value in parser.items(section):
            # Use the bare key, uppercased. This matches the env-var
            # names that pydantic-settings looks for, e.g.
            #     [api] company  →  COMPANY
            #     [ollama] ollama_url  →  OLLAMA_URL
            defaults[key.upper()] = value
    return defaults


class Settings(BaseSettings):
    """Service settings loaded from environment, ``.env``, and ``config.ini``.

    Attributes are typed and parsed at instantiation time. Use
    :func:`get_settings` to obtain a cached instance.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # API defaults
    company: str = Field(
        default="NCI",
        description="Default company acronym applied when callers omit it.",
    )
    proposal_start_date: str = Field(
        default="2025-01-01",
        description="Default proposal start date in ISO YYYY-MM-DD format.",
    )

    cors_allow_origins: list[str] = Field(
        default=["http://localhost:8888"],
        description="Origins allowed to call this API from a browser (the console UI's origin).",
    )

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, v: object) -> object:
        # Allow a plain comma-separated env value (CORS_ALLOW_ORIGINS=
        # http://localhost:8888,http://localhost:3000) alongside the
        # JSON-list form pydantic-settings expects by default for list
        # fields — plain-string env vars are the more common ergonomic.
        if isinstance(v, str) and not v.strip().startswith("["):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    # Logging
    log_level: LogLevel = Field(
        default="INFO",
        description="Root log level for application loggers.",
    )

    # Ollama LLM backend
    # Typed as str to keep the default literal safe across Pydantic versions.
    # The value is validated by _validate_ollama_url below.
    ollama_url: str = Field(
        default="http://host.docker.internal:11434",
        description="Base URL of the Ollama HTTP API.",
    )
    ollama_model: str = Field(
        default="qwen2.5:3b",
        description="Model name to use for task and table classification.",
    )
    ollama_connect_timeout_seconds: float = Field(default=30.0, ge=1.0)
    ollama_read_timeout_seconds: float = Field(default=600.0, ge=1.0)
    ollama_num_ctx: int = Field(default=8192, ge=512)
    ollama_max_workers: int = Field(default=4, ge=1, le=16)

    # Keycloak (OIDC)
    keycloak_url: str | None = Field(
        default=None,
        description="Internal Keycloak base URL used by the backend.",
    )
    keycloak_public_url: str | None = Field(
        default=None,
        description="Public Keycloak base URL used by browser clients.",
    )
    keycloak_realm: str = Field(default="proposal")
    keycloak_client_id: str = Field(default="proposal-api")
    keycloak_allowed_clients: list[str] = Field(
        default_factory=lambda: ["swagger-ui", "proposal-api"],
        description="Allow-list of azp claim values accepted in JWTs.",
    )
    keycloak_jwks_cache_seconds: int = Field(default=3600, ge=0)

    # Microsoft Graph (Planner)
    ms_tenant_id: str | None = None
    ms_client_id: str | None = None
    ms_client_secret: str | None = None

    api_prefix: str = Field(
        default="/api/v1",
        description="URL prefix for versioned API routes. Bump to '/api/v2' for breaking changes.",
    )


    # GitHub (Issues integration)
    github_token: str | None = Field(
        default=None,
        description="Personal Access Token with Issues read/write on the repo.",
    )
    github_api_url: str = Field(
        default="https://api.github.com",
        description="GitHub REST API root. Override for GitHub Enterprise Server.",
    )
    github_owner: str | None = Field(
        default=None,
        description="Default repository owner (user or org) when callers omit it.",
    )
    github_repo: str | None = Field(
        default=None,
        description="Default repository name when callers omit it.",
    )


    # Async jobs (Redis + arq)
    redis_url: str = Field(
        default="redis://redis:6379/0",
        description="Redis DSN for the job queue and event bus.",
    )
    job_upload_dir: str = Field(
        default="/data/uploads",
        description="Shared volume where queued PDFs are staged for the worker.",
    )
    job_ttl_seconds: int = Field(default=86400, ge=60)
    worker_max_jobs: int = Field(default=2, ge=1, le=16)
    worker_job_timeout_seconds: int = Field(default=1800, ge=60)

    # UI
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"])


    @field_validator("api_prefix")
    @classmethod
    def _normalize_api_prefix(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith("/"):
            raise ValueError("api_prefix must start with '/'")
        return value

    @field_validator("proposal_start_date")
    @classmethod
    def _validate_iso_date(cls, value: str) -> str:
        from datetime import date

        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                "proposal_start_date must be ISO YYYY-MM-DD"
            ) from exc
        return value

    @field_validator("company")
    @classmethod
    def _normalize_company(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("company must be a non-empty acronym")
        return normalized

    @field_validator("ollama_url")
    @classmethod
    def _validate_ollama_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("ollama_url must start with http:// or https://")
        return value.rstrip("/")

    def validate_required(self) -> None:
        """Raise if any startup-required setting is missing or unusable.

        Called once during application startup so misconfigured deployments
        fail loudly before they begin serving requests.

        Raises:
            RuntimeError: If a required setting is missing.
        """
        missing: list[str] = []
        if not self.keycloak_url:
            missing.append("KEYCLOAK_URL")
        if not self.keycloak_public_url:
            missing.append("KEYCLOAK_PUBLIC_URL")
        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached :class:`Settings` instance.

    INI defaults are applied to ``os.environ`` only when no real env var
    already exists for that key (``setdefault``). pydantic-settings then
    reads the populated environment as usual, so a real env var or .env
    value always wins over the INI default.

    Tests may call ``get_settings.cache_clear()`` between cases to pick up
    environment changes.
    """
    for key, value in load_ini_defaults().items():
        os.environ.setdefault(key, value)
    return Settings()



