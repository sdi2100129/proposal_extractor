"""Build Microsoft Planner-ready payloads from WorkPackage models."""

from __future__ import annotations

from datetime import date

from dateutil.relativedelta import relativedelta

from proposal_service.models import WorkPackage
from proposal_service.utils import month_number


def month_to_planner_date(
    proposal_start_date: str,
    month: str,
    *,
    end_of_month: bool = False,
) -> str | None:
    """
    Convert "M05" → ISO datetime string for MS Planner.

    Planner expects:
        YYYY-MM-DDTHH:MM:SSZ
    """

    month_no = month_number(month)
    if month_no <= 0:
        return None

    start = date.fromisoformat(proposal_start_date)

    target = start + relativedelta(months=month_no - 1)

    if end_of_month:
        target = target + relativedelta(months=1, days=-1)

    # Planner prefers a time component
    return target.isoformat() + "T17:00:00Z"


# Build Planner payload
def build_planner_payload(
    work_packages: list[WorkPackage],
    *,
    proposal_start_date: str,
) -> dict:
    """
    Convert WorkPackage model → Planner-ready payload.

    Output structure:

    {
        "buckets": [...],
        "tasks": [...]
    }
    """

    buckets: list[dict] = []
    tasks: list[dict] = []

    # Buckets (one per WP)
    for wp in work_packages:
        buckets.append(
            {
                "external_id": wp.id,               # keep mapping for later Graph linking
                "name": f"{wp.id} - {wp.title}".strip(" -"),
            }
        )

    # Tasks 
    for wp in work_packages:
        for task in wp.tasks:

            start_date = month_to_planner_date(
                proposal_start_date,
                task.start_month,
                end_of_month=False,
            )

            due_date = month_to_planner_date(
                proposal_start_date,
                task.end_month,
                end_of_month=True,
            )

            #   Checklist items (one per deliverable due month)
            checklist_items: list[dict] = []
            for deliv in task.deliverables:

                # Support multiple due months (rare but exists)
                for due_month in deliv.due_months:

                    due_date_deliv = month_to_planner_date(
                        proposal_start_date,
                        due_month,
                        end_of_month=True,
                    )

                    title_text = (
                        f"{deliv.id} - {deliv.name or deliv.description} "
                        f"({due_month}, {due_date_deliv})"
                    ).strip(" -")
                    checklist_items.append(
                        {
                            "title": title_text,
                            "external_id": deliv.id,
                        }
                    )

            # Task payload
            tasks.append(
                {
                    "external_id": f"{wp.id}:{task.id}",

                    # Graph Planner fields
                    "title": f"{task.id} - {task.title}",
                    "bucket_external_id": wp.id,
                    "startDateTime": start_date,
                    "dueDateTime": due_date,

                    # Custom metadata (you can store in description or extensions)
                    "details": {
                        "description": task.description,
                        "role": task.role.value,
                        "partners": ", ".join(task.partners),
                    },
                    "checklist": checklist_items,
                }
            )

    return {"buckets": buckets, "tasks": tasks}

