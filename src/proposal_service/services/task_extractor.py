"""LLM-backed task and table classification via Ollama."""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

import requests

from proposal_service.config import get_settings
from proposal_service.utils import clean


logger = logging.getLogger(__name__)


class OllamaUnavailable(RuntimeError):
    """Raised when the Ollama backend cannot be reached or times out."""


_VALID_TABLE_LABELS = {"wp_list", "effort", "deliverable", "other"}
_VALID_PAGE_LABELS = {"toc_or_index", "section3_start", "section3_body", "other"}


def _ollama_chat(
    prompt: str,
    *,
    options: dict[str, Any],
    connect_timeout: float | None = None,
    read_timeout: float | None = None,
) -> str:
    """Call Ollama's ``/api/chat`` endpoint and return the response text.

    Args:
        prompt: Full user prompt.
        options: Ollama request options (``num_ctx``, ``temperature``, ...).
        connect_timeout: Override the configured connect timeout.
        read_timeout: Override the configured read timeout.

    Raises:
        OllamaUnavailable: When the request fails or times out.
    """
    settings = get_settings()
    url = f"{str(settings.ollama_url).rstrip('/')}/api/chat"
    payload = {
        "model": settings.ollama_model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": options,
    }
    timeout = (
        connect_timeout if connect_timeout is not None else settings.ollama_connect_timeout_seconds,
        read_timeout if read_timeout is not None else settings.ollama_read_timeout_seconds,
    )
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.ReadTimeout as exc:
        raise OllamaUnavailable("Ollama timed out") from exc
    except requests.RequestException as exc:
        raise OllamaUnavailable(f"Ollama request failed: {exc}") from exc

    data = response.json()
    return data["message"]["content"].strip()


def _strip_code_fences(content: str) -> str:
    """Remove leading/trailing ``` and optional ``json`` token."""
    if content.startswith("```"):
        content = content.strip("`")
        content = content.replace("json", "", 1).strip()
    return content


def extract_tasks_via_llm(wp_text: str) -> list[dict]:
    """Extract structured task data from a single WP section.

    Args:
        wp_text: The prose text of one work-package section.

    Returns:
        A list of raw task dicts as parsed from the LLM JSON output. Returns
        an empty list if the response is not valid JSON.

    Raises:
        OllamaUnavailable: When Ollama is unreachable or times out.
    """
    prompt = f"""
You extract structured Horizon Europe task data from one Work Package section.

Return ONLY valid JSON.
Do not include markdown fences.
Extract ALL explicitly labeled tasks.
Do not stop early.

A task is any item explicitly labeled like T1.1, T2.3, T5.8.

Extraction rules:
- Extract only tasks explicitly labeled with IDs like T1.1, T2.3, T5.8.
- Do not invent tasks.
- The title may be on the same line as the task ID or in nearby text.
- The description may span one or more following lines until the next task ID
  or section heading.
- Partners may appear in parentheses or after labels like "Participants",
  "Partner(s)", "Task Partners" or "Lead beneficiary".
- If partners include "All Partners" or "All", return ["All Partners"].
- If partners are missing, return [].
- If months are missing, return "" for start_month and end_month.
- If description is missing, return "".

Schema:
[
  {{
    "id": "T1.1",
    "wp_id": "WP1",
    "title": "Task title",
    "start_month": "M01",
    "end_month": "M12",
    "partners": ["NCI", "UCD"],
    "description": "short description"
  }}
]

TEXT:
{wp_text}
""".strip()

    settings = get_settings()
    content = _ollama_chat(
        prompt,
        options={
            "num_ctx": settings.ollama_num_ctx,
            "temperature": 0,
            "num_predict": 1800,
        },
    )

    try:
        result = json.loads(_strip_code_fences(content))
    except json.JSONDecodeError as exc:
        logger.warning("Could not parse Ollama task response as JSON: %s", exc)
        logger.debug("Raw LLM response that failed to parse: %s", content[:1000])
        return []

    if not isinstance(result, list):
        logger.warning(
            "Ollama task response was not a JSON array; got type=%s, preview=%r",
            type(result).__name__,
            content[:200],
        )
        return []

    if not result:
        # Empty list — log enough to see which prompt produced no tasks.
        # The first line of wp_text is the header we injected ("WP Number WPx\n").
        first_line = wp_text.splitlines()[0] if wp_text else ""
        logger.warning(
            "Ollama returned EMPTY task list. Header=%r, prompt snippet=%r",
            first_line,
            wp_text[:300],
        )
    else:
        logger.debug(
            "LLM returned %d tasks: %s",
            len(result),
            [t.get("id") for t in result if isinstance(t, dict)],
        )

    return result


def validate_task(raw: dict) -> dict | None:
    """Validate that a raw task dict from the LLM has all required fields.

    Returns the dict unchanged if valid, otherwise ``None``.
    """
    required = (
        "id",
        "wp_id",
        "title",
        "start_month",
        "end_month",
        "partners",
        "description",
    )
    for key in required:
        if key not in raw:
            logger.warning(
                "Task missing field %r: %s — skipping",
                key,
                raw.get("id", "?"),
            )
            return None

    if not isinstance(raw["partners"], list):
        logger.warning("Task %s: 'partners' is not a list — skipping", raw["id"])
        return None
    return raw


def classify_table_via_llm(table_rows: list[list[str]]) -> str:
    """Classify a table as ``wp_list``, ``effort``, ``deliverable``, or ``other``."""
    preview = "\n".join(
        " | ".join(clean(c) for c in row if c is not None)
        for row in table_rows[:20]
        if row
    )

    prompt = f"""
You classify tables from Horizon Europe grant agreement proposals.

Return ONLY one of these exact strings:
wp_list
effort
deliverable
other

'wp_list': compact summary table listing multiple work packages with columns
like WP, WP Title, Leader/Lead, PMs/Person-Months, Start, End.

'effort': person-months / effort matrix. Partner acronyms on one axis, WP
identifiers on the other, mostly numeric body.

'deliverable': contains deliverable headers (Deliverable, Del., Diss, Type,
Deliv date), deliverable IDs like D1.1, dissemination codes (PU, CO, SEN,
RE), and due months like M04.

'other': milestones, risks, costs, partner profiles, WP description blocks.

TABLE PREVIEW:
{preview}
""".strip()

    try:
        content = _ollama_chat(
            prompt,
            options={"temperature": 0, "num_predict": 20},
            connect_timeout=10,
            read_timeout=30,
        )
    except OllamaUnavailable as exc:
        logger.warning("Table classification fell back to 'other': %s", exc)
        return "other"

    result = content.strip().lower()
    if result in _VALID_TABLE_LABELS:
        return result
    for label in _VALID_TABLE_LABELS - {"other"}:
        if label in result:
            return label
    return "other"


def classify_page_once_via_llm(page_text: str, page_num: int) -> str:
    """Single-shot LLM classification of a page; see :func:`classify_toc_or_not_via_llm`."""
    prompt = f"""
You classify pages from Horizon Europe proposals.

Return ONLY one of these exact strings:
toc_or_index
section3_start
section3_body
other

Definitions:
- toc_or_index: the page is a table of contents or index.
- section3_start: the first page where actual Section 3 BODY TEXT appears.
- section3_body: page inside Section 3 body, but not the first.
- other: none of the above.

PAGE {page_num}:
{page_text[:5000]}
""".strip()

    try:
        content = _ollama_chat(
            prompt,
            options={"temperature": 0, "num_predict": 30},
            connect_timeout=10,
            read_timeout=30,
        )
    except OllamaUnavailable as exc:
        logger.warning("Page classification fell back to 'other': %s", exc)
        return "other"

    result = content.strip().lower()
    if result in _VALID_PAGE_LABELS:
        return result
    for label in _VALID_PAGE_LABELS - {"other"}:
        if label in result:
            return label
    return "other"


def classify_toc_or_not_via_llm(page_text: str, page_num: int, retries: int = 3) -> str:
    """Majority vote across ``retries`` LLM calls; prefers Section 3 starts on ties."""
    votes = [classify_page_once_via_llm(page_text, page_num) for _ in range(retries)]
    counts = Counter(votes)
    logger.debug("page %d classification votes: %s", page_num, votes)

    best_label, best_count = counts.most_common(1)[0]
    if len([label for label, c in counts.items() if c == best_count]) > 1:
        priority = ["section3_start", "section3_body", "toc_or_index", "other"]
        tied = [label for label, c in counts.items() if c == best_count]
        for candidate in priority:
            if candidate in tied:
                return candidate
    return best_label


    