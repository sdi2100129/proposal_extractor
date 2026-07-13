"""
Stage runners and a convenience full-pipeline wrapper.

Each ``run_stageN`` function is independently callable and accepts an
abstract stage implementation, so backends can be swapped without touching
this module.

Checkpointing is opt-in: pass a ``checkpoint`` path to a stage to save its
output as JSON. Without it, no disk writes happen — important for the API
path, which works on temporary files.
"""

from __future__ import annotations

import logging

from proposal_service.interfaces import (
    Assembler,
    ProposalTables,
    ProposalTasks,
    TableParser,
    TaskExtractor,
)
from proposal_service.models import WorkPackage


logger = logging.getLogger(__name__)


def run_stage1(
    pdf_path: str,
    parser: TableParser,
    *,
    checkpoint: str | None = None,
) -> ProposalTables:
    """Run Stage 1 (table parsing).

    Args:
        pdf_path: Path to the input PDF.
        parser: A :class:`TableParser` implementation.
        checkpoint: Optional path to persist the result as JSON.

    Returns:
        The parsed tables.
    """
    logger.info("[1/3] Parsing structured tables from: %s", pdf_path)
    result = parser.parse(pdf_path)
    for warning in result.warnings:
        logger.warning(warning)
    if checkpoint:
        result.save(checkpoint)
        logger.info("Stage 1 checkpoint saved: %s", checkpoint)
    return result


def run_stage2(
    pdf_path: str,
    extractor: TaskExtractor,
    *,
    checkpoint: str | None = None,
) -> ProposalTasks:
    """Run Stage 2 (task extraction).

    Args:
        pdf_path: Path to the input PDF.
        extractor: A :class:`TaskExtractor` implementation.
        checkpoint: Optional path to persist the result as JSON.

    Returns:
        The extracted tasks.
    """
    logger.info("[2/3] Extracting tasks from WP sections")
    result = extractor.extract(pdf_path)
    if checkpoint:
        result.save(checkpoint)
        logger.info("Stage 2 checkpoint saved: %s", checkpoint)
    return result


def run_stage3(
    assembler: Assembler,
    stage1: ProposalTables,
    stage2: ProposalTasks,
    *,
    out_path: str | None = None,
) -> list[WorkPackage]:
    """Run Stage 3 (assembly).

    Args:
        assembler: A :class:`Assembler` implementation.
        stage1: Result from Stage 1.
        stage2: Result from Stage 2.
        out_path: Optional JSON output path. The API path leaves this
            unset so no temp-file write happens; the CLI sets it to
            produce the final artifact.

    Returns:
        Assembled work packages.
    """
    logger.info("[3/3] Assembling final data model")
    work_packages = assembler.assemble(stage1, stage2)

    if out_path:
        import json
        from pathlib import Path

        Path(out_path).write_text(
            json.dumps(
                [wp.model_dump() for wp in work_packages],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        logger.info("Saved %s", out_path)

    return work_packages


def run(
    pdf_path: str,
    parser: TableParser,
    extractor: TaskExtractor,
    assembler: Assembler,
    *,
    out_path: str | None = None,
    stage1_checkpoint: str | None = None,
    stage2_checkpoint: str | None = None,
) -> list[WorkPackage]:
    """Run all three stages end-to-end.

    Args:
        pdf_path: Input PDF path.
        parser: Stage 1 implementation.
        extractor: Stage 2 implementation.
        assembler: Stage 3 implementation.
        out_path: Optional final JSON output path. Defaults to no disk write.
        stage1_checkpoint: Optional Stage 1 checkpoint path.
        stage2_checkpoint: Optional Stage 2 checkpoint path.

    Returns:
        Assembled work packages.
    """
    stage1 = run_stage1(pdf_path, parser, checkpoint=stage1_checkpoint)
    stage2 = run_stage2(pdf_path, extractor, checkpoint=stage2_checkpoint)
    return run_stage3(assembler, stage1, stage2, out_path=out_path)
