"""Stage-1 table parsers (WP list, effort matrix, deliverables)."""

from __future__ import annotations

import logging
import re
from typing import Any

from proposal_service.utils import clean, norm_cell, normalize_month_token


logger = logging.getLogger(__name__)


def _merge_header_rows(header_rows: list[list[str]]) -> list[str]:
    """
    Merge 1 or 2 header rows column-by-column.

    Example:
      ["Lead", "Person", "Start"]
      ["Short name", "Months", "month"]

    becomes:
      ["LEAD SHORT NAME", "PERSON MONTHS", "START MONTH"]
    """

    if not header_rows:
        return []

    max_cols = max(len(r) for r in header_rows)
    merged: list[str] = []

    for col_idx in range(max_cols):
        parts: list[str] = []
        for row in header_rows:
            if col_idx < len(row):
                value = norm_cell(row[col_idx])
                if value:
                    parts.append(value)

        merged.append(norm_cell(" ".join(parts)))

    return merged



def merge_wp_list_fragments(fragments: list[list[list[str]]]) -> list[list[str]]:
    """Concatenate a WP-list table plus any headerless continuation
    fragments from later pages (e.g. Table 3.1a split across a Gantt
    chart / page break) into a single table.

    parse_wp_list_table only inspects the first 1-2 rows to detect the
    header, so simple concatenation is safe: header rows come from the
    first fragment, and every row of subsequent fragments is treated as
    a data row.
    """
    merged: list[list[str]] = []
    for frag in fragments:
        if frag:
            merged.extend(frag)
    return merged


def parse_wp_list_table(table: list[list[str]]) -> dict[str, dict]:
    """
    Parse WP list summary table.

    Handles both:
      1-row header:
        WP | WP Title | Leader | PMs | Start | End

      2-row header:
        WP | Work package title | Lead | Lead | Person Months | Start | End
        No.| No.                | No.  | Short name |          | month | month
    """

    wp_info: dict[str, dict] = {}
    if not table:
        return wp_info

    rows = [[clean(c) for c in row] for row in table if row]
    if len(rows) < 2:
        return wp_info

    # Detect whether row 2 is a continuation header or first data row
    second_row_is_header = (
        len(rows) > 2
        and any(
            x in norm_cell(c)
            for c in rows[1]

            #   If second row contains "No", "Number", "Month" or "Short Name" it's likely a header row
            for x in ("NO", "NUMBER", "MONTH", "SHORT NAME")
        )

        #   But if it contains "WP1", "WP2" etc. it's likely a data row with misaligned first column, so don't treat it as header
        and not any(re.search(r"\bWP\s*\d+\b", norm_cell(c), re.I) for c in rows[1])
    )

    #  Merge header rows if needed, and normalize to uppercase
    header_source = rows[:2] if second_row_is_header else rows[:1]
    header = [norm_cell(c) for c in _merge_header_rows(header_source)]
    
    data_rows = rows[2:] if second_row_is_header else rows[1:]

    #   Return the first column index that contains any of the needles, ignoring columns that contain any of the reject words
    def find_col(*needles: str, reject: tuple[str, ...] = ()) -> int | None:
        for i, h in enumerate(header):
            if any(r in h for r in reject):
                continue
            if any(n in h for n in needles):
                return i
        return None

    wp_col = find_col("WP", reject=("TITLE", "WORK PACKAGE"))
    
    title_col = find_col("WP TITLE", "WORK PACKAGE TITLE", "WORKPACKAGE TITLE")
    
    leader_col = find_col("SHORT NAME", "LEADER", "LEAD", reject=("NO", "NUMBER"))
    
    pm_col = find_col(
        "PERSON", "PM", reject=("START", "END", "MONTH START", "MONTH END")
    )

    start_col = find_col("START")
    
    end_col = find_col("END")

    # Helper to safely get a cell value by index, returning default if index is out of range
    def cell(row: list[str], idx: int | None, default: str = "") -> str:
        return row[idx] if idx is not None and idx < len(row) else default

    for row in data_rows:
        cells = [clean(c) for c in row]
        if not any(cells):
            continue

        # WP id
        wp_raw = cell(cells, wp_col)
        #   First try to find a WP pattern in the configured WP column (handles "WP1", "WP 1", etc.)
        match = re.search(r"\bWP\s*(\d+)\b", wp_raw, re.I)
        
        #   If not found, try to find a WP pattern anywhere in the row (handles misaligned rows where WP number is in another column)
        if not match:
            # First: WP\d may sit in a different column than wp_col (misaligned row).
            joined = " | ".join(cells)
            match = re.search(r"\bWP\s*(\d+)\b", joined, re.I)

        if not match:
            # Otherwise the id may be a bare integer in a shifting column
            # (KnoWare-style '5'/'6'); take the first standalone 1-2 digit cell.
            bare = next((c for c in cells if re.fullmatch(r"\d{1,2}", c)), "")
            if bare:
                wp_id = f"WP{bare}"
            else:
                continue
        else:
            wp_id = f"WP{match.group(1)}"
        
        wp_id = wp_id.upper()

        # Title
        title = cell(cells, title_col)
        
        if not title or re.fullmatch(r"WP\s*\d+", title, re.I):
            #   Choose the first long text cell that is not 64, 61.5, M01, 1, UCD, DIVE
            title = next(
                (
                    c
                    for c in cells
                    if len(c) > 6
                    and not re.fullmatch(r"WP\s*\d+", c, re.I)
                    and not re.fullmatch(r"\d+[\.,]?\d*", c)
                    and not re.fullmatch(r"M?\d+", c, re.I)
                    and not re.fullmatch(r"[A-Z][A-Z0-9&\-]{1,9}", c)
                ),
                "",
            )

        # Leader
        leader = cell(cells, leader_col)
        
        #   If detected leader doesn't look like a partner acronym, try to find a cell that looks like an acronym (handles misaligned rows where leader is in another column)
        if not leader:
            leader = next(
                (
                    c
                    for c in cells
                    if re.fullmatch(r"(?=.{1,9})[A-Z]+(?:[&\-][A-Z]+)*", c, re.I)
                    and not re.fullmatch(r"WP\s*\d+", c, re.I)
                ),
                "",
            )

        # PMs
        pms_str = cell(cells, pm_col)
        
        if not re.fullmatch(r"\d+[\.,]?\d*", pms_str or ""):
            #   Collect all numeric cells
            nums = [c for c in cells if re.fullmatch(r"\d+[\.,]?\d*", c)]
            
            # Prefer decimals because PMs are often 61,5 / 14,8 etc.
            pms_str = next((n for n in nums if "," in n or "." in n), "")
            
            # Fallback: If there is no decimal column avoid WP no / lead no / start / end by taking third-from-last
            if not pms_str and len(nums) >= 3:
                pms_str = nums[-3]
            elif not pms_str and nums:
                pms_str = nums[-1]
        
        
        pms = float(pms_str.replace(",", ".")) if pms_str else 0.0

        # Start / end months
        start = normalize_month_token(cell(cells, start_col))
        end = normalize_month_token(cell(cells, end_col))
        
        if not start or not end:
            numeric_months = [normalize_month_token(c) for c in cells if normalize_month_token(c)]
            
            # Usually final two numeric columns are start/end
            if len(numeric_months) >= 2:
                start = numeric_months[-2]
                end = numeric_months[-1]

        wp_info[wp_id] = {
            "title": title,
            "leader": leader,
            "pms": pms,
            "start": start,
            "end": end,
        }

    return wp_info


def _to_float(cell_value: str) -> float:
    text = (cell_value or "").replace(",", ".").strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group()) if match else 0.0


def parse_effort_table(
    table: list[list[str]],
    expected_wp_ids: list[str] | None = None,
    *,
    company: str,
) -> dict[str, float]:
    """Parse the effort/person-months table for ``company``.

    Supports two layouts:
    Layout A (AGROBOOST-style):
      row 0 = partner acronyms
      rows below = numeric rows, one row per WP
      WPs may be implicit by row order

    Layout B (HyperImage-style):
      one row contains WP1..WPn headers
      body rows contain partner name in first column
      values for the configured company are in that row
    """
    effort: dict[str, float] = {}
    if not table:
        return effort

    rows = [[clean(c) for c in row] for row in table if row]
    company = company.upper().strip()

    # Layout A: company acronym appears in the header row.
    header = [c.upper() for c in rows[0]]
    if company in header:
        company_col = next((i for i, c in enumerate(header) if c == company), None)
        if company_col is None:
            return effort

        explicit_wp_rows: list[list[str]] = []
        for row in rows[1:]:
            if not row:
                continue
            first = row[0].upper() if row else ""
            if re.fullmatch(r"WP\d+", first):
                explicit_wp_rows.append(row)

        if explicit_wp_rows:
            for row in explicit_wp_rows:
                wp_id = row[0].upper()
                raw = row[company_col] if company_col < len(row) else ""
                effort[wp_id] = _to_float(raw)
            return effort

        numeric_rows: list[list[str]] = []
        for row in rows[1:]:
            vals = [c for c in row if c]
            if not vals:
                continue
            numericish = sum(bool(re.search(r"\d", c)) for c in vals)
            if numericish >= max(3, len(vals) // 2):
                numeric_rows.append(row)

        if expected_wp_ids:
            wp_ids = sorted(
                expected_wp_ids,
                key=lambda x: int(re.search(r"\d+", x).group()),  # type: ignore[union-attr]
            )
            numeric_rows = numeric_rows[: len(wp_ids)]
            for wp_id, row in zip(wp_ids, numeric_rows):
                raw = row[company_col] if company_col < len(row) else ""
                effort[wp_id] = _to_float(raw)
        else:
            for idx, row in enumerate(numeric_rows, start=1):
                wp_id = f"WP{idx}"
                raw = row[company_col] if company_col < len(row) else ""
                effort[wp_id] = _to_float(raw)
        return effort

    # Layout B: a row contains WP1..WPn, company appears as a body row.
    wp_header_idx: int | None = None
    wp_cols: dict[int, str] = {}
    for i, row in enumerate(rows):
        current = {
            j: c.upper()
            for j, c in enumerate(row)
            if re.fullmatch(r"WP\d+", c, re.I)
        }
        if len(current) >= 3:
            wp_header_idx = i
            wp_cols = current
            break

    if wp_header_idx is None:
        return effort

    # Partner cells combine an id with the short name, and the ordering
    # varies by document ("10/NETC" vs "NETC/P10") — match the company as
    # a token bounded by "/" or the cell edges rather than requiring an
    # exact cell match, so either ordering (or a bare "NETC" cell) works.
    company_token_re = re.compile(
        rf"(?:^|/)\s*{re.escape(company)}\s*(?:/|$)", re.I
    )
    company_row: list[str] | None = None
    for row in rows[wp_header_idx + 1 :]:
        if not row:
            continue
        if any(c and company_token_re.search(c) for c in row):
            company_row = row
            break

    if company_row is None:
        return effort

    for col_idx, wp_id in wp_cols.items():
        candidates: list[str] = []
        for k in (col_idx, col_idx - 1, col_idx + 1):
            if 0 <= k < len(company_row):
                candidates.append(company_row[k])
        raw = next((c for c in candidates if c and re.search(r"\d", c)), "")
        effort[wp_id] = _to_float(raw)

    return effort


def parse_deliverable_ids(id_cell: str) -> list[str]:
    """Parse deliverable IDs from a cell, supporting explicit lists and ranges."""
    text = clean(id_cell).replace("–", "-").replace("—", "-")
    range_match = re.fullmatch(r"(D(\d+)\.(\d+))\s*-\s*(D(\d+)\.(\d+))", text, re.I)
    if range_match:
        wp1, start = int(range_match.group(2)), int(range_match.group(3))
        wp2, end = int(range_match.group(5)), int(range_match.group(6))
        if wp1 == wp2 and start <= end:
            return [f"D{wp1}.{n}" for n in range(start, end + 1)]
    ids = re.findall(r"D\d+\.\d+", text, re.I)
    return [d.upper() for d in ids]


def looks_like_deliverable_id_cell(text: str) -> bool:
    """True when the cell text contains a D-number anywhere."""
    text = clean(text).replace("–", "-").replace("—", "-")
    return bool(re.search(r"\bD\d+\.\d+\b", text, re.I))


def _looks_like_deliverable_row(
    ids: list[str], lead: str, dtype: str, diss: str, months: list[str]
) -> bool:
    return bool(ids) and bool(lead) and bool(dtype) and bool(diss) and bool(months)


def parse_deliverables(pages: list[Any]) -> list[dict]:
    """Parse the deliverables table(s) and return a list of raw dicts."""
    raw_deliverables: list[dict] = []
    all_rows: list[list[str]] = []

    for page in pages:
        for table in page.extract_tables():
            if not table:
                continue
            for row in table:
                all_rows.append([clean(c) for c in row])

    for row in all_rows:
        id_cell = next((c for c in row if looks_like_deliverable_id_cell(c)), None)
        if not id_cell:
            continue

        ids = parse_deliverable_ids(id_cell)
        if not ids:
            continue

        # WP number
        wp = ""
        for c in row:
            if re.match(r"^WP\s*\d+$", c, re.I):
                wp = "WP" + re.search(r"\d+", c).group()  # type: ignore[union-attr]
                break
        if not wp:
            inferred = re.match(r"D(\d+)\.\d+", ids[0], re.I)
            if inferred:
                wp = "WP" + inferred.group(1)

        # Lead partner
        lead = next(
            (
                c
                for c in row
                if re.match(r"^(?=.{1,9}$)[A-Z]+(?:[&\-][A-Z]+)*$", c, re.I)
                and not re.match(r"^(WP\d+|PU|CO|SEN|RE|R|DEM|DMP|DEC|OTHER)$", c, re.I)
            ),
            "",
        )

        # Type (R / DEM / OTHER, possibly combined)
        dtype = ""
        for c in row:
            matches = re.findall(r"\b(R|DEM|DMP|DEC|OTHER)\b", c, re.I)
            if matches:
                dtype = ", ".join(dict.fromkeys(m.upper() for m in matches))
                break

        # Dissemination (PU / CO / SEN / RE)
        diss = ""
        for c in row:
            match = re.search(r"(PU|SEN|CO|RE)", c, re.I)
            if match:
                diss = match.group(1).upper()
                break

        # Due months
        month_cells = [c for c in row if re.search(r"M\d+", c)]
        months: list[str] = []
        for mc in month_cells:
            months.extend(re.findall(r"M\d+", mc))
        months = [m.upper() for m in months]

        if not _looks_like_deliverable_row(ids, lead, dtype, diss, months):
                    logger.debug(
                        "deliverable row DROPPED ids=%s | missing=%s | row=%r",
                        ids,
                        [n for n, v in (("lead", lead), ("type", dtype),
                                        ("diss", diss), ("months", months)) if not v],
                        row[:8],
                    )
                    continue

        consumed = {id_cell, wp, lead, dtype, diss} | set(month_cells)
        text_cells = [
            c
            for c in row
            if c not in consumed
            and len(c) > 8
            and not re.match(r"^(D\d|WP|M\d)", c, re.I)
        ]
        # Columns are left-to-right: Name precedes Short description.
        # Do NOT sort by length — it scrambles which is which.
        name = text_cells[0] if text_cells else ""
        description = text_cells[1] if len(text_cells) > 1 else ""
       
        if name and description and len(name) > len(description):
            name, description = description, name

        raw_deliverables.append(
            {
                "ids": ids,
                "name": name,
                "description": description,
                "wp": wp.upper(),
                "lead": lead,
                "type": dtype,
                "dissemination": diss,
                "months": months,
            }
        )

    return raw_deliverables

