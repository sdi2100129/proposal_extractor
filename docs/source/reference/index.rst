API reference
=============

Generated from the package docstrings.

Domain models and API schemas
-----------------------------

.. autosummary::
   :toctree: generated
   :recursive:

   proposal_service.models
   proposal_service.schemas

Pipeline contracts and orchestration
-------------------------------------

.. autosummary::
   :toctree: generated
   :recursive:

   proposal_service.interfaces
   proposal_service.services.pipeline
   proposal_service.services.implementations
   

Stage internals
---------------

.. autosummary::
   :toctree: generated
   :recursive:

   proposal_service.services.pdf_locator
   proposal_service.services.table_parser
   proposal_service.services.task_extractor
   proposal_service.services.assembler

Planner integration
-------------------

.. autosummary::
   :toctree: generated
   :recursive:

   proposal_service.services.planner_adapter
   proposal_service.services.planner_client

Application wiring
------------------

.. autosummary::
   :toctree: generated
   :recursive:

   proposal_service.main
   proposal_service.api.routes
   proposal_service.config
   proposal_service.auth

Async worker and job queue
---------------------------

.. autosummary::
   :toctree: generated
   :recursive:

   proposal_service.worker
   proposal_service.jobs
   proposal_service.api.routes_extraction