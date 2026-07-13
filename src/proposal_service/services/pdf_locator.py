"""Locating Section 3, its tables, and WP task sections inside a PDF.

This module wraps pdfplumber-level scanning and docling-based fallbacks
for rotated tables. All extraction routines return plain Python data; the
service layer is responsible for turning them into typed models.
"""

from __future__ import annotations

from cProfile import label
import logging
import re
from typing import Any

import pdfplumber

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

from proposal_service.services.task_extractor import (
    classify_table_via_llm,
    classify_toc_or_not_via_llm,
)
from proposal_service.utils import clean


logger = logging.getLogger(__name__)


# Regexes used across the module
WP_DESC_TABLE_RE = re.compile(
    r"""
    (?:
        work\s+packages?\s+(?:description|descriptions|desc\.?|descr\.?)
        |
        3\.1b\s+work\s+packages?\s+(?:description|descriptions|desc\.?|descr\.?)
        |
        work\s+packages?\s+desc
    )
    """,
    re.I | re.X
)


#   \bWP -> Word boundary + literal WP
#   BRANCH 1: WP\s*Number\s*:?\s*(?:WP\s*)?(\d+)   I.E. WP Number WP2 or WP Number: 2 or WP Number 2
#   BRANCH 2: WP\s*(\d+)                          I.E. WP2 (without "Number")
#   \s*[-–:]? -> optional whitespace + optional separator (dash or colon)
#   \s+(.+?) -> whitespace + non-greedy capture group for WP title
#   STOP title capture when next thing is:
#   \s+Objectives
#   \s+T\d+\.\d+
#   or end of string.
WP_HEADER_RE = re.compile(
    r"\bWP\s*#?\s*(?:Number\s*:?\s*(?:WP\s*)?)?(?P<num>\d+)\b"
    r"\s*[-–:]?\s*"
    r"(?P<title>[A-Z][^\n]{3,220}?)"
    r"(?=\s+Objectives:|\s+T\d+\.\d+|\s+Notes:|$)",
    re.I | re.S
)
    

#   (?=...) positive lookahead to split before the next WP header, which can be in various formats:
#   ALTERNATIVE 1: WP\s*Number\s*:?\s*(?:WP\s*)?\d+\s*[-–:]?   i.e. WP Number WP2: or WP Number: 2 - or WP2
#   ALTERNATIVE 2: WP\s*\d+\b\s*(?:[-–:]|\s+[A-Z][A-Za-z].{0,150}?\bObjectives:) i.e. WP2 - Title or WP2: Title or WP2 Title Objectives:
WP_HEADER_SPLIT_RE = re.compile(
    r"(?="
    r"\bWP\s*#?\s*(?:Number\s*:?\s*(?:WP\s*)?)?\d+\b"
    r"\s*[-–:]?\s*"
    r"(?:Title\s+)?"
    r"[A-Z][^\n]{3,220}?"
    r"(?=\s+Objectives:|\s+T\d+\.\d+|\s+Notes:)"
    r")",
    re.I | re.S
)


TASK_RE = re.compile(r"\bT\d+\.\d+\b", re.I)

STOP_RE = re.compile(
    r"(List of deliverables|List of milestones|Critical risks|Capacity of participants|Associated partners)",
    re.I,
)

RE_SEC3 = re.compile(
    r"\b3(?:\.|\s)\s*(QUALITY|IMPLEMENTATION|WORK\s*PLAN|EFFICIENCY)",
    re.I,
)

RE_SEC4 = re.compile(
    r"\b4(\.\s)\s*(ETHICS|BUDGET|ANNEX|FINANCIAL)",
    re.I,
)



# Caption of a staff-effort / person-month summary table. If this text is on a
# page but no usable table came out, the matrix is almost certainly a pasted
# image (no text layer) — the one case worth spending OCR on.
EFFORT_CAPTION_RE = re.compile(
    r"(summary\s+of\s+(?:staff\s+)?effort|staff\s+effort)",
    re.I,
)


# Cache of docling-parsed PDFs so the heavy conversion runs at most once per
# request. Keyed on (pdf_path, page_range) — a request only ever converts a
# single Section 3 window, so this still resolves to one conversion per file.
_DOCLING_CACHE: dict[tuple[str, tuple[int, int] | None, bool], Any] = {}


def find_task_sections(pdf: Any) -> list[tuple[str, str, list[int], str]]:
    """
    Scans the whole PDF and builds structured chunks per Work Package.

    Args:
        pdf: An open ``pdfplumber.PDF`` instance.

    Returns:
        A list of ``(wp_id, wp_title, page_indexes, section_text)`` tuples,
        one per detected work package.
    """

    #   Track the current WP
    sections: list[tuple[str, str, list[int], str]] = []
    current_wp_id: str | None = None
    current_wp_title = ""
    current_pages: list[int] = []
    current_text: list[str] = []


    in_wp_description_zone = False

    #   Helper function that finalizes the current WP and saves it.
    def flush() -> None:
        #   Modify variables from the outer function find_task_sections (not local ones)
        nonlocal current_wp_id, current_wp_title, current_pages, current_text
        
    
        #   Only save if we have a valid WP ID and some text collected
        section_text = "\n".join(current_text).strip()
        if current_wp_id and section_text and TASK_RE.search(section_text):
            sections.append(
                (current_wp_id, current_wp_title, current_pages[:], section_text)
            )

        #   Reset state for the next WP
        current_wp_id = None
        current_wp_title = ""
        current_pages = []
        current_text = []


    #   Iterate through all pages, looking for WP sections and collecting their text.
    for i, page in enumerate(pdf.pages):
        text = clean(page.extract_text() or "")
        if not text:
            continue

        logger.debug("page %d chars=%d", i + 1, len(text))
        
        #DELETE  LATER
        logger.debug(text[:1200])
        for table_idx, table in enumerate(page.extract_tables() or [], start=1):
            if not table:
                continue

            logger.debug(f"\n  [table {table_idx}] rows={len(table)}")
            for r_idx, row in enumerate(table[:8], start=1):
                cleaned_row = [clean(c) for c in row]
                logger.debug(f"    row {r_idx}: {cleaned_row}")


        if WP_DESC_TABLE_RE.search(text):
            in_wp_description_zone = True
            logger.debug("WP description zone starts on page %d", i + 1)

        if not in_wp_description_zone:
            continue

        # Pages that look like deliverable or risk descriptions can also
        # contain WP-like patterns; skip them.
        upper_text = text.upper()
        if "DEL. NO" in upper_text or "DESCRIPTION OF RISK" in upper_text:
            continue

        stop_match = STOP_RE.search(text)
        if stop_match:
            text = text[: stop_match.start()]
            should_stop_after_page = True
        else:
            should_stop_after_page = False

        segments = WP_HEADER_SPLIT_RE.split(text)


        # If no WP header on this page, segments = [full text]
        # If one header, segments = [text before header, text from header onward]
        # If two headers, segments = [before, from WP_n, from WP_n+1]
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue

            seg_upper = seg.upper()
            if any(
                x in seg_upper
                for x in (
                    "LIST OF DELIVERABLES",
                    "CRITICAL RISKS",
                    "3.1.3 ASSOCIATED PARTNERS",
                    "3.2 CAPACITY OF PARTICIPANTS",
                )
            ):
                logger.debug("Flushing %s on page %d (stop keyword)", current_wp_id, i + 1)
                flush()
                continue

            # Reject inline range references like "WP3–WP5 for revision..."
            if re.match(r"WP\s*\d+\s*[-–]\s*WP\s*\d+", seg, re.I):
                if current_wp_id:
                    current_text.append(seg)
                continue

            #   Detect the start of a WP section by looking for "WP Number WPn" and presence of task patterns like "T1.1".
            #wp_match = re.search(r"WP [Nn]umber\s+(?:WP)?(\d+)", seg, re.I)
            wp_match = WP_HEADER_RE.search(seg)
            has_task = bool(TASK_RE.search(seg))

            if wp_match:
                title = clean(wp_match.group("title"))
                wp_num = wp_match.group("num")

                # Reject inline references like "WP2 for revision..."
                if re.match(r"^(for|from|and|to|in|with|of|technical|work)\b", title, re.I):
                    if current_wp_id:
                        current_text.append(seg)
                    continue


                # Accept if Objectives/tasks present, OR if title follows the
                # "WP Title <name>" convention (objectives may start on next page).
                is_real_wp_header = (
                    re.search(r"\bObjectives\b|\bT\d+\.\d+\b", seg, re.I)
                    or re.match(r"^WP\s+Title\b", title, re.I)
                )
                if not is_real_wp_header:
                    if current_wp_id:
                        current_text.append(seg)
                    continue


                #   If the start of a new WP section is found save previous WP
                flush()
                current_wp_id = f"WP{wp_num}"
                current_wp_title = title
                current_pages = [i]
                current_text = [seg]
                logger.debug("Found %s on page %d", current_wp_id, i + 1)

            elif current_wp_id:
                current_pages.append(i)
                current_text.append(seg)

        # only now stop after this page
        if should_stop_after_page:
            logger.debug("WP descriptions end on page %d", i + 1)
            flush()
            break

    flush()
    return sections


#   Strips spaces, compresses multiple spaces and uppercases everything
def _norm_cell(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def _merge_header_rows(rows: list[list[str]]) -> list[str]:
    if len(rows) < 2:
        return [_norm_cell(c) for c in rows[0]] if rows else []
    
    max_cols = max(len(rows[0]), len(rows[1]))
    merged: list[str] = []
    for i in range(max_cols):
        a = _norm_cell(rows[0][i]) if i < len(rows[0]) and rows[0][i] else ""
        b = _norm_cell(rows[1][i]) if i < len(rows[1]) and rows[1][i] else ""
        merged.append(" ".join(x for x in (a, b) if x).strip())
    return merged


def classify_table_by_rules(table_rows: list[list[str]], context: str = "") -> str | None:
    """Classify a table using deterministic rules; return ``None`` if unsure.

    The service layer calls the LLM only when this function returns ``None``.
    Emits a per-table signal trace at DEBUG so mislabels are explainable.
    """

    #   Normalize cells and remove empty rows
    rows = [[clean(c) for c in row] for row in table_rows if row]
    if not rows:
        logger.debug("%s -> None (no rows after cleaning)", context)
        return None

    #   Collect all non-empty cells in uppercase for easier pattern matching
    flat_upper = [_norm_cell(c) for row in rows for c in row if c]
    
    first_row_upper = [_norm_cell(c) for c in rows[0] if c]
    header_text = " | ".join(first_row_upper)
    merged_header_cells = _merge_header_rows(rows[:2])
    combined_header = " | ".join(merged_header_cells)
    first_col = [_norm_cell(row[0]) for row in rows if row and row[0]]

    # One line that explains *every* downstream decision for this table.
    logger.debug(
        "%s rows=%d | combined_header=%r | first_col=%s",
        context, len(rows), combined_header[:220], first_col[:8],
    )

    # WP-description / figure prose that pdfplumber captured as a "table":
    # one or more enormous free-text cells, not tabular data.
    longest_cell = max((len(c) for row in rows for c in row if c), default=0)
    has_deliv_ids = any(re.match(r"^D\d+\.\d+", c.strip()) for c in first_col)
    if longest_cell > 400 and not has_deliv_ids:
        logger.debug("%s -> other (prose blob, longest cell=%d chars)", context, longest_cell)
        return "other"

    # Milestones / other
    ms_cell = next((c for c in flat_upper if re.search(r"\bMS\d+\b", c)), None)
    if ms_cell is not None:
        logger.debug("%s -> other (milestone id in cell %r)", context, ms_cell)
        return "other"
    if "MILESTONE" in header_text or "MEANS OF VERIFICATION" in combined_header:
        logger.debug("%s -> other (MILESTONE/MEANS OF VERIFICATION in header)", context)
        return "other"

    # WP body mini-table
    if any("WP NUMBER" in c for c in first_row_upper):
        logger.debug("%s -> other ('WP NUMBER' in first row = WP description block)", context)
        return "other"

    # WP list summary table
    has_wp_title = any(
        x in combined_header
        for x in ("WP TITLE", "WORK PACKAGE TITLE", "WORKPACKAGE TITLE")
    )

    has_lead = any(x in combined_header for x in ("LEADER", "LEAD", "SHORT NAME"))
    has_start = any(x in combined_header for x in ("START", "START MONTH"))
    has_end = any(x in combined_header for x in ("END", "END MONTH"))

    if has_wp_title and has_lead and has_start and has_end:
        wp_rows = 0
        for row in rows[1:]:
            first = _norm_cell(row[0]) if row and row[0] else ""
            row_text = " | ".join(_norm_cell(c) for c in row if c)
            if re.search(r"\bWP\s*\d+\b", row_text, re.I) or re.fullmatch(r"\d{1,2}", first):
                wp_rows += 1
        
        if wp_rows >= 2:
            logger.debug("%s -> wp_list (wp_title+lead+start+end, %d WP rows)", context, wp_rows)
            return "wp_list"
        logger.debug(
            "%s wp_list header matched but only %d WP rows (<2), continuing",
            context, wp_rows,
        )



    # WP list continuation page: Table 3.1a is often split across a page
    # break (e.g. interrupted by a Gantt chart), so the tail fragment has no
    # repeated header. Detect it by row shape instead: a WP id (bare 1-2
    # digit or "WPn") plus a start/end month pair (M1, M18, ...) per row.
    # Mirrors the "strong_deliverable_body" continuation fallback below.
    wp_continuation_rows = 0
    for row in rows:
        cells = [c for c in row if c]
        if not cells:
            continue
        first = _norm_cell(row[0]) if row[0] else ""
        row_text = " | ".join(_norm_cell(c) for c in cells)
        has_wp_id = bool(
            re.fullmatch(r"\d{1,2}", first) or re.fullmatch(r"WP\s*\d+", first, re.I)
        )
        has_month_pair = len(re.findall(r"\bM\d{1,2}\b", row_text, re.I)) >= 2
        if has_wp_id and has_month_pair:
            wp_continuation_rows += 1

    if wp_continuation_rows >= 2:
        logger.debug(
            "%s -> wp_list (continuation page, %d WP-like rows, no header)",
            context, wp_continuation_rows,
        )
        return "wp_list"
    

    # Deliverables
    has_deliverable_header = (
        "DELIVERABLE" in combined_header
        or "DEL." in combined_header
        or "DISS" in combined_header
        or "TYPE" in combined_header
        or "DELIV" in combined_header
    )

    deliverable_id_cells = [
            c for c in first_col
            if re.match(r"^D\d+\.\d+\b", c.strip()) and len(c.strip()) <= 12
        ]
    has_deliverable_ids_in_first_col = len(deliverable_id_cells) >= 2

    has_diss_codes = any(c in {"PU", "CO", "SEN", "RE"} for c in flat_upper)
    has_due_months = any(re.search(r"\bM\d+\b", c) for c in flat_upper)
    
    strong_deliverable_body = has_deliverable_ids_in_first_col and (
        has_diss_codes or has_due_months
    )

    logger.debug(
        "%s deliverable signals | header=%s ids_in_col0=%s diss_codes=%s due_M=%s strong_body=%s",
        context, has_deliverable_header, has_deliverable_ids_in_first_col,
        has_diss_codes, has_due_months, strong_deliverable_body,
    )

    if has_deliverable_header and (
        has_deliverable_ids_in_first_col or (has_diss_codes and has_due_months)
    ):
        logger.debug("%s -> deliverable (header + ids/codes)", context)
        return "deliverable"
    
    # continuation-page fallback
    if strong_deliverable_body:
        logger.debug("%s -> deliverable (continuation page, strong body, no header)", context)
        return "deliverable"


    # Effort matrix
    looks_like_wp_summary_header = (
        "WORK PACKAGE TITLE" in combined_header
        or "WP TITLE" in combined_header
        or ("START" in combined_header and "END" in combined_header and "LEAD" in combined_header)
    )

    if len(rows) >= 3 and not looks_like_wp_summary_header:
        header_cells = merged_header_cells or [_norm_cell(c) for c in rows[0] if c]
        body_rows = rows[2:] if len(rows) > 2 else rows[1:]


        # Pattern A: partners in header, WPs in first col
        # Pattern B: partners in first col, WPs in header
        first_col_vals = [_norm_cell(row[0]) for row in body_rows if row and row[0]]
        
        has_partners_in_first_col = (
            sum(
                1
                for v in first_col_vals
                if re.fullmatch(r"[A-Z][A-Z&\-]{1,6}", v)
                and not re.match(r"^WP\s*\d+$", v)
            )
            >= 3
        )

        # Check for "Person-Months" or "PMs" anywhere in header — strong effort signal
        has_wp_in_first_col = (
            sum(1 for v in first_col_vals if re.match(r"^WP\s*\d+$", v)) >= 2
        )

        # Check for "Person-Months" or "PMs" anywhere in header — strong effort signal
        has_pm_header = any(
            "PERSON" in c or "PM" in c or "PMS" in c or "MONTH" in c
            for c in header_cells
        )

        # Structural signal: header dominated by WP1..WPn columns. Computed
        # from the raw first row (not header_cells/merged_header_cells,
        # which blindly concatenate rows[0]+rows[1] even when row[1] is
        # actually the first *data* row for a single-header-row table like
        # this one — that contamination would break a fullmatch check).
        # Independent of the "PM" keyword (absent when the header just says
        # "Total") and of how the partner cell combines id + name.
        raw_header_row = [_norm_cell(c) for c in rows[0] if c]
        wp_col_count = sum(1 for c in raw_header_row if re.fullmatch(r"WP\s*\d+", c, re.I))
        strong_wp_header = wp_col_count >= 3


        # Check acronyms in header (original logic, relaxed)
        acronym_count = 0
        for c in header_cells:
            if "TOTAL" in c or "PM" in c or "PMS" in c:
                acronym_count += 1
            elif re.fullmatch(r"[A-Z][A-Z&\-]{1,6}", c):
                acronym_count += 1
        
        long_text_in_header = sum(len(c.split()) >= 4 for c in header_cells)


        # Count numeric body rows
        numeric_rows = 0
        for row in body_rows:
            #   Removes empty cells
            vals = [c.strip() for c in row if c and c.strip()]
            if not vals:
                continue
            numericish = sum(
                1
                for c in vals
                if re.fullmatch(r"\d+(\.\d+)?", c)
                or re.fullmatch(r"\d+\s+\d+(\.\d+)?", c)
            )
            if numericish >= max(2, len(vals) // 3):
                numeric_rows += 1

        is_effort = (
            numeric_rows >= 2
            and long_text_in_header == 0
            and (
                has_pm_header
                or acronym_count >= max(4, len(header_cells) // 2)
                or (has_partners_in_first_col and has_wp_in_first_col)
                or (has_partners_in_first_col and has_wp_in_first_col)
                or strong_wp_header
            )
        )

        # Also catch: partners in first col, WPs in second header row, numeric body
        if not is_effort and has_pm_header and numeric_rows >= 2:
            is_effort = True

        logger.debug(
            "%s effort signals | numeric_rows=%d pm_header=%s acronyms=%d "
            "partners_col0=%s wp_col0=%s long_text_header=%d strong_wp_header=%s -> is_effort=%s",
            context, numeric_rows, has_pm_header, acronym_count,
            has_partners_in_first_col, has_wp_in_first_col, long_text_in_header,
            strong_wp_header, is_effort,
        )

        if is_effort:
            logger.debug("%s -> effort", context)
            return "effort"

    logger.debug("%s -> None (no rule matched; LLM fallback)", context)
    return None


def is_definite_toc(text: str) -> bool:
    """True when the page is unambiguously a table of contents."""
    dotted_lines = [line for line in text.splitlines() if re.search(r"\.{4,}\s*\d+", line)]
    
    # If more than 5 lines have dotted leaders, it's a TOC — no ambiguity    
    return len(dotted_lines) >= 5


def is_definite_section3(text: str) -> bool:
    """True when the page is unambiguously Section 3 body."""
    has_sec3_header = bool(re.search(r"Section\s+3\s*[–-]", text))
    has_sec3_heading = bool(
        re.search(
            r"(?:^|\n)\s*3[.\s]\s*(QUALITY|IMPLEMENTATION|WORK\s*PLAN|EFFICIENCY)",
            text,
            re.I | re.MULTILINE,
        )
    )
    long_lines = [line for line in text.splitlines() if len(line.strip()) > 80]
    has_prose = len(long_lines) >= 3
    no_dotted = not re.search(r"\.{4,}\s*\d+", text)
    return (has_sec3_header or has_sec3_heading) and has_prose and no_dotted


def find_section3_pages(pdf: Any) -> list[int]:
    """
    Hybrid approach:
    1. Use regex to find candidate Section 3 mentions
    2. Use LLM only on those candidate pages
    3. Start at the first page classified as section3_start
    4. Stop at Section 4
    """

    section3_pages: list[int] = []
    start_idx: int | None = None

    # Pass 1: find first true Section 3 start
    for i, page in enumerate(pdf.pages):
        raw_text = page.extract_text() or ""

        #   Skip pages that don’t even mention Section 3.
        if not RE_SEC3.search(raw_text):
            continue

        #   Python decides first, no LLM call needed 
        if is_definite_toc(raw_text):
            logger.debug("page %d: toc_or_index (definite)", i + 1)
            continue

        if is_definite_section3(raw_text):
            logger.debug("page %d: section3_start (definite)", i + 1)
            start_idx = i
            break

        label = classify_toc_or_not_via_llm(raw_text, page_num=i + 1)
        logger.debug("page %d: %s (LLM)", i + 1, label)

        # Skip TOC/index pages entirely — keep scanning
        if label == "toc_or_index":
            continue

        if label in ("section3_start", "section3_body") and start_idx is None:
            start_idx = i
            break

    if start_idx is None:
        return []


    # Pass 2: collect pages until Section 4
    for j in range(start_idx, len(pdf.pages)):
        raw_text = pdf.pages[j].extract_text() or ""
        text = clean(raw_text)
        
        if j > start_idx and RE_SEC4.search(text):
            break

        section3_pages.append(j)

    return section3_pages


def _build_docling_converter(do_ocr: bool = False) -> DocumentConverter:
    """Build a DocumentConverter, OCR off by default.

    These are born-digital proposals: pdfplumber reads the surrounding text
    and tables fine, and docling is only used here to recover *table
    structure* on the few pages where pdfplumber's gridline detection fails.
    OCR contributes nothing to that and is by far the most expensive stage
    (per-batch rapidocr spikes of 1-2 min on a full 187-page render), so it
    is off by default. The one exception is a page whose table is an
    embedded *image* (e.g. an effort matrix pasted as a screenshot): it has
    no text layer, so OCR is the only way to read it — callers opt in with
    ``do_ocr=True`` for that page alone.
    """
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = do_ocr
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )


def get_docling_doc(
    pdf_path: str,
    page_range: tuple[int, int] | None = None,
    do_ocr: bool = False,
) -> Any:
    """Return a memoized docling-parsed document for ``pdf_path``.

    ``page_range`` is a 1-indexed, inclusive ``(first, last)`` span. When
    given, only those pages are converted — docling keeps the original
    1-indexed page numbers in ``table.prov[0].page_no``, so downstream page
    filtering is unaffected. Restricting the range is the single biggest cost
    lever here (a ~14-page Section 3 window vs. 187 pages).
    """
    cache_key = (pdf_path, page_range, do_ocr)
    doc = _DOCLING_CACHE.get(cache_key)
    if doc is None:
        converter = _build_docling_converter(do_ocr=do_ocr)
        if page_range is None:
            doc = converter.convert(pdf_path).document
        else:
            doc = converter.convert(pdf_path, page_range=page_range).document
        _DOCLING_CACHE[cache_key] = doc
    return doc


def is_rotated_text_table(table: list[list[Any]]) -> bool:
    """Heuristically detect whether a table contains rotated/stacked text."""
    if not table:
        return False

    def looks_rotated(cell: str) -> bool:
        lines = cell.splitlines()
        if len(lines) < 3:
            return False
        short_lines = sum(1 for line in lines if len(line.strip()) <= 2)
        return (short_lines / len(lines)) >= 0.6

    # Check first row specifically — rotated headers are the clearest signal
    first_row_cells = [str(c).strip() for c in table[0] if c is not None and str(c).strip()]
    if first_row_cells:
        rotated_in_header = sum(1 for c in first_row_cells if looks_rotated(c))
        if (rotated_in_header / len(first_row_cells)) >= 0.3:
            return True

    # Fallback: check all cells with lower threshold
    flat_cells = [
        str(c).strip()
        for row in table
        for c in row
        if c is not None and str(c).strip()
    ]
    if not flat_cells:
        return False
    rotated = sum(1 for c in flat_cells if looks_rotated(c))
    return (rotated / len(flat_cells)) >= 0.2

def _page_has_large_image(page: Any, min_area_frac: float = 0.06) -> bool:
    """True if the page carries a raster image covering a meaningful fraction
    of its area — the tell-tale of a table pasted in as a screenshot, which
    has no text layer for pdfplumber or OCR-less docling to read.

    The 0.15 floor is well above any logo/letterhead (those are a few percent)
    and below a real embedded table screenshot; tune if a future format sits
    between the two.
    """
    try:
        images = page.images or []
        page_area = float(page.width) * float(page.height)
    except Exception:
        return False
    if not images or page_area <= 0:
        return False
    for img in images:
        w = float(img.get("width") or 0)
        h = float(img.get("height") or 0)
        if (w * h) / page_area >= min_area_frac:
            return True
    return False


def _safe_docling_fallback(
    pdf_path: str,
    page_idx: int,
    fallback_tables: list[list[list[str]]],
    page_range: tuple[int, int] | None = None,
    do_ocr: bool = False,
) -> list[list[list[str]]]:
    """Run the docling fallback without letting it take down the whole request.

    docling/OCR failures (missing model weights, permission errors, OOM on a
    malformed page render, etc.) are environment problems, not signals that
    this page has no table — losing every other already-extracted page over
    one bad page is worse than just skipping docling for this one.
    """
    try:
        return extract_tables_via_docling(pdf_path, page_idx, page_range=page_range, do_ocr=do_ocr)
    except Exception:
        logger.warning(
            "docling fallback failed on page %d — falling back to whatever "
            "pdfplumber found (%d table(s))",
            page_idx + 1, len(fallback_tables or []),
            exc_info=True,
        )
        return fallback_tables
    
    

def extract_tables_via_docling(
    pdf_path: str,
    page_idx: int,
    page_range: tuple[int, int] | None = None,
    do_ocr: bool = False,
) -> list[list[list[str]]]:
    """Fallback table extraction for pages where pdfplumber fails."""
    doc = get_docling_doc(pdf_path, page_range=page_range, do_ocr=do_ocr)
    page_tables: list[list[list[str]]] = []
    for table in doc.tables:
        #   Docling's page numbers are 1-indexed, pdfplumber's are 0-indexed
        if table.prov[0].page_no != page_idx + 1:
            continue

        #   Convert table into pandas dataframe
        df = table.export_to_dataframe()
        rows = [df.columns.tolist()] + df.values.tolist()
        
        #   Normalize cells to strings and handle None values
        page_tables.append([[str(c) if c is not None else "" for c in row] for row in rows])
    
    return page_tables


def find_all_tables(
    pdf: Any,
    page_indices: list[int],
    pdf_path: str | None = None,
) -> dict[str, Any]:
    """Scan pages for tables and classify each via rules + LLM fallback.

    Returns {
        'wp_list': (page_idx, table), to 'wp_list': [(page_idx, table), ...]  # may span pages
        'effort':     (page_idx, table),
        'deliverable': (page_idx, [table, table, ...])  # may span pages
    }
    """
    found: dict[str, Any] = {"wp_list": [], "effort": None, "deliverable": []}
    effort_source: str | None = None
    wp_list_first_page: int | None = None


    # Scope any docling fallback to the Section 3 window (1-indexed, inclusive)
    # instead of converting the whole document. page_indices is contiguous
    # (find_section3_pages appends a range), so min/max bounds the span.
    docling_range: tuple[int, int] | None = (
        (min(page_indices) + 1, max(page_indices) + 1) if page_indices else None
    )


    for i in page_indices:
            page = pdf.pages[i]
            tables = page.extract_tables()
            page_text = page.extract_text() or ""

            def _has_usable_table(tbls: list[list[list[Any]]] | None) -> bool:
                # Same bar the classify-skip step applies later
                # (max_real_cols < 3 => dropped before classify). A page
                # whose only pdfplumber output is a sparse 1-2 column
                # artifact (common when a table is actually an embedded
                # image and pdfplumber picks up faint gridlines/shading)
                # is functionally the same as finding nothing — it should
                # fall through to docling too, not just a truly empty list.
                for t in tbls or []:
                    if not t or len(t) < 2:
                        continue
                    max_real_cols = max(
                        sum(1 for c in row if c is not None and str(c).strip())
                        for row in t
                    )
                    if max_real_cols >= 3:
                        return True
                return False

            if pdf_path and tables and any(is_rotated_text_table(t) for t in tables if t):
                logger.debug("docling fallback on page %d (rotated text)", i + 1)
                tables = extract_tables_via_docling(pdf_path, i, page_range=docling_range)
            elif (
                pdf_path
                and found["effort"] is None
                and not _has_usable_table(tables)
                and _page_has_large_image(page)
                and (EFFORT_CAPTION_RE.search(page_text) or _page_has_large_image(page))
            ):
                # Effort matrix pasted as an image — no text layer, so OCR is
                # the only path. Scope OCR to this one page instead of the
                # whole window, and only bother while the effort table is
                # still missing.
                logger.debug("docling fallback on page %d (embedded image, OCR on)", i + 1)
                logger.debug(
                    "docling fallback on page %d (effort caption/image, OCR on)", i + 1
                )
                tables = _safe_docling_fallback(
                    pdf_path, i, tables, page_range=(i + 1, i + 1), do_ocr=True
                )
            elif pdf_path and not _has_usable_table(tables):
                logger.debug("docling fallback on page %d (no usable pdfplumber tables)", i + 1)
                tables = _safe_docling_fallback(pdf_path, i, tables, page_range=docling_range)

            logger.debug("page %d: %d raw table(s) extracted", i + 1, len(tables or []))

            for t_idx, table in enumerate(tables or [], start=1):
                n_rows = len(table) if table else 0
                if not table or n_rows < 2:
                    logger.debug(
                        "page %d table %d SKIPPED: %d row(s) (<2) — dropped before classify",
                        i + 1, t_idx, n_rows,
                    )
                    continue

                max_real_cols = max(
                    sum(1 for c in row if c is not None and str(c).strip())
                    for row in table
                )
                header_preview = [str(c).strip() if c else "" for c in table[0]]
                logger.debug(
                    "page %d table %d extracted: rows=%d max_cols=%d header=%s",
                    i + 1, t_idx, n_rows, max_real_cols, header_preview,
                )

                if max_real_cols < 3:
                    logger.debug(
                        "page %d table %d SKIPPED: %d populated column(s) (<3) — "
                        "dropped before classify",
                        i + 1, t_idx, max_real_cols,
                    )
                    continue

                context = f"page {i + 1} table {t_idx}"
                rule_label = classify_table_by_rules(table, context=context)
                label = rule_label if rule_label is not None else classify_table_via_llm(table)
                source = "rules" if rule_label is not None else "LLM"
                logger.debug(
                    "page %d table %d classified as %s (via %s)",
                    i + 1, t_idx, label, source,
                )

                if label == "wp_list":
                    # Guard against a far-away table being swallowed into the
                    # WP list (e.g. an effort matrix the LLM mislabels as
                    # wp_list many pages later). A genuine continuation
                    # fragment of Table 3.1a sits within a couple of pages of
                    # the first wp_list table found — it's only ever split by
                    # a single intervening Gantt-chart page.
                    if wp_list_first_page is None:
                        wp_list_first_page = i
                        found["wp_list"].append((i, table))
                    elif i - wp_list_first_page <= 3:
                        found["wp_list"].append((i, table))
                    else:
                        logger.debug(
                            "page %d table %d classified wp_list but %d "
                            "pages after the first wp_list table (page %d) "
                            "— treating as a misclassification, not a "
                            "continuation fragment",
                            i + 1, t_idx, i - wp_list_first_page,
                            wp_list_first_page + 1,
                        )
                elif label == "effort":
                    if found["effort"] is None or (source == "rules" and effort_source == "LLM"):
                        found["effort"] = (i, table); effort_source = source
                elif label == "deliverable":
                    found["deliverable"].append((i, table))

    return found


