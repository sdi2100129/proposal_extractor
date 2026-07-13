Overview
========

``proposal-service`` turns a Horizon Europe proposal PDF into structured,
company-filtered work-package data. A caller uploads a proposal and a target
company acronym; the service locates Section 3 of the proposal, extracts the
work packages, tasks, effort allocations, and deliverables, and returns the
subset that the target company participates in or leads. The result can also
be pushed straight into Microsoft Planner.

This page covers how the pipeline is put together and why. For per-module
detail, see the API reference; for the live HTTP contract, run the service and
open Swagger UI at ``/docs``.


Architecture
------------

The core of the service is a three-stage pipeline. Each stage is defined by an
abstract contract in :mod:`proposal_service.interfaces` and has one concrete
implementation in :mod:`proposal_service.services.implementations`. Because the
stages communicate only through typed containers
(:class:`~proposal_service.interfaces.ProposalTables` and
:class:`~proposal_service.interfaces.ProposalTasks`), a backend can be swapped
— a different PDF library, a different LLM provider — without touching the
others.

The stages are also independently runnable. Each is exposed as its own HTTP
endpoint and as a ``run_stageN`` function in
:mod:`proposal_service.services.pipeline`, so a single stage can be tested or
re-run in isolation without paying the cost of the whole pipeline.


Stage 1 — Table extraction
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:class:`~proposal_service.services.implementations.PdfplumberTableParser`
first locates Section 3 by content heuristics rather than hard-coded page
numbers, so it survives differing proposal layouts. It then scans only those
pages for tables and classifies each one — WP list, effort matrix, or
deliverables — with deterministic rules, falling back to the LLM classifier
only when the rules are unsure. Rotated or stacked-text tables that pdfplumber
mangles are re-parsed through a docling fallback. The stage emits
``wp_info``, ``effort`` (person-months for the target company), and raw
deliverables, plus soft warnings for any table it could not find.


Stage 2 — Task extraction
~~~~~~~~~~~~~~~~~~~~~~~~~~~

:class:`~proposal_service.services.implementations.OllamaTaskExtractor` finds
each work package's prose section and splits it into one chunk per task
heading using a deliberately strict regex, so inline references such as
"...presented to T4.3..." are not mistaken for real task headings. Each chunk
is sent to the Ollama-hosted model in parallel through a thread pool, bounded
by ``ollama_max_workers`` to respect Ollama's own concurrency limit. The work
package id is re-stamped onto every returned task to correct LLM confusion,
and duplicate task ids are de-duplicated by a quality score that prefers tasks
with partners, month ranges, and richer descriptions.


Stage 3 — Assembly
~~~~~~~~~~~~~~~~~~~

:class:`~proposal_service.services.implementations.DefaultAssembler` merges the
Stage 1 tables with the Stage 2 tasks, attaches each deliverable to its closest
matching task, filters everything down to the target company, and assigns a
``leader`` or ``participant`` role per work package and task. The output is a
list of :class:`~proposal_service.models.WorkPackage` domain models.


Design rationale
----------------

A few decisions are worth recording, since they are not obvious from the code
alone:

**Content-based section detection.** Proposal PDFs do not agree on page
numbering or boilerplate, so Section 3 is found by what the text *says*, not
where it sits. This is what lets the same code handle the ARTEMIS and
HyperImage formats without per-proposal tuning.

**Rules first, LLM second.** Table and page classification run deterministic
rules before any model call. The LLM is consulted only on genuinely ambiguous
inputs, which keeps the common path fast, cheap, and reproducible.

**The LLM is scoped narrowly.** Locating and chunking task headings, and
re-stamping work-package ids, are handled deterministically around the model.
The model is used for the parts that are genuinely prose. This both reduces
latency and limits the blast radius of hallucinations.

**Silent failures are guarded against.** A regex mismatch or an over-eager
filter can produce zero output with no error, so intermediate counts are
logged and the de-duplication step keeps the best of any conflicting tasks
rather than dropping them outright.

**Stages are decoupled on purpose.** Independent contracts, independent
endpoints, and opt-in JSON checkpointing make each stage debuggable on its own
and let backends be replaced behind a stable interface.


Request lifecycle
-----------------

Routes in :mod:`proposal_service.api.routes` are intentionally thin. A request:

#. is authenticated against Keycloak (a bearer JWT, validated in
   :mod:`proposal_service.auth`);
#. has its form parameters validated by Pydantic
   (:class:`~proposal_service.schemas.ProposalParams`);
#. has its uploaded PDF written to a temporary file that is always cleaned up;
#. runs each pipeline stage inside a threadpool so the event loop stays
   responsive; and
#. is serialised back through a typed response model.

Business logic never lives in the route handlers. Errors are normalised by the
global exception handlers in :mod:`proposal_service.main` — a missing
Section 3 becomes ``422``, an unreachable model becomes ``503``, and anything
unexpected becomes a generic ``500`` with the traceback logged but not leaked.


Deployment
----------

The service ships as a Docker Compose stack: the API, an Ollama instance (with
a one-shot init container that pulls the model), nginx as a reverse proxy,
Keycloak for authentication, and a PostgreSQL database backing Keycloak.

Prerequisites: the Ollama model volume and the PostgreSQL volume are declared
as **external**, so they survive renames and rebuilds. Confirm they exist
before the first bring-up:

.. code-block:: bash

   docker volume ls | grep proposal_extractor

Bring the stack up:

.. code-block:: bash

   cp .env.example .env
   # edit .env: KEYCLOAK_PUBLIC_URL, admin password, DB credentials, etc.
   make docker-up

Once running, the API is reachable through nginx on ``http://localhost``,
Swagger UI at ``http://localhost/docs``, and Keycloak at
``http://localhost:8081``.

Configuration follows a single precedence: environment variables override
``config.ini`` defaults, and everything is read through the typed
:class:`~proposal_service.config.Settings` object — no module reads
``os.environ`` directly.


Operational notes
-----------------

A few things to keep in mind when operating or tuning the stack:

* **Worker count is not free.** Each API worker loads its own docling model and
  competes for Ollama, which caps its own parallelism. A naive CPU-count
  default can exhaust memory; size workers against the Ollama limit, not the
  core count.
* **Rebuild after code changes.** The container runs the built image, so a code
  edit only takes effect after ``docker compose build`` (or ``--build``). A
  bare file save changes nothing in the running container.
* **Keycloak runs in development mode.** The Compose file starts Keycloak with
  ``start-dev`` and publishes its port. This is convenient locally but should
  be switched to production ``start`` and have its host port closed before any
  real deployment.
