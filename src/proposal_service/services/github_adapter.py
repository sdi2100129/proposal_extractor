"""Build GitHub Issues-ready payloads from WorkPackage models.

This is the GitHub counterpart to :mod:`planner_adapter`. It turns the
assembled :class:`WorkPackage` list into a flat, backend-agnostic payload of
milestones, labels, and issues that :mod:`github_client` then creates via the
GitHub REST API.

Mapping:
    Work package -> milestone (one per WP; WP end month -> milestone due date)
    Task         -> issue (assigned to the WP's milestone)
    Deliverables -> task-list checkboxes inside the issue body
    Partners     -> labels on the issue
    Leader/role  -> a ``role:*`` label

Because one repository hosts many proposals, milestone and issue titles are
prefixed with the proposal title to keep them unambiguous, and every issue
carries a ``proposal:<slug>`` label so a whole proposal can be filtered out of
the shared repo later.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date

from dateutil.relativedelta import relativedelta

from proposal_service.models import Task, WorkPackage
from proposal_service.utils import month_number


def _slugify(text: str) -> str:
    """Lowercase ``text`` and collapse non-alphanumerics into single hyphens."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "proposal"


def _month_to_iso_date(
    proposal_start_date: str,
    month: str,
    *,
    end_of_month: bool = False,
) -> str | None:
    """Convert ``"M05"`` to an ISO 8601 timestamp GitHub accepts for ``due_on``.

    Mirrors :func:`planner_adapter.month_to_planner_date`. GitHub only exposes a
    due date on milestones (issues have no native date fields), so this is used
    solely for the WP end month.
    """
    month_no = month_number(month)
    if month_no <= 0:
        return None

    start = date.fromisoformat(proposal_start_date)
    target = start + relativedelta(months=month_no - 1)
    if end_of_month:
        target = target + relativedelta(months=1, days=-1)

    # 08:00Z keeps the displayed calendar day correct across western time zones.
    return target.isoformat() + "T08:00:00Z"


def _label_color(name: str) -> str:
    """Deterministic 6-hex color so a given acronym always looks the same."""
    #   MD5 is not secure for cryptographic/security purposes we only using it for a harmless deterministic hash.
    #   Then convert the binary hash result into a readable hexadecimal string
    #   GitHub label colors need 6 hexadecimal characters
    digest = hashlib.md5(name.encode("utf-8"), usedforsecurity=False).hexdigest()
    return digest[:6]


def _months_range(task: Task) -> str:
    if task.start_month and task.end_month:
        return f"{task.start_month}\u2013{task.end_month}"
    return task.start_month or task.end_month or ""


def _build_issue_body(task: Task, *, months: str) -> str:
    """Render a Markdown issue body: schedule, role, partners, description, checklist."""
    lines: list[str] = []

    meta: list[str] = []
    if months:
        meta.append(f"**Schedule:** {months}")
    if task.role:
        meta.append(f"**Role:** {task.role.value}")
    if task.partners:
        meta.append(f"**Partners:** {', '.join(task.partners)}")
    if meta:
        lines.extend(meta)
        lines.append("")

    if task.description:
        lines.append(task.description)
        lines.append("")

    if task.deliverables:
        lines.append("**Deliverables**")
        for deliv in task.deliverables:
            label = deliv.name or deliv.description or deliv.id
            due = f" ({', '.join(deliv.due_months)})" if deliv.due_months else ""
            lines.append(f"- [ ] {deliv.id} \u2013 {label}{due}")

    return "\n".join(lines).strip()


def build_github_payload(
    work_packages: list[WorkPackage],
    *,
    proposal_start_date: str,
    plan_title: str = "Proposal Plan",
) -> dict:
    """Convert WorkPackage models into a GitHub-ready payload.

    Args:
        work_packages: Assembled work packages from Stage 3.
        proposal_start_date: ISO ``YYYY-MM-DD`` proposal start, used to turn
            ``Mnn`` markers into real calendar dates for milestone due dates.
        plan_title: Human title for this proposal. Prefixes milestone/issue
            titles and seeds the ``proposal:<slug>`` label so multiple
            proposals coexist in one repo.

    Returns:
        ``{"milestones": [...], "labels": [...], "issues": [...]}`` with stable
        ``external_id`` values that the client maps onto GitHub numbers.
    """
    prefix = plan_title.strip()
    proposal_label = f"proposal:{_slugify(plan_title)}"

    milestones: list[dict] = []
    issues: list[dict] = []

    #   GitHub labels
    #   The key is Label name -> labeldescription; deduped so each label is emitted once.
    label_names: dict[str, str] = {proposal_label: "All issues from this proposal"}

    for wp in work_packages:
        due_on = _month_to_iso_date(proposal_start_date, wp.end_month, end_of_month=True)

        desc_bits = [f"Work package {wp.id}"]
        if wp.leader:
            desc_bits.append(f"leader: {wp.leader}")
        if wp.effort_pm:
            desc_bits.append(f"effort: {wp.effort_pm} PM")

        milestones.append(
            {
                "external_id": wp.id,
                "title": f"{prefix} \u00b7 {wp.id} \u2013 {wp.title}".strip(" \u00b7-"),
                "description": " \u00b7 ".join(desc_bits),
                "due_on": due_on,
            }
        )

        for task in wp.tasks:
            role_label = f"role:{task.role.value}"
            label_names.setdefault(role_label, "Company role on this task")

            issue_labels = [proposal_label, role_label]
            for partner in task.partners:
                partner = partner.strip()
                if partner:
                    label_names.setdefault(partner, "Partner")
                    issue_labels.append(partner)

            issues.append(
                {
                    "external_id": f"{wp.id}:{task.id}",
                    "title": f"{prefix} \u00b7 {task.id} \u2013 {task.title}".strip(" \u00b7-"),
                    "body": _build_issue_body(task, months=_months_range(task)),
                    "milestone_external_id": wp.id,
                    "labels": issue_labels,
                }
            )

    labels = [
        {"name": name, "color": _label_color(name), "description": desc}
        for name, desc in label_names.items()
    ]

    return {"milestones": milestones, "labels": labels, "issues": issues}
