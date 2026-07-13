"""
Microsoft Graph / Planner client.

Handles OAuth client-credentials token acquisition and the four Graph
endpoints we need: create plan, create bucket, create task, update task
details (for checklist + description). The :func:`push_to_planner`
function is the single entry point used by the ``/proposal/planner`` route.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import requests


logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Default request timeouts: (connect, read) seconds.
_DEFAULT_TIMEOUT = (10, 30)


# Authentication
def get_token(
    client_id: str,
    client_secret: str,
    tenant_id: str,
) -> str:
    """Obtain an app-only access token from Azure AD.

    Args:
        client_id: Azure AD application (client) ID.
        client_secret: Azure AD application client secret.
        tenant_id: Azure AD tenant (directory) ID.

    Returns:
        The bearer token string. Caller is responsible for caching.

    Raises:
        requests.HTTPError: If Azure AD rejects the request.
    """
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    response = requests.post(url, data=data, timeout=_DEFAULT_TIMEOUT)
    response.raise_for_status()
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# Graph endpoints
def create_plan(token: str, *, owner_group_id: str, title: str) -> str:
    """Create a new Planner plan owned by an M365 group."""
    payload = {"owner": owner_group_id, "title": title}
    response = requests.post(
        f"{GRAPH_BASE}/planner/plans",
        headers=_headers(token),
        json=payload,
        timeout=_DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    plan_id = response.json()["id"]
    logger.info("Created Planner plan id=%s title=%s", plan_id, title)
    return plan_id


def create_bucket(token: str, *, plan_id: str, name: str) -> str:
    """Create a bucket inside an existing plan."""
    payload = {"name": name, "planId": plan_id, "orderHint": " !"}
    response = requests.post(
        f"{GRAPH_BASE}/planner/buckets",
        headers=_headers(token),
        json=payload,
        timeout=_DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["id"]


def create_task(
    token: str,
    *,
    plan_id: str,
    bucket_id: str,
    title: str,
    start: str | None,
    due: str | None,
) -> str:
    """Create a Planner task under a bucket."""
    payload: dict[str, Any] = {
        "planId": plan_id,
        "bucketId": bucket_id,
        "title": title,
    }
    if start:
        payload["startDateTime"] = start
    if due:
        payload["dueDateTime"] = due

    response = requests.post(
        f"{GRAPH_BASE}/planner/tasks",
        headers=_headers(token),
        json=payload,
        timeout=_DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["id"]


def get_task_details(token: str, task_id: str) -> tuple[dict[str, Any], str | None]:
    """Fetch task details + the ETag required for subsequent PATCH calls."""
    response = requests.get(
        f"{GRAPH_BASE}/planner/tasks/{task_id}/details",
        headers=_headers(token),
        timeout=_DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    return response.json(), response.headers.get("ETag")


def update_task_details(
    token: str,
    *,
    task_id: str,
    description: str | None = None,
    checklist_items: list[dict[str, Any]] | None = None,
) -> None:
    """Patch a task's description and checklist.

    Microsoft Graph requires an ``If-Match`` header set to the current ETag,
    so this performs a GET first.
    """
    _, etag = get_task_details(token, task_id)
    payload: dict[str, Any] = {}
    if description:
        payload["description"] = description
    if checklist_items:
        payload["checklist"] = {
            str(uuid.uuid4()): {
                "@odata.type": "microsoft.graph.plannerChecklistItem",
                "title": item["title"],
                "isChecked": False,
            }
            for item in checklist_items
        }
    if not payload:
        return

    headers = _headers(token)
    if etag:
        headers["If-Match"] = etag
    response = requests.patch(
        f"{GRAPH_BASE}/planner/tasks/{task_id}/details",
        headers=headers,
        json=payload,
        timeout=_DEFAULT_TIMEOUT,
    )
    response.raise_for_status()


# Full push pipeline
def push_to_planner(
    payload: dict[str, Any],
    *,
    token: str,
    owner_group_id: str,
    plan_title: str = "Proposal Plan",
) -> dict[str, Any]:
    """Push a complete Planner payload to Microsoft Graph.

    Args:
        payload: Output of :func:`planner_adapter.build_planner_payload`.
        token: Access token from :func:`get_token`.
        owner_group_id: M365 group ID that will own the new plan.
        plan_title: Display title of the new plan.

    Returns:
        ``{"plan_id", "bucket_map", "task_map"}`` mapping external ids to
        the IDs assigned by Graph.
    """
    plan_id = create_plan(token, owner_group_id=owner_group_id, title=plan_title)

    bucket_map: dict[str, str] = {}
    for bucket in payload["buckets"]:
        bucket_id = create_bucket(token, plan_id=plan_id, name=bucket["name"])
        bucket_map[bucket["external_id"]] = bucket_id

    task_map: dict[str, str] = {}
    for task in payload["tasks"]:
        bucket_id = bucket_map[task["bucket_external_id"]]
        task_id = create_task(
            token,
            plan_id=plan_id,
            bucket_id=bucket_id,
            title=task["title"],
            start=task.get("startDateTime"),
            due=task.get("dueDateTime"),
        )
        task_map[task["external_id"]] = task_id

        details = task.get("details", {})
        description = (
            f"Description:\n{details.get('description', '')}\n\n"
            f"Role: {details.get('role', '')}\n"
            f"Partners: {details.get('partners', '')}"
        )
        update_task_details(
            token,
            task_id=task_id,
            description=description,
            checklist_items=task.get("checklist", []),
        )

    logger.info(
        "Pushed Planner payload: %d bucket(s), %d task(s) created",
        len(bucket_map),
        len(task_map),
    )
    return {"plan_id": plan_id, "bucket_map": bucket_map, "task_map": task_map}
