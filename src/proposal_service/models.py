"""Domain models for the proposal pipeline.

These models describe the structures the pipeline produces and pass between
its three stages. API-layer request and response wrappers live in
``schemas.py`` instead, per the skills guide.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Role(str, Enum):
    """Whether the configured company leads a WP/task or merely participates."""

    LEADER = "leader"
    PARTICIPANT = "participant"


class Deliverable(BaseModel):
    """A single deliverable extracted from the proposal."""

    id: str = Field(..., description="Deliverable identifier, e.g. 'D1.1'.")
    name: str = Field(default="", description="Short deliverable title.")
    description: str = Field(default="", description="Longer description text.")
    lead: str = Field(default="", description="Partner acronym leading the deliverable.")
    type: str = Field(default="", description="Deliverable type code (R, DEM, OTHER...).")
    dissemination: str = Field(
        default="",
        description="Dissemination level (PU, SEN, CO, RE).",
    )
    due_months: list[str] = Field(
        default_factory=list,
        description="Due months in 'Mnn' format.",
    )
    planner_due_dates: list[str] = Field(
        default_factory=list,
        description="Resolved ISO due dates for Planner integration.",
    )

    @field_validator("id")
    @classmethod
    def _normalize_id(cls, value: str) -> str:
        return value.strip().upper()


class Task(BaseModel):
    """A single task that the configured company participates in."""

    id: str = Field(..., description="Task identifier, e.g. 'T1.1'.")
    title: str = Field(default="")
    start_month: str = Field(default="", description="Start month in 'Mnn' format.")
    end_month: str = Field(default="", description="End month in 'Mnn' format.")
    partners: list[str] = Field(default_factory=list)
    role: Role = Field(default=Role.PARTICIPANT)
    description: str = Field(default="")
    planner_start_date: str = Field(default="")
    planner_due_date: str = Field(default="")
    deliverables: list[Deliverable] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _normalize_id(cls, value: str) -> str:
        return value.strip().upper()


class WorkPackage(BaseModel):
    """A work package and the tasks the configured company is part of."""

    id: str = Field(..., description="Work-package identifier, e.g. 'WP1'.")
    title: str = Field(default="")
    leader: str = Field(default="")
    role: Role = Field(default=Role.PARTICIPANT)
    effort_pm: float = Field(default=0.0, ge=0.0, description="Person-months allocated.")
    start_month: str = Field(default="")
    end_month: str = Field(default="")
    tasks: list[Task] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _normalize_id(cls, value: str) -> str:
        return value.strip().upper()
    

    