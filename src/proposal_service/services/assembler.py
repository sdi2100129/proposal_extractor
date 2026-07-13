"""Stage-3 assembly: merge tables and tasks into WorkPackage models."""

from __future__ import annotations

import logging
import re

from proposal_service.models import Deliverable, Role, Task, WorkPackage
from proposal_service.utils import company_in_partners, first_listed, month_number


logger = logging.getLogger(__name__)


def build_deliverable_map(raw_delivs: list[dict]) -> dict[str, list[Deliverable]]:
    """
    Map each deliverable to its WP.
    Returns { "WP1": [Deliverable, ...], "WP5": [...], ... }
    """

    wp_map: dict[str, list[Deliverable]] = {}

    for rd in raw_delivs:
        wp = rd.get("wp", "")
        if not wp:
            continue

        #months = rd["months"]
        for d_id in rd["ids"]:
            #due = [months[i]] if i < len(months) else ([months[-1]] if months else [])


            deliv = Deliverable(
                id=d_id,
                name=rd.get("name", ""),
                description=rd.get("description", ""),
                lead=rd.get("lead", ""),
                type=rd.get("type", ""),
                dissemination=rd.get("dissemination", ""),
                due_months=rd.get("months", []),
                planner_due_dates=[],
            )

            wp_map.setdefault(wp, []).append(deliv)
    return wp_map


def assign_deliverables_to_tasks(
    tasks: list[Task],
    wp_deliverables: list[Deliverable],
    *,
    max_tasks_per_deliverable: int | None = 3,
) -> None:
    """Attach each deliverable to the closest matching tasks in-place."""

    def month_num(m: str) -> int:
        match = re.search(r"\d+", m or "")
        return int(match.group()) if match else 0

    for deliv in wp_deliverables:
        due_months = [month_num(m) for m in deliv.due_months if month_num(m) > 0]
        if not due_months:
            continue

        #   Candidate tasks. Will store: (distance from due date, task)
        matched: list[tuple[int, Task]] = []

        for task in tasks:
            t_start = month_num(task.start_month)
            t_end = month_num(task.end_month)
            if t_start == 0 or t_end == 0:
                continue

            overlaps = any(t_start <= due <= t_end for due in due_months)
            if not overlaps:
                continue

            #   How close the task finishes to the deliverable deadline
            distance = min(abs(t_end - due) for due in due_months)
            matched.append((distance, task))

        #   Sort by distance to deadline (closest first)
        matched.sort(key=lambda item: item[0])

        #   Keep the max_tasks closest
        selected = matched if max_tasks_per_deliverable is None else matched[:max_tasks_per_deliverable]
        
        for _, task in selected:
            if deliv not in task.deliverables:
                task.deliverables.append(deliv)



def assemble(
    wp_info: dict[str, dict],
    effort: dict[str, float],
    raw_tasks: list[dict],
    raw_delivs: list[dict],
    *,
    company: str,
    proposal_start_date: str,
) -> list[WorkPackage]:
    """Combine all parsed data into a list of WorkPackage objects.

    Only WPs and tasks where ``company`` appears (as leader or partner) are
    kept.
    """
    company = company.upper()

    # WPs where NCI has effort
    company_wp_ids = {wp for wp, pm in effort.items() if pm > 0}
    
    # Build deliverable map
    deliverable_map = build_deliverable_map(raw_delivs)
    
    # Group tasks by WP, keeping only NCI-relevant ones
    tasks_by_wp: dict[str, list[Task]] = {wp: [] for wp in company_wp_ids}

    placeholder_month = month_number(proposal_start_date)

    for raw in raw_tasks:
        wp_id = raw["wp_id"].upper()
        if wp_id not in company_wp_ids:
            logger.warning(
                "Dropping task %s: WP %s not in effort table (company_wp_ids=%s)",
                raw.get("id"), wp_id, sorted(company_wp_ids),
            )
            continue
        partners = raw.get("partners", [])
        if not company_in_partners(partners, company):
            logger.warning(
                "Dropping task %s: company %s not in partners=%s",
                raw.get("id"), company, partners,
            )
            continue

        role = (
            Role.LEADER
            if first_listed(partners).upper() == company
            else Role.PARTICIPANT
        )

        task = Task(
            id=raw["id"],
            title=raw.get("title", ""),
            start_month=raw.get("start_month", ""),
            end_month=raw.get("end_month", ""),
            partners=partners,
            role=role,
            description=raw.get("description", ""),
            planner_start_date=str(placeholder_month),
            planner_due_date=str(placeholder_month),
        )
        
        # Attach deliverables
        tasks_by_wp[wp_id].append(task)

    # Assign deliverables after all tasks for a WP are known
    for wp_id, tasks in tasks_by_wp.items():
        assign_deliverables_to_tasks(tasks, deliverable_map.get(wp_id, []))

    # Build WorkPackage objects
    result: list[WorkPackage] = []
    for wp_id in sorted(company_wp_ids):
        info = wp_info.get(wp_id, {})
        role = (
            Role.LEADER
            if info.get("leader", "").upper() == company
            else Role.PARTICIPANT
        )
        wp = WorkPackage(
            id=wp_id,
            title=info.get("title", ""),
            leader=info.get("leader", ""),
            role=role,
            effort_pm=effort.get(wp_id, 0.0),
            start_month=info.get("start", ""),
            end_month=info.get("end", ""),
            tasks=tasks_by_wp.get(wp_id, []),
        )
        result.append(wp)

    logger.info(
        "Assembled %d work packages for company=%s (%d total tasks)",
        len(result),
        company,
        sum(len(wp.tasks) for wp in result),
    )
    return result

    