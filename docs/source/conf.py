"""Sphinx configuration for the proposal-service documentation.

Builds an HTML reference site from the package's docstrings and Pydantic
models. Run from the repo root with ``make docs`` (see the project Makefile)
or directly with ``sphinx-build -b html docs/source docs/build/html``.

Notes:
    * ``autodoc`` imports ``proposal_service`` to read docstrings, so the
      package must be importable when the docs build (``poetry install``
      first, or build inside the project venv).
    * ``autodoc_pydantic`` renders ``models.py`` / ``schemas.py`` with their
      ``Field(description=...)`` text, validators, and constraints.
    * ``napoleon`` parses the Google-style docstrings used across the
      codebase (Args/Returns/Raises sections).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

# Make the package importable. This file lives at docs/source/conf.py, so the
# repo root is three levels up; the package lives under src/.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))


# Project information
project = "proposal-service"
author = "Proposal Service Team"
copyright = f"{datetime.now(timezone.utc):%Y}, {author}"

# Pull the version from the installed package; fall back gracefully so a
# docs build never hard-fails just because the package isn't installed.
try:
    from proposal_service import __version__ as release
except Exception:  # pragma: no cover - docs build convenience only
    release = "0.0.0"
version = ".".join(release.split(".")[:2])


# General configuration
extensions = [
    "sphinx.ext.autodoc",          # pull docstrings from the codebase
    "sphinx.ext.napoleon",         # understand Google-style docstrings
    "sphinx.ext.viewcode",         # add [source] links to rendered objects
    "sphinx.ext.intersphinx",      # cross-link to Python / Pydantic / FastAPI
    "sphinx.ext.autosummary",      # generate per-module summary tables
    "sphinxcontrib.autodoc_pydantic",  # rich rendering of Pydantic models
]

templates_path = ["_templates"]
exclude_patterns: list[str] = []

# Generate autosummary stub pages automatically.
autosummary_generate = True


# Autodoc behaviour
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
    "member-order": "bysource",
}
# Resolve `str | None`-style annotations and keep them readable.
autodoc_typehints = "description"
autodoc_typehints_description_target = "documented"
# Don't execute settings/config side effects at import where avoidable.
autodoc_mock_imports = [
    # Heavy / environment-specific deps that need not be installed just to
    # render docstrings. Remove an entry if you want its members documented.
    "pdfplumber",
    "docling",
    "jose",
]


# autodoc_pydantic behaviour
autodoc_pydantic_model_show_json = False
autodoc_pydantic_model_show_config_summary = False
autodoc_pydantic_model_show_validator_summary = True
autodoc_pydantic_model_show_field_summary = True
autodoc_pydantic_field_list_validators = True
autodoc_pydantic_model_member_order = "bysource"
autodoc_pydantic_field_show_constraints = True
autodoc_pydantic_field_doc_policy = "docstring"

# Napoleon (Google-style docstrings)
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = False
napoleon_use_param = True
napoleon_use_rtype = True

suppress_warnings = ["autodoc", "autosummary"]

# Intersphinx
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pydantic": ("https://docs.pydantic.dev/latest/", None),
    "fastapi": ("https://fastapi.tiangolo.com/", None),
}


# HTML output
html_theme = "furo"  # clean, modern; swap for 'alabaster' to avoid the dep
html_static_path = ["_static"]
html_title = f"{project} {version}"

