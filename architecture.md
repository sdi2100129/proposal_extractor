# Architecture

This page documents the runtime topology and the three-stage extraction
pipeline of `proposal-service`. All diagrams are written in
[Mermaid](https://mermaid.js.org/) so they render directly on GitHub and in
the Sphinx docs (see *Rendering in Sphinx* at the bottom).

## 1. Deployment topology

The service runs as a set of Docker Compose containers behind an nginx reverse
proxy. The API talks to Ollama for LLM inference and to Keycloak for token
verification; Keycloak persists to PostgreSQL.

```mermaid
flowchart TB
    client["Client / Swagger UI"]

    subgraph net["Docker Compose network"]
        nginx["nginx<br/>:80 — reverse proxy"]
        api["proposal-api<br/>Gunicorn + UvicornWorker :8000"]
        ollama["Ollama<br/>:11434 — qwen2.5:3b"]
        keycloak["Keycloak<br/>:8080 — OIDC"]
        postgres[("PostgreSQL<br/>keycloak DB")]
    end

    client -->|HTTP| nginx
    nginx -->|proxy| api
    api -->|"POST /api/chat"| ollama
    api -->|"JWKS + token verify"| keycloak
    keycloak --> postgres
```

## 2. Three-stage pipeline

A proposal PDF is processed in three independent, separately runnable stages.
Stages 1 and 2 both read the PDF; Stage 3 merges their typed outputs and filters
to the configured company.

```mermaid
flowchart LR
    pdf["Proposal PDF"]

    subgraph s1["Stage 1 — Table extraction"]
        direction TB
        loc["locate Section 3 pages"]
        tabs["find + classify tables<br/>(rules first, LLM fallback)"]
        parse["parse WP list / effort / deliverables"]
        loc --> tabs --> parse
    end

    subgraph s2["Stage 2 — Task extraction"]
        direction TB
        sect["find WP task sections"]
        chunk["split into per-task chunks"]
        llm["LLM extract via Ollama<br/>(thread pool, per WP)"]
        sect --> chunk --> llm
    end

    subgraph s3["Stage 3 — Assembly"]
        direction TB
        merge["merge tables + tasks + deliverables"]
        filter["filter by company effort"]
        merge --> filter
    end

    pdf --> s1
    pdf --> s2
    parse -->|ProposalTables| merge
    llm -->|ProposalTasks| merge
    filter --> out["list[WorkPackage]"]
```

## 3. Request lifecycle (`POST /proposal`)

The full pipeline route. Routes stay thin: validate, persist the upload to a
temp file, run each stage in a threadpool so the event loop stays responsive,
then return a typed response model.

```mermaid
sequenceDiagram
    participant C as Client
    participant N as nginx
    participant A as FastAPI (routes)
    participant K as Keycloak
    participant P as Pipeline
    participant O as Ollama

    C->>N: POST /proposal (PDF + company, start_date)
    N->>A: proxy request
    A->>K: verify JWT (cached JWKS)
    K-->>A: claims OK (azp allow-listed)
    A->>A: persist upload to temp file

    A->>P: run_stage1 (threadpool)
    P-->>A: ProposalTables

    A->>P: run_stage2 (threadpool)
    P->>O: extract tasks per WP chunk
    O-->>P: raw task JSON
    P-->>A: ProposalTasks

    A->>P: run_stage3 (assemble + filter)
    P-->>A: list[WorkPackage]

    A-->>C: ProposalResponse (JSON)
```

## 4. Table classification: rules first, LLM as fallback

A core design principle of Stage 1: deterministic rules decide whenever they
can, and the LLM is consulted only when the rules are unsure. Rules fail
*visibly* (return `None`); the LLM is the costly, less predictable path.

```mermaid
flowchart TD
    t["extracted table"]
    rot{"rotated / stacked text?"}
    doc["docling fallback extract"]
    rules["classify_table_by_rules()"]
    known{"rule returned a label?"}
    llm["classify_table_via_llm()<br/>(Ollama)"]
    label["wp_list / effort / deliverable / other"]

    t --> rot
    rot -->|yes| doc --> rules
    rot -->|no| rules
    rules --> known
    known -->|yes| label
    known -->|no| llm --> label
```

## Rendering in Sphinx

GitHub renders the blocks above automatically. To render them in the Sphinx
build, add the Mermaid extension:

1. Add the dependency to the docs group in `pyproject.toml`:

   ```toml
   [tool.poetry.group.docs.dependencies]
   sphinxcontrib-mermaid = "^1.0"
   ```

2. Enable it in `docs/conf.py`:

   ```python
   extensions = [
       # ... existing extensions ...
       "sphinxcontrib.mermaid",
   ]
   ```

3. Include this page. If you use MyST-Parser, the ` ```mermaid ` fences above
   render as-is. If your docs are pure reStructuredText, convert each fence to a
   directive instead:

   ```rst
   .. mermaid::

      flowchart TB
          client["Client"] --> nginx["nginx :80"]
   ```
