import re
import pdfplumber
from proposal_service.services.pdf_locator import find_task_sections
from proposal_service.services.implementations import split_wp_into_task_chunks

PDF_PATH = "AGROBOOST_Proposal-SEP-211030513 1-174-187.pdf"
TASK_RE = re.compile(r"\bT\d+\.\d+\b", re.I)

with pdfplumber.open(PDF_PATH) as pdf:
    sections = find_task_sections(pdf)

print(f"\nfind_task_sections returned {len(sections)} WP sections\n")
for wp_id, title, pages, text in sections:
    task_ids = sorted(set(t.upper() for t in TASK_RE.findall(text)))
    chunks = split_wp_into_task_chunks(text, wp_id=wp_id)
    print(f"{wp_id} — {title[:60]!r}")
    print(f"  pages={pages}, chars={len(text)}")
    print(f"  task IDs in text: {task_ids}")
    print(f"  chunks: {len(chunks)}")
    print()