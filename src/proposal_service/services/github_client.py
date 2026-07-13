"""GitHub REST API client for the Issues-based proposal integration.

This is the GitHub counterpart to :mod:`planner_client`. It authenticates with
a single Personal Access Token (no token exchange, unlike Planner's
client-credentials flow) and creates milestones, labels, and issues in one
repository shared across many proposals.

All "create" operations are lookup-or-create so re-running a push is
idempotent: existing labels and milestones are reused by name/title, and an
issue with a matching title under the same milestone is skipped rather than
duplicated. :func:`push_to_github` is the single entry point used by the
``/proposal/github`` route.
"""

from __future__ import annotations

import logging
from typing import Any

import requests


logger = logging.getLogger(__name__)

# Default request timeouts: (connect, read) seconds.
#   Wait 10s to connect, then 30s for the response body. 
#   GitHub's API is usually fast, but some proposals have hundreds of issues and can take a while to create.
_DEFAULT_TIMEOUT = (10, 30)
_PER_PAGE = 100


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _paginated_get(
    url: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """GET every page of a list endpoint and return the concatenated items."""


    results: list[dict[str, Any]] = []
    page = 1
    #   If params was given, copy it into a new dictionary. 
    #   If params is None, use an empty dictionary.
    #   Because later page_parameters need **base_params to be a dict.
    base_params = dict(params or {})
    #   Keep requesting pages until there are no more pages.
    while True:
        page_params = {**base_params, "per_page": _PER_PAGE, "page": page}
        response = requests.get(
            url,
            headers=_headers(token),
            params=page_params,
            timeout=_DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        batch = response.json()
        if not batch:
            break
        results.extend(batch)
        if len(batch) < _PER_PAGE:
            break
        page += 1
    return results


# Labels: makes sure all needed labels exist in the GitHub repo.
def ensure_labels(
    payload_labels: list[dict[str, Any]],
    *,
    token: str,
    base: str,
    owner: str,
    repo: str,
) -> None:
    """Create any labels that don't already exist (matched by name)."""

    #   GitHub's API endpoint for labels in that repo
    url = f"{base}/repos/{owner}/{repo}/labels"
    #   Get the existing labels in that repo, and store their names in lowercase in a set.
    existing = {lbl["name"].lower() for lbl in _paginated_get(url, token=token)}

    #   Create any labels that don't already exist.
    for label in payload_labels:
        if label["name"].lower() in existing:
            continue
        body: dict[str, Any] = {
            "name": label["name"],
            "color": label.get("color", "ededed"),
        }
        if label.get("description"):
            body["description"] = label["description"]

        response = requests.post(
            url, headers=_headers(token), json=body, timeout=_DEFAULT_TIMEOUT
        )
        # A 422 here means another writer created it first — treat as present.
        if response.status_code == 422:
            logger.debug("Label %s already exists (race)", label["name"])
            continue
        response.raise_for_status()
        existing.add(label["name"].lower())
        logger.info("Created label %s", label["name"])


# Milestones
def ensure_milestones(
    payload_milestones: list[dict[str, Any]],
    *,
    token: str,
    base: str,
    owner: str,
    repo: str,
) -> dict[str, int]:
    """Lookup-or-create each milestone; return ``{external_id: number}``."""

    #   GitHub's API endpoint for milestones in that repo
    url = f"{base}/repos/{owner}/{repo}/milestones"
    #
    existing = {
        ms["title"]: ms["number"]
        for ms in _paginated_get(url, token=token, params={"state": "all"})
    }

    milestone_map: dict[str, int] = {}
    for milestone in payload_milestones:
        title = milestone["title"]
        if title in existing:
            milestone_map[milestone["external_id"]] = existing[title]
            logger.debug("Reusing milestone %s (#%d)", title, existing[title])
            continue

        body: dict[str, Any] = {"title": title}
        if milestone.get("description"):
            body["description"] = milestone["description"]
        if milestone.get("due_on"):
            body["due_on"] = milestone["due_on"]

        response = requests.post(
            url, headers=_headers(token), json=body, timeout=_DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        number = response.json()["number"]
        existing[title] = number
        milestone_map[milestone["external_id"]] = number
        logger.info("Created milestone %s (#%d)", title, number)

    return milestone_map


# Issues
def _existing_issue_titles(
    *,
    token: str,
    base: str,
    owner: str,
    repo: str,
    milestone_number: int,
) -> dict[str, int]:
    """Return ``{title: issue_number}`` for issues under one milestone."""
    url = f"{base}/repos/{owner}/{repo}/issues"
    items = _paginated_get(
        url,
        token=token,
        params={"milestone": milestone_number, "state": "all"},
    )
    # The issues endpoint also returns pull requests; skip those.
    return {
        item["title"]: item["number"] for item in items if "pull_request" not in item
    }


def create_issue(
    *,
    token: str,
    base: str,
    owner: str,
    repo: str,
    title: str,
    body: str,
    milestone_number: int,
    labels: list[str],
) -> int:
    """Create a single issue under ``milestone_number`` and return its number."""

    url = f"{base}/repos/{owner}/{repo}/issues"
    payload = {
        "title": title,
        "body": body,
        "milestone": milestone_number,
        "labels": labels,
    }
    response = requests.post(
        url, headers=_headers(token), json=payload, timeout=_DEFAULT_TIMEOUT
    )
    response.raise_for_status()
    return response.json()["number"]


# Full push pipeline
def push_to_github(
    payload: dict[str, Any],
    *,
    token: str,
    owner: str,
    repo: str,
    base_url: str = "https://api.github.com",
) -> dict[str, Any]:
    """Push a GitHub payload to one repository, idempotently.

    Args:
        payload: Output of :func:`github_adapter.build_github_payload`.
        token: GitHub Personal Access Token with Issues read/write on the repo.
        owner: Repository owner (user or org login).
        repo: Repository name.
        base_url: API root; override for GitHub Enterprise Server.

    Returns:
        ``{"milestone_map", "issue_map", "skipped_issues"}`` mapping external
        ids to the numbers GitHub assigned.
    """
    base = base_url.rstrip("/")

    ensure_labels(payload["labels"], token=token, base=base, owner=owner, repo=repo)
    milestone_map = ensure_milestones(
        payload["milestones"], token=token, base=base, owner=owner, repo=repo
    )

    issue_map: dict[str, int] = {}
    skipped: list[str] = []
    # Cache existing-issue lookups so each milestone is scanned at most once.
    seen_titles: dict[int, dict[str, int]] = {}

    for issue in payload["issues"]:
        milestone_number = milestone_map[issue["milestone_external_id"]]
        if milestone_number not in seen_titles:
            seen_titles[milestone_number] = _existing_issue_titles(
                token=token,
                base=base,
                owner=owner,
                repo=repo,
                milestone_number=milestone_number,
            )

        existing = seen_titles[milestone_number]
        if issue["title"] in existing: 
            issue_map[issue["external_id"]] = existing[issue["title"]]
            skipped.append(issue["external_id"])
            logger.debug("Skipping existing issue %s", issue["title"])
            continue

        number = create_issue(
            token=token,
            base=base,
            owner=owner,
            repo=repo,
            title=issue["title"],
            body=issue["body"],
            milestone_number=milestone_number,
            labels=issue["labels"],
        )
        existing[issue["title"]] = number
        issue_map[issue["external_id"]] = number

    logger.info(
        "Pushed to %s/%s: %d milestone(s), %d issue(s) created, %d skipped",
        owner,
        repo,
        len(milestone_map),
        len(issue_map) - len(skipped),
        len(skipped),
    )
    return {
        "milestone_map": milestone_map,
        "issue_map": issue_map,
        "skipped_issues": skipped,
    }
