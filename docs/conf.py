"""Sphinx configuration."""

from __future__ import annotations

from call_report import __version__

project = "call-report"
copyright = "2026, RNKuhns"
author = "RNKuhns"
release = __version__
version = ".".join(release.split(".")[:2])

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "numpydoc",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "pydata_sphinx_theme"

numpydoc_show_class_members = False
autosummary_generate = True

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}
