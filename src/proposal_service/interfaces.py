"""
Abstract contracts and typed containers for the three-stage pipeline.

Each stage is independently runnable:

* Stage 1 — :class:`TableParser` turns a PDF into :class:`ProposalTables`.
* Stage 2 — :class:`TaskExtractor` turns a PDF into :class:`ProposalTasks`.
* Stage 3 — :class:`Assembler` merges both into :class:`list[WorkPackage]`.

To plug in a new backend (different PDF library, different LLM provider,
different output shape), subclass the relevant ABC. No other file needs
to change.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from proposal_service.models import WorkPackage


# Typed result containers
@dataclass
class ProposalTables:
    """Output of Stage 1.

    Attributes:
        wp_info: ``{wp_id: {title, leader, pms, start, end}}`` from the WP list table.
        effort: ``{wp_id: person_months_for_target_company}`` from the effort table.
        raw_delivs: List of raw deliverable dicts as produced by the table parser.
        warnings: Soft errors collected during parsing (missing tables, etc.).
    """

    wp_info: dict[str, dict[str, Any]]
    effort: dict[str, float]
    raw_delivs: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wp_info": self.wp_info,
            "effort": self.effort,
            "raw_delivs": self.raw_delivs,
            "warnings": self.warnings,
        }

    def save(self, path: str | Path) -> None:
        """Persist this result to a JSON checkpoint."""
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "ProposalTables":
        """Rehydrate a result previously written by :meth:`save`."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            wp_info=data["wp_info"],
            effort=data["effort"],
            raw_delivs=data["raw_delivs"],
            warnings=data.get("warnings", []),
        )


@dataclass
class ProposalTasks:
    """Output of Stage 2.

    Attributes:
        raw_tasks: Validated task dicts produced by the LLM.
        failed_wps: WP IDs (with error messages) whose extraction failed.
    """

    raw_tasks: list[dict[str, Any]]
    failed_wps: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"raw_tasks": self.raw_tasks, "failed_wps": self.failed_wps}

    def save(self, path: str | Path) -> None:
        """Persist this result to a JSON checkpoint."""
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "ProposalTasks":
        """Rehydrate a result previously written by :meth:`save`."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            raw_tasks=data["raw_tasks"],
            failed_wps=data.get("failed_wps", []),
        )


# Abstract stage interfaces
class TableParser(ABC):
    """Stage 1: extract structured tables from the PDF."""

    @abstractmethod
    def parse(self, pdf_path: str) -> ProposalTables:
        """Read ``pdf_path``, locate Section 3, and return parsed tables."""
        ...


class TaskExtractor(ABC):
    """Stage 2: extract tasks from prose WP sections."""

    @abstractmethod
    def extract(self, pdf_path: str) -> ProposalTasks:
        """Read ``pdf_path``, locate WP task sections, and extract tasks."""
        ...


class Assembler(ABC):
    """Stage 3: merge Stage 1 and Stage 2 into the final domain model."""

    @abstractmethod
    def assemble(
        self,
        stage1: ProposalTables,
        stage2: ProposalTasks,
    ) -> list[WorkPackage]:
        """Combine WP info, effort, tasks, and deliverables for the target company."""
        ...
