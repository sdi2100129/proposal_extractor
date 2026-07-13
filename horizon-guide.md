# Writing Section 3 So It Parses Cleanly

A formatting guide for Horizon Europe proposal authors.

This document describes the **layout conventions** the extraction tool relies on to find Section 3 and read its tables (work packages, effort, deliverables, tasks). The tool reads your PDF deterministically — it looks for specific heading words, column labels, and cell patterns. If your tables follow the conventions below, every work package, person-month figure, deliverable, and task is captured automatically. If they don't, the affected rows are quietly skipped rather than guessed.

A guiding principle: **the tool fails visibly, not silently — but only if you give it a clean anchor to latch onto.** When an ID, header word, or code is missing, the affected row simply drops out of the output instead of being invented. So the single most useful habit is to keep the *anchor* columns — IDs, header labels, dissemination/type codes — consistent and predictable.

The final section (§10) is a complete reference list of every pattern the tool recognises, for readers who want the exact rules.

---

## 1. How the tool finds Section 3

The tool scans for the **Section 3 heading** and reads everything from there until **Section 4**.

**Do** start Section 3 with a heading containing the number `3` followed by one of:

- `Quality`
- `Implementation`
- `Work plan`
- `Efficiency`

Recognised examples:

> `3. Quality and efficiency of the implementation`
> `3 Implementation`
> `Section 3 – Quality and efficiency of the implementation`

**Do** end the section with a Section 4 heading containing `4.` plus one of `Ethics`, `Budget`, `Annex`, `Financial`. The tool stops scanning there.

**Avoid:**

- Putting the Section 3 heading *only* in the table of contents. A page with 5+ dotted-leader lines (`........ 34`) is detected as a ToC and skipped; the real heading must appear on the section page itself, surrounded by at least a few lines of normal paragraph text.
- Renaming the section so it no longer contains one of the keyword words above.

*(For pages that are genuinely ambiguous — a keyword present but no clear structure — the tool falls back to an automated check, so a slightly unusual layout will usually still be found. The conventions above make that fallback unnecessary.)*

---

## 2. General rules for every table

Every Section 3 table must first pass two basic checks:

| Requirement | Why |
|---|---|
| At least **2 rows** (one header + one data row) | A single-row fragment is dropped before classification. |
| At least **3 columns with actual content** | Tables that collapse to 1–2 populated columns are treated as stray fragments and dropped. |

Two further rules keep ordinary tables from being misread:

- **Keep individual cells short in the WP-list and effort tables.** If any cell exceeds ~400 characters, the whole table is treated as a prose paragraph and ignored. (Deliverable tables are exempt — their first column of `D-numbers` identifies them even with long description cells.)
- **Don't reuse reserved tokens.** A cell containing `MS1`, `MS2`, … marks the table as *milestones* (skipped). The words `Milestone` or `Means of verification` in a header do the same. The phrase `WP Number` in the first row marks a table as a *WP description block* (skipped as a summary table). Keep those tokens only where they belong.

*(Rotated or vertically-stacked table text — e.g. partner acronyms printed sideways — is detected and re-read through a secondary engine, so a rotated effort header can still work. Horizontal text is always safest.)*

---

## 3. Table 3.1a — List of work packages

The WP summary table is identified by its **header row**, then read one work package per data row.

### Required header columns

The header must contain a column for **each** of these (any listed wording works):

| Field | Accepted header wording |
|---|---|
| WP number | `WP` (a column labelled `WP` that does **not** also say "Title") |
| WP title | `WP Title` · `Work package title` |
| Leader | `Leader` · `Lead` · `Short name` |
| Person-months | `PMs` · `Person Months` · `PM` |
| Start | `Start` · `Start month` |
| End | `End` · `End month` |

A table is accepted as the WP list when the header has **WP-title AND leader AND start AND end**, plus at least **two** WP data rows. A one-line or two-line header band both work.

### Row conventions

| Column | Write it as | Notes |
|---|---|---|
| WP id | `WP1` *or* a bare `1` | Both recognised. Be consistent down the column. |
| Title | Free text | A line wrap inside the title is fine. |
| Leader | An uppercase acronym (up to 9 chars) | `SINTEF`, `NTNU`, `G&D`, `MEWS L` — see leader note below. |
| PMs | A number | Decimal comma or dot both work: `135,5` / `135.5`. |
| Start / End | `M1`, `M01`, `M06` … | Normalised to `M01` form. |

### Example (recognised)

| WP | WP Title | Leader | PMs | Start | End |
|---|---|---|---|---|---|
| WP1 | Project and Technical Management | UCD | 64 | M01 | M60 |
| WP2 | Socioeconomics methodologies and tools | FIB | 133 | M01 | M60 |
| WP3 | AR/VR and robotic solutions | INE | 129 | M07 | M48 |

### Notes and caveats

- **Leaders:** a leader is taken as-is from the Leader column, so multi-token names such as `MEWS L` are now kept **when the column is cleanly aligned.** If a row gets fragmented during extraction and the tool has to recover the leader from elsewhere in the row, only a single acronym (letters, optionally joined by `&` or `-`, up to 9 chars) is matched — a two-word name may then come back empty. Space-free acronyms are the most robust, but no longer strictly required.
- **The table may now span a page break.** Table 3.1a is often split by a Gantt chart or page boundary. A continuation fragment on a later page is recognised **even without a repeated header**, as long as each continuation row still carries a **WP id** (`WP4` or a bare `4`) **and a start/end month pair** (two `M#` tokens). Fragments are automatically stitched back together.
  - The continuation must appear **within 3 pages** of the first WP-list fragment. A WP-list-looking table further away is treated as a misclassification and ignored (this protects against an effort matrix many pages later being swallowed in).
  - If you *can* keep the table on one page, or repeat the header on the continuation, that's still the safest choice.
- **Avoid a `Total` row inside the WP rows** where possible. A row that has neither a real WP id nor a month pair won't be mistaken for a WP, but keeping the total outside the table removes all doubt.

---

## 4. Table 3.1b — Work package descriptions and tasks

This block feeds the task extractor. The zone begins at a heading containing **`Work package description`** (also `Work packages description` / `… desc`).

### WP description header

Open each work package with a header line in one of these shapes — all recognised:

> `WP Number 1` · `WP Number WP1` · `WP number: 1` · `WP1` · `WP #1`
> followed by the title, or by `WP Title <the title>`.

Keep the literal phrase **`WP Number`** here — it is the marker that says "this is a description block, not the summary table." The title is read up to the word `Objectives:` or the first task ID.

A WP header is only accepted as a real section start when the block also contains **`Objectives`** or a **task ID** (`T1.1`), *or* the title is written in the `WP Title <name>` form. Inline mentions like "…as covered in WP2 for revision…" are rejected, and a `WP3–WP5` range reference is never treated as a header.

### Task headings

Write each task heading so it **starts a line** and follows this shape:

> `T<wp>.<n> <Title> [M<start>-M<end>] (<Leader>; <Partners>) … prose …`

| Element | Write it as | Example |
|---|---|---|
| Task ID | `T` + WP number + `.` + task number, at line start | `T3.2` |
| Month range | In square brackets | `[M07-M48]` or `[M1–M36]` |
| Partners | In parentheses after the title | `(AUA, UCD)` |

A task ID must be **immediately followed** by a separator (`-`, `–`, `:`), an uppercase title word, a `(` partner list, or a `[` month bracket. That is how the tool distinguishes a real heading (`T3.2 AR-based system …`) from an inline reference (`…feeds into T3.2 and T4.1…`), which it ignores.

**Avoid** using a task ID from a *different* WP as a standalone heading inside a WP block (e.g. `T2.1` written as a heading inside the WP6 description) — those are filtered to prevent cross-contamination. Inline references in prose are fine.

The zone ends automatically at any of: `List of deliverables`, `List of milestones`, `Critical risks`, `Capacity of participants`, `Associated partners`.

---

## 5. Table 3.1c — List of deliverables

Identified by its header **plus** a first column of deliverable IDs.

### Header

Include at least one of: `Deliverable`, `Del. No`, `Type`, `Diss`, `Deliv date`.

### Required columns — fill *every* one

A deliverable row is kept only when **all five** of these are present. Leave any blank and the row is dropped:

| Column | Write it as | Accepted values |
|---|---|---|
| ID | `D<wp>.<n>` in the first column | `D1.1`; ranges `D1.2 – D1.6`; lists `D5.1, D5.3, D5.5` |
| Lead | Short acronym (≤ 9 chars) | `UCD`, `WIS`, `NTNU`, `G&D` |
| Type | One or more type codes | `R`, `DEM`, `DMP`, `DEC`, `OTHER` (combinations like `R, DEM` are fine) |
| Diss. level | A dissemination code | `PU`, `CO`, `SEN`, `RE` |
| Due month(s) | One or more month tokens | `M04`; multiple like `M06, M20, M34` |

Name and short-description columns are read positionally: **Name first, Short description second.** Keep them in that order.

### Example (recognised)

| Del. No | Name | Description | WP | Lead | Type | Diss | Due |
|---|---|---|---|---|---|---|---|
| D1.1 | Project handbook | Management plan and QA guidelines | WP1 | UCD | R | SEN | M04 |
| D1.2 – D1.6 | Data management & ethics | Data policy and compliance reports | WP1 | WIS | R | SEN | M06, M20, M34, M48, M60 |

### Notes

- **Ranges and lists expand automatically.** `D1.2 – D1.6` becomes five deliverables; `D5.1, D5.3, D5.5` becomes three. Use a dash/en-dash for ranges and commas for lists.
- The WP number can be its own `WP1` cell, or it's inferred from the deliverable ID (`D3.x → WP3`).
- Long description cells are fine here (unlike the WP-list/effort tables) because the `D-number` first column identifies the table even on a continuation page with no header.

---

## 6. Table 3.1f — Summary of staff effort

The person-months matrix. **Two layouts** are recognised — pick whichever fits.

### Layout A — partners across the top, one row per WP

| | UCD | NTU | KUL | … | NCI | … | Total PM |
|---|---|---|---|---|---|---|---|
| WP1 | 24 | 2 | 1 | … | 4 | … | 64 |
| WP2 | 4 | 10 | 1 | … | 0 | … | 133 |

### Layout B — WPs across the top, one row per partner

| | WP1 | WP2 | WP3 | WP4 | WP5 | WP6 | WP7 | Total PM |
|---|---|---|---|---|---|---|---|---|
| SINTEF/P1 | 16 | 4 | 10 | 3,5 | 3 | 4,5 | 4 | 45 |
| NETC/P10 | 1 | 3 | 3 | 16 | 7 | 2 | 2 | 34 |

### Rules that make it recognisable

- The header row must be **short labels only** — partner acronyms, `WPn`, or `Total PM`. A header cell containing a full sentence (4+ words) stops the table from being detected. Keep the descriptive paragraph ("Most of the effort is allocated in…") as prose *above or below* the table, never inside it.
- At least **3 rows** of numeric data.
- The matrix is now recognised on **any** of these signals: a `Person Months`/`PM` header word, a header dominated by partner acronyms, both partners and WPs present as row/column labels, **or simply a header with three or more `WPn` columns.** A `Total PM` column is helpful but no longer strictly required.
- **Partner labels may carry a participant number in either order** — `NETC/P10` *or* `10/NETC`, and a bare `NETC` all match. The participant suffix is stripped automatically.
- Decimal comma is fine (`3,5`).
- A `Total PM` / `Total Person Months` column or row is fine and is ignored in the per-WP figures.
- Keep WP columns/rows in **numeric order** (WP1, WP2, …); values are mapped positionally onto that order.

---

## 7. Milestones and risks (3.1d / 3.1e)

These are **deliberately excluded** from the structured output, but format them predictably so they don't interfere with the tables above:

- **Milestones (3.1d):** use `MS1`, `MS2`, … in the first column and include `Milestone` or `Means of verification` in the header. These markers tell the tool "skip this." Keep `MS#` tokens *out* of every other table.
- **Risks (3.1e):** an ordinary free-text table; no special handling needed.

---

## 8. What is *not* extracted (by design)

So you know where the boundary sits: the tool captures the WP list, the effort matrix, deliverables, and the WP/task descriptions. It intentionally ignores milestones, the risk table, subcontracting/purchase/other-cost tables (3.1g–3.1j), and the consortium-capacity prose. You don't need to reformat those for the tool — only make sure they don't borrow the reserved tokens above.

---

## 9. Quick checklist

**Section**
- [ ] Section 3 heading contains `3` + (Quality / Implementation / Work plan / Efficiency), on the real page (not only the ToC).
- [ ] A Section 4 heading (`4.` + Ethics / Budget / Annex / Financial) marks the end.

**Every table**
- [ ] Header row + at least one data row, and at least 3 columns with content.
- [ ] No giant paragraph cells in the WP-list or effort tables.
- [ ] `MS#` only in the milestone table; `WP Number` only in the 3.1b blocks.

**3.1a — WP list**
- [ ] Header has WP, WP Title, Leader/Lead/Short name, PMs, Start, End.
- [ ] WP ids consistent (`WP1` *or* `1`).
- [ ] If split across pages, each continuation row still has a WP id + start/end months, and the continuation is within 3 pages.

**3.1b — WP descriptions**
- [ ] Each block opens with `WP Number <n>` and a title (or `WP Title <title>`).
- [ ] Block contains `Objectives` or task IDs.
- [ ] Task headings start a line: `T<wp>.<n>` + separator/title/`(`/`[`, with `[M..-M..]` and `(Leader; Partners)`.

**3.1c — Deliverables**
- [ ] Header includes Deliverable / Type / Diss / Deliv date.
- [ ] Every row has ID, Lead, Type, Diss level, and Due month — none blank.
- [ ] Name column before Description column.

**3.1f — Effort**
- [ ] Header is short labels only (acronyms / `WPn` / `Total PM`), no sentences.
- [ ] At least 3 numeric rows; WP order preserved.
- [ ] Partner labels like `NETC/P10` or `10/NETC` are both fine.

---

## 10. Complete pattern reference

Every recognition rule the tool applies, for precision.

### Section location
- **Section 3 start:** heading matching `3` + (`.`/space) + `QUALITY | IMPLEMENTATION | WORK PLAN | EFFICIENCY`.
- **Definite start (no fallback needed):** `Section 3 –` or the heading above, **and** ≥3 lines longer than 80 chars, **and** no dotted-leader lines.
- **Section end:** heading matching `4.` + `ETHICS | BUDGET | ANNEX | FINANCIAL`.
- **Table-of-contents skip:** ≥5 lines containing `....` followed by a page number.

### Table pre-filters (before classification)
- Dropped if fewer than 2 rows.
- Dropped if fewer than 3 populated columns.
- Rotated/stacked-text tables are re-extracted through a secondary engine.

### Table classification (first match wins)
1. **Prose blob → ignored:** any cell > 400 chars **and** no `D#.#` in the first column.
2. **Milestone → ignored:** any cell matching `MS#`, or header containing `MILESTONE` / `MEANS OF VERIFICATION`.
3. **WP description block → ignored as a table:** `WP NUMBER` present in the first row.
4. **WP list:** header contains (`WP TITLE` / `WORK PACKAGE TITLE`) **and** (`LEADER` / `LEAD` / `SHORT NAME`) **and** (`START` / `START MONTH`) **and** (`END` / `END MONTH`), with ≥2 rows carrying `WP#` or a bare 1–2 digit first cell.
5. **WP list continuation (no header):** ≥2 rows each with a first cell that is `WP#` or a bare 1–2 digit number **and** ≥2 `M#` tokens in the row.
6. **Deliverables:** header contains `DELIVERABLE` / `DEL.` / `DISS` / `TYPE` / `DELIV`, **and** either ≥2 first-column `D#.#` cells (≤12 chars) **or** (dissemination codes **and** `M#` due months present).
7. **Deliverables continuation (no header):** ≥2 first-column `D#.#` cells **and** (dissemination codes **or** `M#` months).
8. **Effort matrix:** ≥3 rows, header is not a WP-summary header, ≥2 numeric body rows, no header cell of 4+ words, **and** any of: a `PERSON`/`PM`/`PMS`/`MONTH` header word · ≥4 acronym-like header cells · partner acronyms in the first column together with `WP#` labels · **≥3 `WP#` columns in the header.**

### Collection across pages
- **WP list:** all fragments within 3 pages of the first are collected and merged.
- **Effort:** one table; a rules-classified table overrides an earlier fallback-classified one.
- **Deliverables:** every matching table across pages is collected.

### Field parsing — WP list
- **Columns** by header keyword: WP (excluding `TITLE`/`WORK PACKAGE`); `WP TITLE`/`WORK PACKAGE TITLE`; `SHORT NAME`/`LEADER`/`LEAD` (excluding `NO`/`NUMBER`); `PERSON`/`PM` (excluding `START`/`END`); `START`; `END`.
- **WP id:** `WP#` in the WP column → `WP#` anywhere in the row → first bare 1–2 digit cell.
- **Leader:** the Leader-column cell as written; if empty, the first acronym (letters, optional `&`/`-` joins, ≤9 chars) that isn't a `WP#`.
- **PMs:** the PM-column number; otherwise a decimal-bearing number, else a positional numeric fallback.
- **Start/End:** month tokens from the Start/End columns, else the last two month-like values.

### Field parsing — effort
- **Layout A:** company acronym found in the header row; values read down that column, one per WP (by explicit `WP#` rows or by row order).
- **Layout B:** a header row with ≥3 `WP#` columns; the company's row matched by acronym bounded by `/` or the cell edges (`NETC/P10`, `10/NETC`, `NETC`); values read across, aligned to the WP columns.

### Field parsing — deliverables
- **IDs:** any `D#.#`; ranges `D#.# – D#.#` expanded; comma lists split.
- **WP:** a `WP#` cell, else inferred from the first ID.
- **Lead:** an acronym ≤9 chars (letters with optional `&`/`-`) that isn't a WP/type/diss code.
- **Type:** any of `R | DEM | DMP | DEC | OTHER` (combined if several).
- **Dissemination:** `PU | SEN | CO | RE`.
- **Months:** all `M#` tokens.
- **Row kept only if** ID, lead, type, dissemination, and at least one month are all present.
- **Name / Description:** first and second remaining text cells (>8 chars), in column order.

### WP/task description text
- **Zone start:** heading matching `work package(s) description/desc`.
- **WP header forms:** `WP Number 1`, `WP Number WP1`, `WP number: 1`, `WP1`, `WP #1`, `WP Title <name>`.
- **Accepted as a real header** only with `Objectives` or a task ID present, or a `WP Title` form.
- **Rejected:** inline `WP#` references whose title starts with for/from/and/to/in/with/of/technical/work; `WP#–WP#` ranges.
- **Task IDs:** `T#.#` at a line start, followed by a separator, uppercase word, `(`, or `[`.
- **Zone end:** `List of deliverables | List of milestones | Critical risks | Capacity of participants | Associated partners`.

---

*Following these conventions lets the tool read your Section 3 without manual correction. Where a convention can't be met, the affected field is left empty rather than guessed — so a clean, consistent layout is the difference between a fully populated result and missing rows.*