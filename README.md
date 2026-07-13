# proposal-service

A FastAPI service that turns a Horizon Europe proposal PDF into structured
work-package data and pushes it to a project-management tool (Microsoft
Planner or GitHub Issues).

The core is a three-stage extraction pipeline:

1. **Table extraction** — pdfplumber, with a docling fallback for rotated
   tables. Locates Section 3 by content heuristics (no hard-coded page
   numbers) and pulls out the work-package list, the per-partner effort
   summary, and the deliverables table.
2. **Task extraction** — chunks each WP's prose section by task heading and
   runs the chunks through an Ollama-hosted LLM (`qwen2.5:3b`) in parallel
   (capped at `OLLAMA_MAX_WORKERS`, default 4). The WP id is re-stamped after
   extraction to correct any model confusion.
3. **Assembly** — merges the parsed tables with the extracted tasks into a
   list of `WorkPackage` objects, filtered to the targeted company, with
   deliverables attached to their closest matching tasks.

Each stage is also exposed as its own HTTP endpoint, so it can be tested or
re-run independently.

The pipeline can be run two ways:

* **Synchronously** under `/proposal/*` — best for small proposals and
  debugging individual stages.
* **Asynchronously** under `/api/v1/extractions/*` — a Redis + arq job queue
  that accepts the upload, runs the pipeline in a background worker, and
  streams live progress over Server-Sent Events. This is the right path for
  large PDFs, where holding an HTTP connection open for the whole pipeline is
  impractical.

The assembled result can be returned as JSON, or pushed out as a real plan to
**Microsoft Planner** (Graph API) or **GitHub Issues** (REST API).


## Project tree

```text
proposal-service/
├── config.ini                  # non-secret defaults
├── .env.example                # documented env vars
├── pyproject.toml              # metadata + tool config (Poetry)
├── Makefile                    # dev shortcuts
├── Dockerfile
├── docker-compose.yml          # api + worker + redis + ollama + keycloak + nginx + postgres
├── gunicorn_conf.py            # worker count, timeouts, logging
├── nginx.conf
├── requirements.txt            # consumed by Docker
├── keycloak/import/
│   └── proposal-realm.json     # realm imported on first start
├── src/proposal_service/
│   ├── __init__.py
│   ├── main.py                 # app factory, lifespan, middleware, handlers
│   ├── config.py               # typed Settings (pydantic-settings)
│   ├── logging_setup.py        # UTC ISO 8601 root logger
│   ├── auth.py                 # Keycloak JWT verification
│   ├── models.py               # WorkPackage, Task, Deliverable
│   ├── schemas.py              # API request/response models
│   ├── interfaces.py           # stage ABCs + result containers
│   ├── utils.py                # pure helpers (text + timeline)
│   ├── worker.py               # arq worker entrypoint (async pipeline runner)
│   ├── api/
│   │   ├── routes.py           # synchronous /proposal handlers
│   │   └── routes_extractions.py  # async /api/v1/extractions handlers (SSE)
│   └── services/
│       ├── pipeline.py         # run_stage1 / run_stage2 / run_stage3
│       ├── implementations.py  # concrete TableParser/TaskExtractor/Assembler
│       ├── pdf_locator.py      # find Section 3, WP sections, tables
│       ├── table_parser.py     # parse WP list / effort / deliverables
│       ├── task_extractor.py   # Ollama prompts + validators
│       ├── assembler.py        # merge + filter + role assignment
│       ├── jobs.py             # Redis-backed job state + SSE event bus
│       ├── planner_adapter.py  # WorkPackage -> Planner payload
│       ├── planner_client.py   # Microsoft Graph API client
│       ├── github_adapter.py   # WorkPackage -> GitHub milestones/labels/issues
│       └── github_client.py    # GitHub REST API client (idempotent)
└── tests/
    ├── conftest.py
    ├── test_api.py
    └── test_units.py
```


## Endpoints

All routes require a Keycloak bearer token (see Swagger UI at `/docs`).
The `/proposal/*` and async POST routes accept a multipart upload (`pdf`)
plus form fields (`company`, `start_date`). Empty form fields fall back to
the defaults in `config.ini`.

### Synchronous pipeline

| Method | Path                          | Description                                       |
| ------ | ----------------------------- | ------------------------------------------------- |
| GET    | `/health`                     | Liveness probe                                    |
| POST   | `/proposal/stage1`            | Run table extraction only                         |
| POST   | `/proposal/stage2`            | Run LLM task extraction only                      |
| POST   | `/proposal`                   | Run all three stages, return assembled WPs        |
| POST   | `/proposal/planner-payload`   | Build the Planner payload without pushing it      |
| POST   | `/proposal/planner`           | Push the proposal as a new Microsoft Planner plan |
| POST   | `/proposal/github-payload`    | Build the GitHub payload without pushing it       |
| POST   | `/proposal/github`            | Push the proposal as GitHub milestones + issues   |

`/proposal/planner` additionally requires `owner_group_id` (M365 group) and
optionally `plan_title`. It returns `503` if the `MS_*` env vars are not set.

`/proposal/github` additionally takes `owner`, `repo`, and `plan_title`
(falling back to `GITHUB_OWNER` / `GITHUB_REPO`). It returns `503` if the
GitHub token is not configured.

### Asynchronous job queue

Mounted under `/api/v1/extractions`. Use this for large proposals.

| Method | Path                                  | Description                                          |
| ------ | ------------------------------------- | ---------------------------------------------------- |
| POST   | `/api/v1/extractions/work-packages`   | Queue an extraction job; returns `202` + status URLs |
| GET    | `/api/v1/extractions/{job_id}`        | Current status, progress percent, and result URL     |
| GET    | `/api/v1/extractions/{job_id}/events` | Server-Sent Events stream of live progress           |
| GET    | `/api/v1/extractions/{job_id}/result` | Assembled work packages once the job is `completed`  |

The flow is: `POST` persists the upload to a shared volume, records a queued
job in Redis, and enqueues an arq task → the worker runs the pipeline off the
request path, publishing progress as it goes → the client polls `GET {job_id}`
or subscribes to the SSE stream, then fetches the result. Job state and
results carry a TTL, so abandoned jobs expire without a reaper.

> **Note:** Swagger UI cannot render SSE streams. Test the events endpoint
> with `curl -N -H "Authorization: Bearer <token>" .../events`.


## Output integrations

Both integrations follow the same adapter/client split: an `*_adapter`
converts `WorkPackage` objects into a backend-agnostic payload, and a
`*_client` talks to the remote API.

### Microsoft Planner (Graph API)

Authenticates with the client-credentials flow (`MS_TENANT_ID`,
`MS_CLIENT_ID`, `MS_CLIENT_SECRET`) and creates a plan owned by an M365 group.

### GitHub Issues (REST API)

Implemented as a Planner substitute for environments without MS Teams
permissions. One shared repository hosts many proposals. The mapping:

| Proposal element | GitHub element                                  |
| ---------------- | ----------------------------------------------- |
| Work package     | Milestone (WP end month → milestone due date)   |
| Task             | Issue (assigned to the WP's milestone)          |
| Deliverables     | Markdown task-list checkboxes in the issue body |
| Partners         | Labels on the issue                             |
| Company role     | A `role:*` label                                |

Authentication is a single fine-grained PAT. Milestone and issue titles are
prefixed with the proposal title, and every issue carries a `proposal:<slug>`
label so a whole proposal can be filtered out of the shared repo. All create
operations are **lookup-or-create**, so re-running a push is idempotent —
existing labels/milestones are reused and matching issues are skipped rather
than duplicated.


## Configuration

Three sources, highest precedence first:

1. Environment variables.
2. `.env` file in the working directory (developer machines only).
3. `config.ini` non-secret defaults shipped with the service.

The single source of truth at runtime is the typed `Settings` object returned
by `proposal_service.config.get_settings()`. **No module reads `os.environ`
directly.** Required settings (`KEYCLOAK_URL`, `KEYCLOAK_PUBLIC_URL`) are
validated at startup so a misconfigured deployment fails loudly before
serving traffic.

Key environment variables:

| Variable                         | Purpose                                          |
| -------------------------------- | ------------------------------------------------ |
| `COMPANY`, `PROPOSAL_START_DATE` | Pipeline defaults                                |
| `OLLAMA_URL`, `OLLAMA_MODEL`     | LLM backend                                      |
| `OLLAMA_MAX_WORKERS`             | Concurrent LLM requests (default 4)              |
| `KEYCLOAK_URL`, `KEYCLOAK_PUBLIC_URL`, `KEYCLOAK_REALM` | OIDC auth               |
| `REDIS_URL`                      | Job queue + state backend                        |
| `JOB_UPLOAD_DIR`                 | Shared volume for queued uploads                 |
| `MS_TENANT_ID` / `MS_CLIENT_ID` / `MS_CLIENT_SECRET` | Planner push (optional)      |
| `GITHUB_TOKEN` / `GITHUB_OWNER` / `GITHUB_REPO` | GitHub push (optional)            |
| `GUNICORN_WORKERS`               | Override worker count (see Performance notes)    |


## Running locally

### With Docker (recommended)

```bash
cp .env.example .env
# edit .env: KEYCLOAK_PUBLIC_URL, KC_ADMIN_PASSWORD, integration creds, etc.
docker compose up -d
```

The Compose stack runs the API (Gunicorn behind nginx), the arq **worker**,
**Redis**, Ollama (with a one-shot init container that pulls the model),
Keycloak, and its PostgreSQL backing store.

* API via nginx — `http://localhost`
* Swagger UI — `http://localhost/docs`
* Keycloak — `http://localhost:8081`

> Adding a new variable to a `environment:` block requires
> `docker compose up -d` (no rebuild). **Editing source requires a rebuild**
> (`docker compose up -d --build api worker`) because the code is baked into
> the image at build time.

### Without Docker

```bash
make install         # runtime + dev deps in editable mode
make run-dev         # uvicorn with --reload
arq proposal_service.worker.WorkerSettings   # the queue worker, separately
```

You will need an Ollama instance at `OLLAMA_URL`, a Redis instance at
`REDIS_URL`, and a Keycloak instance at `KEYCLOAK_URL`.


## Development workflow

```bash
make install         # one-off
make format          # black + isort
make lint            # ruff
make typecheck       # mypy (strict-ish; see pyproject)
make test            # pytest with coverage
make help            # full target list
```

Documentation is built with Sphinx (autodoc, napoleon, autosummary,
autodoc_pydantic, Furo theme). The OpenAPI contract is served separately at
`/docs` (Swagger UI) and `/redoc`.


## Testing notes

The unit suite stubs `pdfplumber` and `docling` at collection time so it runs
without the full PDF stack, and isolates `Settings` via `monkeypatch` so tests
never read the real `.env` or reach external services.

Tests that touch real services (the Ollama pipeline, the GitHub API) are
marked `integration` and excluded from the per-commit run:

```bash
pytest -m "not integration"     # fast unit suite (CI per-commit)
pytest -m integration           # requires Ollama/Keycloak/etc. (nightly)
```

Global exception handlers are exercised via
`TestClient(app, raise_server_exceptions=False)` — see `tests/conftest.py`.

CI is a declarative Jenkinsfile on a `python:3.12-slim` agent: parallel
lint/typecheck stages, JUnit + Cobertura reporting, and Docker build/push
gated to `main` and tags. The integration suite is kept out of the
per-commit pipeline.


## Performance notes

* **Worker count.** The Gunicorn CPU-count default (e.g. 16) is wrong for
  this workload: Ollama is capped at `OLLAMA_NUM_PARALLEL=4` and docling loads
  ML models *per worker*. Use 2–4 workers via `GUNICORN_WORKERS`.
* **docling model cache.** Worker recycling (`max_requests`) discards the
  cached docling models and forces a reload — weigh that overhead against
  memory stability before enabling it.
* **Async worker amortizes model load.** On the queue path, docling models
  load lazily once per worker process and are reused across all jobs, unlike
  the per-Gunicorn-worker loading on the synchronous path.


## Design principle: prefer regex-first for structured data

The LLM's failure mode is **silent and confident** — it hallucinates
plausible-but-wrong values for structured fields (partner acronyms, month
ranges) that are undetectable downstream. Deterministic parsing that fails on
an unrecognized format instead produces *empty* fields, which are visible and
actionable. Where a value can be parsed from structured heading data, parse it
deterministically and fall back to the LLM only for free-form prose.