"""Async-job state and progress event bus, backed by Redis.

This module is the single boundary between the extraction API/worker and
Redis. It owns three things:

* **Job state** — a small hash per job (status, percent, stage, error).
* **Results** — the assembled work packages as JSON, under a TTL.
* **Progress events** — published on a per-job pub/sub channel and consumed
  by the SSE endpoint.

Two access styles are provided on purpose:

* :class:`JobStore` uses a *synchronous* Redis client. The web layer calls it
  through ``run_in_threadpool`` (matching the existing route style) and the
  worker calls it directly from the thread running the blocking pipeline.
* :func:`stream_events` uses ``redis.asyncio`` because the SSE endpoint awaits
  messages on a long-lived connection and must not block the event loop.

All keys carry a TTL, so abandoned jobs expire without a separate reaper.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import redis
#   redis.asyncio is imported in stream_events() 
#   because SSE keeps a connection open and waits for new messages without blocking the whole FastAPI event loop.
import redis.asyncio as aioredis

from proposal_service.config import get_settings
from proposal_service.schemas import JobStatus


logger = logging.getLogger(__name__)

_TERMINAL = {JobStatus.COMPLETED.value, JobStatus.FAILED.value}


# Key helpers
def _job_key(job_id: str) -> str:
    return f"extraction:job:{job_id}"


def _result_key(job_id: str) -> str:
    return f"extraction:result:{job_id}"


#   It is a Redis channel where the worker can publish messages and the SSE endpoint can listen.
def _events_channel(job_id: str) -> str:
    return f"extraction:events:{job_id}"

#   Current time in UTC.
def _now_iso() -> str:
    """UTC ISO 8601 with a trailing ``Z`` (matches the logging convention)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

#   SSE frame formatter. SSE messages must follow a specific text format:
#   event: progress
#   data: {"stage": "parsing", "message": "Reading PDF", "percent": 30}
#
#
#   \n\n tells the browser that the event is complete.
def _sse(event: str, data: dict[str, Any]) -> str:
    """Format a single Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


#   When the job finishes, the SSE stream does not send the whole result but a URL.
#   SSE should usually send small progress messages, not huge JSON results.
def _result_url(job_id: str) -> str:
    return f"/api/v1/extractions/{job_id}/result"


# Synchronous client (web via threadpool + worker thread). Stores one shared Redis client for the process.
_sync_client: redis.Redis | None = None


def _get_sync_client() -> redis.Redis:
    """Return a process-wide synchronous Redis client (lazy, pooled)."""
    #   Lazy initialization: The Redis client is created only when first needed, which avoids unnecessary connections if the service is running but not processing jobs.
    #   Connection pool: The Redis client can reuse network connections to Redis instead of opening a brand-new TCP connection for every command. So even if we have one client,, internally Redis may manage multiple Redis connections efficiently.

    global _sync_client
    if _sync_client is None:
        _sync_client = redis.Redis.from_url(
            #   Redis returns strings instead of bytes.
            get_settings().redis_url, decode_responses=True
        )
    return _sync_client


class JobStore:
    """Synchronous Redis-backed job state writer and event publisher.

    Safe to instantiate per request or per job; it shares a module-level
    connection pool. All write methods refresh the key TTL so an active job
    never expires mid-flight.
    """

    def __init__(self, client: redis.Redis | None = None) -> None:
        #   connection to Redis server. If not provided, a shared client is created.
        self._client = client or _get_sync_client()
        #   job expiration time.
        self._ttl = get_settings().job_ttl_seconds

    def create(self, job_id: str, *, company: str, start_date: str) -> None:
        """Record a freshly queued job."""
        now = _now_iso()
        key = _job_key(job_id)
        self._client.hset(
            key,
            mapping={
                "job_id": job_id,
                "status": JobStatus.QUEUED.value,
                "percent": 0,
                "stage": "queued",
                "message": "Job accepted and queued.",
                "company": company,
                "start_date": start_date,
                "created_at": now,
                "updated_at": now,
                "error": "",
            },
        )
        self._client.expire(key, self._ttl)

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Return the raw job hash, or ``None`` if the job is unknown/expired."""

        #   Returns the job state from Redis.
        data = self._client.hgetall(_job_key(job_id))
        #   If the job does not exist, hgetall returns an empty dict, which is falsy, so we return None in that case.
        return data or None

    def report(self, job_id: str, *, stage: str, message: str, percent: int) -> None:
        """Move the job to RUNNING and publish a ``progress`` event."""

        self._update(
            job_id,
            status=JobStatus.RUNNING.value,
            stage=stage,
            message=message,
            percent=int(percent),
        )

        #   Publish a progress event to the Redis pub/sub channel for this job to extraction:events:job_id.
        self._publish(
            job_id, "progress", {"stage": stage, "message": message, "percent": int(percent)}
        )

    def complete(self, job_id: str, result: list[dict[str, Any]]) -> None:
        """Store the result, mark COMPLETED, and publish a ``completed`` event."""

        self._client.set(
            _result_key(job_id),
            #   Convert the result from Python list/dict into JSON string
            json.dumps(result, ensure_ascii=False),
            ex=self._ttl,
        )
        self._update(
            job_id,
            status=JobStatus.COMPLETED.value,
            stage="completed",
            message="Extraction complete.",
            percent=100,
        )
        self._publish(
            job_id, "completed", {"job_id": job_id, "result_url": _result_url(job_id)}
        )

    def fail(self, job_id: str, error: str) -> None:
        """Mark FAILED with a client-safe error and publish a ``failed`` event."""
        self._update(
            job_id,
            status=JobStatus.FAILED.value,
            stage="failed",
            message="Extraction failed.",
            error=error,
        )
        self._publish(job_id, "failed", {"job_id": job_id, "error": error})

    def get_result(self, job_id: str) -> list[dict[str, Any]] | None:
        """Return the stored result list, or ``None`` if absent/expired."""

        #    Reads extraction:result:{job_id}
        raw = self._client.get(_result_key(job_id))
        #   Converts JSON string back to Python if exists, otherwise returns None.
        return json.loads(raw) if raw else None

    def _update(self, job_id: str, **fields: Any) -> None:
        fields["updated_at"] = _now_iso()
        key = _job_key(job_id)
        self._client.hset(key, mapping=fields)
        self._client.expire(key, self._ttl)

    def _publish(self, job_id: str, event: str, data: dict[str, Any]) -> None:
        self._client.publish(
            #   Publish a message to the Redis pub/sub channel for this job to extraction:events:job_id.
            _events_channel(job_id), json.dumps({"event": event, "data": data})
        )


# Asynchronous SSE subscription (web only)
async def stream_events(job_id: str) -> AsyncIterator[str]:
    """Yield SSE frames for a job's progress until it reaches a terminal state.

    The subscription is opened *before* the state snapshot is read, so an event
    published in that window is queued rather than lost. A late subscriber to an
    already-finished job still gets a current snapshot plus the terminal frame.

    Args:
        job_id: The job to follow.

    Yields:
        Pre-formatted ``text/event-stream`` frames, including periodic
        heartbeat comments so idle connections aren't reaped by proxies.
    """

    #   Create an asynchronous Redis client to connect to the Redis server. This client is used for subscribing to the job's event channel and receiving progress updates in real-time.
    client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    #   Create a pubsub object from the Redis client. This object allows us to subscribe to channels and receive messages published to those channels.
    pubsub = client.pubsub()
    try:
        #   Subscribe to the Redis pub/sub channel for this job. This ensures that we receive any progress events published by the worker for this job.
        await pubsub.subscribe(_events_channel(job_id))

        #   Read the current job state from Redis. 
        #   This snapshot is used to send an immediate progress update to the client, so they see the current state of the job 
        #   even if they connected after some events were already published.
        snapshot = await client.hgetall(_job_key(job_id))
        if not snapshot:
            yield _sse("error", {"detail": "unknown job_id"})
            return

        # Current state first, so reconnecting clients render immediately.
        yield _sse(
            "progress",
            {
                "stage": snapshot.get("stage", ""),
                "message": snapshot.get("message", ""),
                "percent": int(snapshot.get("percent") or 0),
            },
        )
        if snapshot.get("status") in _TERMINAL:
            yield _terminal_frame(job_id, snapshot)
            return

        while True:
            #   Wait 15 seconds for the next message on the pub/sub channel. 
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=15.0
            )

            #   If no message arrives, send a heartbeat comment to keep the connection alive.
            if message is None:
                yield ": keep-alive\n\n"  # heartbeat comment
                continue

            #   Convert the Redis pub/sub message into an SSE-formatted message and yields it to the browser.
            payload = json.loads(message["data"])
            yield _sse(payload["event"], payload["data"])

            
            if payload["event"] in ("completed", "failed"):
                return
            
    finally:
        await pubsub.unsubscribe(_events_channel(job_id))
        await pubsub.aclose()
        await client.aclose()


def _terminal_frame(job_id: str, snapshot: dict[str, str]) -> str:
    if snapshot.get("status") == JobStatus.COMPLETED.value:
        return _sse("completed", {"job_id": job_id, "result_url": _result_url(job_id)})
    return _sse("failed", {"job_id": job_id, "error": snapshot.get("error", "")})
