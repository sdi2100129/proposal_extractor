"""Concrete implementations of the three pipeline stage contracts."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pdfplumber
from pdfplumber.utils.exceptions import PdfminerException

from proposal_service.config import get_settings
from proposal_service.interfaces import (
    Assembler,
    ProposalTables,
    ProposalTasks,
    TableParser,
    TaskExtractor,
)
from proposal_service.models import WorkPackage
from proposal_service.services.assembler import assemble as _assemble_logic
from proposal_service.services.pdf_locator import (
    find_all_tables,
    find_section3_pages,
    find_task_sections,
)
from proposal_service.services.table_parser import (
    merge_wp_list_fragments,
    parse_deliverables,
    parse_effort_table,
    parse_wp_list_table,
)
from proposal_service.services.task_extractor import (
    OllamaUnavailable,
    extract_tasks_via_llm,
    validate_task,
)
from typing import Callable


logger = logging.getLogger(__name__)


# Real task headings sit at the start of a line (most common) OR right after
# sentence-ending punctuation on the same line. They are followed by either:
#   - a separator (- – :) and an uppercase title word, or
#   - a parenthesis '(' (partner list), or
#   - a bracket '[' (month range).
# Inline references like "...presented to T4.3 and T4.5 on M8..." or
# "...affects T4.4 as it..." are rejected because the preceding char is a
# letter, digit, or space-following-lowercase-word.
TASK_HEADING_RE = re.compile(
    r"(?:^|(?<=\s))"                #   start of string or right after whitespace
    r"(?:Task\s+)?"                 #   optional "Task" prefix
    r"(?P<id>T(?P<wp>\d+)\.\d+)\b"  #   the task ID, capturing the WP number
    r"\s*"
    r"(?=[-–:]|[A-Z]|\(|\[)",       #   followed by a separator, an UPPERCASE title word, a partner list, or a month bracket
    # no re.I  
)


# Page footers that leak into extracted text and pollute partner lists.
PDF_FOOTER_RE = re.compile(
    r"^\s*CL\d+-\d+-[A-Z\-]+\d+\s+Part\s+B\s*-\s*Page\s+\d+\s+of\s+\d+.*$",
    re.M | re.I,
)


def _strip_pdf_footers(text: str) -> str:
    """Remove repeated page-footer lines that contaminate partner lists."""
    return PDF_FOOTER_RE.sub("", text)


def split_wp_into_task_chunks(wp_text: str, wp_id: str | None = None) -> list[str]:
    """Split a WP prose blob into one chunk per task heading."""

    # Strip out common PDF footer lines.
    wp_text = _strip_pdf_footers(wp_text)
    # Find all candidate task headings in the WP text.
    matches = list(TASK_HEADING_RE.finditer(wp_text))
    chunks = []

    expected_wp_num: str | None = None
    if wp_id:
        m = re.search(r"\d+", wp_id)
        expected_wp_num = m.group(0) if m else None

    for idx, m in enumerate(matches):
        task_wp_num = m.group("wp")

        # Reject T2.x references inside WP6, WP7, etc.
        if expected_wp_num and task_wp_num != expected_wp_num:
            continue

        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(wp_text)
        chunk = wp_text[start:end].strip()

        if not chunk:
            continue

        # Sanity-check: a real task chunk should have either a separator after
        # the ID followed by a title word, OR parenthesised partner list, OR
        # month brackets within the first ~120 chars. This filters out residual
        # mid-sentence matches that slipped through the regex.
        head = chunk[:200]
        looks_like_real_heading = bool(
            re.match(
                r"^(?:Task\s+)?T\d+\.\d+\s*(?:[-–:]\s*)?[A-Z(\[]",
                head,
                re.I,
            )
        )
        if not looks_like_real_heading:
            continue

        chunks.append(chunk)

    return chunks



# --- Deterministic heading parser -------------------------------------------
# id / months / leader / partners / title all live in the rigid heading line:
#   T2.2 <title> [M1 – M33] - Leader: EUT - Task Partners: NC, UWA - [PMs: 76] <prose...>
# Parsing them here keeps these fields off the LLM, which hallucinated them
# (M12 end-months, NCI/UCD partners copied from the prompt's schema example).
_ID_RE       = re.compile(r"^\s*(?:Task\s+)?(T(\d+)\.\d+)", re.I)


# First parenthesised group = the heading partner list. Titles are paren-free
# and this group precedes the prose, so the first (...) is the partner paren.
_HEADING_PAREN_RE = re.compile(r"\(([^()]*)\)")


# Months in a bracket: [M1-M36] (KnoWare) or multi-range
# [M01-M10, M25-M36, M49-M53] / [M01-M30, M49, M60] (AGROBOOST).
# Months inside a HyperImage heading paren, after the "|": "M1-M4" / "M5-18".
_BRACKET_MONTHS_RE = re.compile(r"\[\s*[M\u039c]\d+\s*[-–][^\]]*\]", re.I)
_M_NUM_RE = re.compile(r"[M\u039c](\d+)", re.I)
_PIPE_MONTHS_RE = re.compile(r"[M\u039c]?(\d+)\s*[-–]\s*[M\u039c]?(\d+)", re.I)

# Explicit labels for the rigid ARTEMIS-style heading (no partner parens):
#   ... - Leader: EUT - Task Partners: NC, UWA - [PMs: 76] ...
_PARTNERS_LABEL_RE = re.compile(
    r"Task\s+Partners?\s*:\s*(.*?)(?:\s*[-–]?\s*\[?\s*PMs?\s*:|\Z)", re.I | re.S
)
_LEADER_LABEL_RE = re.compile(
    r"Leader\s*:\s*([A-Za-z]+(?:[&\-](?![Tt]ask\b)[A-Za-z]+)*)", re.I
)


# PMs value is only digits/commas/dots/space, never prose
_PMS_RE = re.compile(r"\[?\s*PMs?\s*:\s*[\d.,\s]*\]?", re.I)

# One acronym token: allows FHG-IWS, 4KMEMS, G&D, and one internal space
# (KnoWare's "MEWS L"). Must carry an uppercase letter, which drops prose
# words like "use cases".
_PARTNER_TOK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9&.\-]*(?:\s[A-Za-z0-9]+)?")


def _parse_partners(raw: str) -> list[str]:
    """Split a comma-separated acronym list, keeping 'ALL'/'All Partners'."""
    if not raw:
        return []
    raw = re.sub(r"\s+", " ", raw).strip(" ,;–-")
    out: list[str] = []
    for tok in raw.split(","):
        p = tok.strip()
        if not p:
            continue
        if re.match(r"^all\b", p, re.I):                 # "ALL" / "All Partners"
            out.append(p)
        elif len(p) <= 12 and _PARTNER_TOK_RE.fullmatch(p) and re.search(r"[A-Z]", p):
            out.append(p)
    return out


def _split_partner_paren(inner: str) -> tuple[str, list[str], str]:
    """Parse a heading-paren body into (leader, partners, pipe_months_str).

    HyperImage : "P1, P2, ... | M1-M4"  -> partners + months
    KnoWare    : "LEADER; P1, P2, ..."  -> leader + partners
    AGROBOOST  : "P1, P2, ..."          -> partners
    """
    months = ""
    if "|" in inner:
        inner, _, months = inner.partition("|")
    leader = ""
    if ";" in inner:
        lead, _, rest = inner.partition(";")
        leader, inner = lead.strip(), rest
    return leader, _parse_partners(inner), months


def parse_heading_and_body(chunk: str) -> tuple[dict, str] | None:
    """Split a task chunk into structured heading fields and prose body.

    Handles four heading grammars: AGROBOOST '(P, ...)', HyperImage
    '(P, ... | M1-M4)', KnoWare '(LEADER; P, ...)', and the rigid
    'Leader: X - Task Partners: ...' label style. Returns None if the chunk
    doesn't start with a task ID.
    """
    c = re.sub(r"\s+", " ", chunk).strip()
    idm = _ID_RE.match(c)
    if not idm:
        return None

    # Partners/leader: an explicit "Task Partners:" label wins (rigid style);
    # otherwise the first paren is the heading partner list. Label-first keeps
    # a prose paren from being mistaken for partners.
    paren = _HEADING_PAREN_RE.search(c)
    plabel = _PARTNERS_LABEL_RE.search(c)
    leader, partners, pipe_months = "", [], ""
    partner_paren = None
    if plabel:
        partners = _parse_partners(plabel.group(1))
    elif paren:
        partner_paren = paren
        leader, partners, pipe_months = _split_partner_paren(paren.group(1))

    llabel = None
    if partner_paren is None:                    # rigid label style only
        llabel = _LEADER_LABEL_RE.search(c)
        if llabel:
            leader = llabel.group(1)

    pms = _PMS_RE.search(c)

    # Months: bracket first, else the pipe-delimited months in a HyperImage paren.
    bm = _BRACKET_MONTHS_RE.search(c)
    start_month = end_month = ""
    if bm:
        nums = [int(n) for n in _M_NUM_RE.findall(bm.group(0))]
        if nums:
            start_month, end_month = f"M{nums[0]:02d}", f"M{nums[-1]:02d}"
    elif pipe_months:
        pm = _PIPE_MONTHS_RE.search(pipe_months)
        if pm:
            start_month = f"M{int(pm.group(1)):02d}"
            end_month = f"M{int(pm.group(2)):02d}"

    # Title ends at the first metadata marker; body starts after the last one.
    markers = [m for m in (bm, partner_paren, plabel, llabel) if m]
    title_cut = min([m.start() for m in markers] + [len(c)])
    body_start = max(
        [m.end() for m in (bm, partner_paren, plabel, llabel, pms) if m] + [idm.end()]
    )

    meta = {
        "id": idm.group(1).upper(),
        "wp_id": f"WP{idm.group(2)}",
        "title": c[idm.end():title_cut].strip(" -–:,"),
        "start_month": start_month,
        "end_month": end_month,
        "leader": leader,
        "partners": partners,
    }
    return meta, c[body_start:].lstrip(" -–:],.")



class PdfplumberTableParser(TableParser):
    """Stage-1 implementation backed by pdfplumber + docling fallback."""

    def __init__(self, company: str) -> None:
        self.company = company.upper().strip()

    def parse(self, pdf_path: str) -> ProposalTables:
        warnings: list[str] = []
        wp_info: dict[str, dict] = {}
        effort: dict[str, float] = {}
        raw_delivs: list[dict] = []

        try:
            pdf_ctx = pdfplumber.open(pdf_path)
        except PdfminerException as exc:
            # A truncated/corrupted upload (interrupted transfer, already-
            # broken source file) — not a pipeline bug. Re-raised as
            # ValueError so it surfaces as a clear, actionable message
            # instead of "Internal extraction error" (see worker.py's
            # except ValueError branch).
            raise ValueError(
                "Could not open the PDF — the file appears to be corrupted "
                "or incomplete. Please try re-uploading it."
            ) from exc

        with pdf_ctx as pdf:

            #   Find the section 3 pages first, then look for tables only in those pages. 
            section3_idxs = find_section3_pages(pdf)
            if not section3_idxs:
                raise ValueError("Could not locate Section 3 in the PDF.")

            #   Find all tables in the section 3 pages, then identify which is which by their content.
            tables = find_all_tables(pdf, section3_idxs, pdf_path)

            if not tables["wp_list"]:
                warnings.append("WP list table not found.")
            else:
                wp_fragments = [t for _, t in tables["wp_list"]]
                wp_info = parse_wp_list_table(merge_wp_list_fragments(wp_fragments))

            if not tables["effort"]:
                warnings.append("Effort table not found.")
            else:
                _, effort_table = tables["effort"]
                effort = parse_effort_table(
                    effort_table,
                    expected_wp_ids=list(wp_info.keys()),
                    company=self.company,
                )

            if not tables["deliverable"]:
                warnings.append("Deliverable table(s) not found.")
            else:
                deliv_page_idxs = [i for i, _ in tables["deliverable"]]
                deliv_pages = [pdf.pages[i] for i in deliv_page_idxs]
                raw_delivs = parse_deliverables(deliv_pages)

        return ProposalTables(
            wp_info=wp_info,
            effort=effort,
            raw_delivs=raw_delivs,
            warnings=warnings,
        )


_MONTH_RANGE_RE = re.compile(r"\[M\s*(\d{1,2})\s*-\s*M\s*(\d{1,2})\]", re.I)


def _fill_months_from_chunk(tasks: list[dict], chunk: str) -> None:
    """Overwrite each task's start/end month from the source chunk.

    The LLM regularly drops the inline "[Mxx-Myy]" marker entirely, or
    fabricates a plausible-looking but wrong range (e.g. "Reliable AI &
    Risk-Based Decision Framework [M31-M36]" coming back as M03-M06).
    The marker always follows the task's own id in the source text, so
    re-extract it deterministically per task instead of trusting the LLM.
    """
    for task in tasks:
        task_id = task.get("id", "")
        if not task_id:
            continue

        id_match = re.search(rf"\b{re.escape(task_id)}\b", chunk, re.I)
        if not id_match:
            continue

        window = chunk[id_match.end(): id_match.end() + 250]
        month_match = _MONTH_RANGE_RE.search(window)
        if month_match:
            task["start_month"] = f"M{int(month_match.group(1)):02d}"
            task["end_month"] = f"M{int(month_match.group(2)):02d}"

        # LLM sometimes leaves the bracket sitting in the title instead of
        # stripping it — remove it now that we've captured it properly.
        title = task.get("title", "")
        if title:
            task["title"] = _MONTH_RANGE_RE.sub("", title).strip()
            
class OllamaTaskExtractor(TaskExtractor):
    """Stage-2 implementation backed by Ollama."""

    def __init__(self, company: str | None = None, on_progress: Callable[[int, int, str], None] | None = None) -> None:
        settings = get_settings()
        self.company = (company or settings.company).upper().strip()
        self._max_workers = settings.ollama_max_workers
        self._on_progress = on_progress

    
    def extract(self, pdf_path: str) -> ProposalTasks:
        with pdfplumber.open(pdf_path) as pdf:

            #  Find the WP task sections in the PDF and extract tasks from each via the LLM.
            task_sections = find_task_sections(pdf)

        if not task_sections:
            raise ValueError("Could not find WP task sections in the PDF.")

        raw_tasks: list[dict] = []
        failed_wps: list[dict] = []
        total = len(task_sections)        
        done = 0   

        t2 = time.monotonic()
        #   Use a thread pool to extract tasks for multiple WPs in parallel.
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:

            #   Submit jobs for each WP section 
            futures = {
                #   The key is the future object, use section[0] aka wp_id as the dictionary value
                executor.submit(self._extract_for_wp, *section): section[0]
                for section in task_sections
            }

            #   As each future completes, get the result (the list of tasks for that WP)
            for future in as_completed(futures):
                wp_id = futures[future]
                try:
                    raw_tasks.extend(future.result())
                except OllamaUnavailable as exc:
                    logger.warning("%s task extraction failed: %s", wp_id, exc)
                    failed_wps.append({"wp_id": wp_id, "error": str(exc)})
                except Exception as exc:  # pragma: no cover — last-resort logging
                    logger.exception("%s task extraction crashed", wp_id)
                    failed_wps.append({"wp_id": wp_id, "error": f"unexpected: {exc}"})
                finally:
                    done += 1
                    if self._on_progress is not None:
                        try:
                            self._on_progress(done, total, wp_id)
                        except Exception:  # progress must never break extraction
                            logger.debug("progress callback raised", exc_info=True)

        logger.info("Stage 2 finished in %.1fs", time.monotonic() - t2)

        # Dedup: when the LLM returns the same task ID more than once (e.g. because
        # a noisy chunk and a real chunk both produced it), keep the *better* one.
        # "Better" = has partners, has months, longer description.
        def _task_quality_score(t: dict) -> int:
            score = 0
            if t.get("partners"):
                score += 10
            if t.get("start_month") and t.get("end_month"):
                score += 10
            if t.get("title") and t["title"][:1].isupper():
                score += 5
            score += min(len(t.get("description") or ""), 1000) // 50
            return score

        best_by_id: dict[str, dict] = {}
        for raw in raw_tasks:
            validated = validate_task(raw)
            if not validated:
                continue

            #   Consistently uppercase the task ID so we can deduplicate reliably.
            tid = validated["id"].upper()
            incumbent = best_by_id.get(tid)

            #   If we have not seen this task ID before, or if the new one is "better" than the incumbent, keep it.
            if incumbent is None or _task_quality_score(validated) > _task_quality_score(incumbent):
                best_by_id[tid] = validated

        valid = list(best_by_id.values())
        print(f"       LLM returned {len(raw_tasks)} tasks, {len(valid)} valid")
        return ProposalTasks(raw_tasks=valid)

    @staticmethod
    def _extract_for_wp(
        wp_id: str,
        wp_title: str,
        page_idxs: list[int],
        section_text: str,
    ) -> list[dict]:
        """Run the LLM over each task chunk in a single WP section."""
        del page_idxs  # currently unused; kept for signature compatibility


        # Some WPs (e.g. WP12) state the WP-level start/end in their header line
        # rather than inside each task. Try to pull them so the LLM can fall
        # back to them when a task has no inline [Mxx-Myy] marker.
        wp_range_match = re.search(
            r"Start\s*M?(?P<s>\d+).{0,40}?End\s*M?(?P<e>\d+)",
            section_text,
            re.I | re.S,
        )
        if wp_range_match:
            wp_start = f"M{int(wp_range_match.group('s')):02d}"
            wp_end = f"M{int(wp_range_match.group('e')):02d}"
        else:
            wp_start = wp_end = ""

        header = (
            f"WP Number {wp_id}\n"
            f"WP Title {wp_title}\n"
            f"WP Start {wp_start}\n"
            f"WP End {wp_end}\n"
        )

        task_chunks = split_wp_into_task_chunks(section_text, wp_id=wp_id)

        all_tasks: list[dict] = []

        for chunk in task_chunks:
            #   LLM call for this WP's task section, with timing
            t0 = time.monotonic()
            tasks = extract_tasks_via_llm(header + chunk)
            _fill_months_from_chunk(tasks, chunk)
            elapsed = time.monotonic() - t0

            #   Force correct WP ID on all tasks returned for this section, since the LLM might mess it up 
            for task in tasks:
                task["wp_id"] = wp_id


            all_tasks.extend(tasks)
            logger.debug(
                "%s: %d tasks from chunk in %.1fs",
                wp_id,
                len(tasks),
                elapsed,
            )

        return all_tasks


class DefaultAssembler(Assembler):
    """Stage-3 implementation that filters and assembles WorkPackages."""

    def __init__(self, company: str, proposal_start_date: str) -> None:
        self.company = company.upper().strip()
        self.proposal_start_date = proposal_start_date

    def assemble(
        self,
        stage1: ProposalTables,
        stage2: ProposalTasks,
    ) -> list[WorkPackage]:
        return _assemble_logic(
            wp_info=stage1.wp_info,
            effort=stage1.effort,
            raw_tasks=stage2.raw_tasks,
            raw_delivs=stage1.raw_delivs,
            company=self.company,
            proposal_start_date=self.proposal_start_date,
        )




class RegexTaskExtractor(TaskExtractor):
    """Stage-2 implementation that parses tasks deterministically (no LLM).

    Same section locator as OllamaTaskExtractor, but the per-chunk LLM call is
    replaced by parse_heading_and_body(), which reads id/title/months/leader/
    partners out of the rigid heading line. The prose after the heading becomes
    the description. Drop-in for OllamaTaskExtractor: identical constructor
    signature and identical ProposalTasks output shape.
    """

    def __init__(
        self,
        company: str | None = None,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> None:
        settings = get_settings()
        self.company = (company or settings.company).upper().strip()
        self._on_progress = on_progress

    def extract(self, pdf_path: str) -> ProposalTasks:
        with pdfplumber.open(pdf_path) as pdf:
            task_sections = find_task_sections(pdf)

        if not task_sections:
            raise ValueError("Could not find WP task sections in the PDF.")

        raw_tasks: list[dict] = []
        total = len(task_sections)

        t2 = time.monotonic()
        # Serial on purpose: regex is CPU-only and microsecond-fast, so the
        # thread pool the LLM path needs (to overlap network I/O) buys nothing.
        for done, (wp_id, wp_title, page_idxs, section_text) in enumerate(
            task_sections, start=1                      # progress callback expects 1-based count
        ):
            del page_idxs, wp_title  # unused; kept for section-tuple compatibility

            raw_tasks.extend(self._extract_for_wp(wp_id, section_text))

            #   report progress after each WP section, so the caller can update a progress bar
            if self._on_progress is not None:
                try:
                    self._on_progress(done, total, wp_id)
                except Exception:  # progress must never break extraction
                    logger.debug("progress callback raised", exc_info=True)

        logger.info("Stage 2 (regex) finished in %.2fs", time.monotonic() - t2)

        # Validate + first-wins dedup so downstream sees the same shape the
        # LLM path produces. (No quality-score tie-break needed: regex yields
        # one deterministic parse per chunk, not competing noisy variants.)
        best_by_id: dict[str, dict] = {}
        for raw in raw_tasks:
            validated = validate_task(raw)
            if not validated:
                continue
            tid = validated["id"].upper()
            best_by_id.setdefault(tid, validated)

        valid = list(best_by_id.values())
        logger.info(
            "Stage 2 (regex): %d task chunks, %d valid", len(raw_tasks), len(valid)
        )
        return ProposalTasks(raw_tasks=valid)

    @staticmethod
    def _extract_for_wp(wp_id: str, section_text: str) -> list[dict]:
        """Parse every task chunk in one WP section deterministically."""

        # WP-level start/end fallback for tasks with no inline [Mxx-Myy],
        # mirroring the header the LLM path fed the model.
        wp_range_match = re.search(
            r"Start\s*M?(?P<s>\d+).{0,40}?End\s*M?(?P<e>\d+)",
            section_text,
            re.I | re.S,
        )
        if wp_range_match:
            wp_start = f"M{int(wp_range_match.group('s')):02d}"
            wp_end = f"M{int(wp_range_match.group('e')):02d}"
        else:
            wp_start = wp_end = ""

        tasks: list[dict] = []
        for chunk in split_wp_into_task_chunks(section_text, wp_id=wp_id):

            parsed = parse_heading_and_body(chunk)
            if parsed is None:
                continue

            meta, body = parsed

            # The common task schema has no separate "leader" field.
            # Include the explicitly parsed leader in the partners list,
            # matching the shape produced by the Ollama extractor.
            partners = list(meta["partners"])
            leader = meta["leader"].strip()

            if leader and leader not in {p.upper() for p in partners}:
                partners.insert(0, leader)                              #   leader first, then the rest of the partners

            tasks.append(
                {
                    "id": meta["id"],
                    "wp_id": wp_id,  # force the section's WP id, not the id-derived one
                    "title": meta["title"],
                    "start_month": meta["start_month"] or wp_start,
                    "end_month": meta["end_month"] or wp_end,
                    "partners": partners,
                    "description": body,
                }
            )
        return tasks
    
    
        