"""Sphinx configuration."""

from __future__ import annotations

import datetime
import importlib
import inspect
import os
import pathlib
import pkgutil
import sys
from typing import Any

import call_report
from call_report import __version__
from call_report.core import BaseCallReport

# -- Project information ---------------------------------------------------
current_year_int = datetime.datetime.now().year

current_year: str
if current_year_int > 2026:
    current_year = f"2026-{current_year_int}"
else:
    current_year = str(current_year_int)

org = "predict-ably"
project = "call-report"
copyright = f"{current_year}, {project} developers (BSD-3 License)"
author = f"{project} developers"

release = __version__
version = ".".join(release.split(".")[:2])
github_tag = f"v{version}"

# -- Path setup --------------------------------------------------------------

# When we build the docs on readthedocs, we build the package and want to
# use the built files in order for sphinx to be able to properly read the
# Cython files. Hence, we do not add the source code path to the system path.
env_rtd = os.environ.get("READTHEDOCS")
# Check if on Read the docs
if env_rtd != "True":
    print("Not on ReadTheDocs")  # noqa: T201
    sys.path.insert(0, pathlib.Path("../..").resolve().as_posix())
else:
    rtd_version = os.environ.get("READTHEDOCS_VERSION")
    if rtd_version == "latest":
        github_tag = "main"

# -- General configuration ---------------------------------------------------
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    # Executes the User Guide's `doctest` blocks under `sphinx-build -b
    # doctest`. Nothing runs them during an html build, so this is inert
    # for Read the Docs and for the -W html check; see the doctest step in
    # .github/workflows/docs.yml.
    "sphinx.ext.doctest",
    "sphinx.ext.intersphinx",
    "sphinx.ext.linkcode",
    "numpydoc",
    "sphinx_design",
    "sphinx_copybutton",
    "sphinx_issues",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# The main toctree document.
master_doc = "index"

# member-order orders the documentation in the order members are defined in
# source. inherited-members is deliberately NOT enabled globally: several
# public classes are StrEnum subclasses (call_report.core.Quarter, Source,
# FileType; call_report.fca.FCASchedule), and turning it on documents every
# inherited str method (count, find, translate, ...) -- numpydoc then tries
# to validate those built-in docstrings against the numpydoc convention and
# fails outright (some have no retrievable source at all), crashing the
# build with "ExtensionError: ... could not get source code". Classes that
# do need it are opted in one at a time, by the autosummary class
# template, from the set built just below.
autodoc_default_options = {
    "members": True,
    "member-order": "bysource",
}

# Bases whose subclasses are opted into ``:inherited-members:`` one class at
# a time, by the autosummary class template. A BaseCallReport subclass
# implements only the abstract hooks (``_load``, ``_load_all``, ...), so most
# of the public surface a user calls (``load``, ``load_all``,
# ``load_institutions``, ``get_params``, ``set_params``) is defined on the
# base and is invisible on the subclass's own page without it. See issue #47.
inherited_member_bases: tuple[type, ...] = (BaseCallReport,)


def find_inherited_member_classes() -> frozenset[str]:
    """Find the classes whose pages should document inherited members.

    Walks every module in the package and collects the classes deriving
    from one of `inherited_member_bases`, under each name they are a
    public attribute of. Resolving them by `issubclass` rather than
    listing class names means a future source's entry point is picked up
    as soon as it subclasses one of those bases.

    Returns
    -------
    frozenset of str
        Dotted paths, each the module a class is reachable from joined to
        the name it is reachable under. These are the values the
        autosummary class template sees as ``{{ module }}.{{ objname }}``,
        so a class re-exported from a parent package is collected under
        both its defining module and the re-exporting one.
    """
    names: set[str] = set()
    modules = [call_report.__name__]
    modules += [
        module_info.name
        for module_info in pkgutil.walk_packages(
            call_report.__path__, prefix=f"{call_report.__name__}."
        )
    ]
    for module_name in modules:
        module = importlib.import_module(module_name)
        for attr_name, attr in vars(module).items():
            if (
                not attr_name.startswith("_")
                and isinstance(attr, type)
                and issubclass(attr, inherited_member_bases)
            ):
                names.add(f"{module_name}.{attr_name}")
    return frozenset(names)


# A plain frozenset of strings rather than a predicate callable: Sphinx
# pickles autosummary_context into its environment cache and warns on any
# value it cannot pickle, which a function is.
autosummary_context = {
    "inherited_member_classes": find_inherited_member_classes(),
}


# Link to GitHub repo for github_issues extension
issues_github_path = f"{org}/{project}"


def linkcode_resolve(domain: str, info: dict[str, Any]) -> str | None:
    """Return URL to source code corresponding.

    Used to link docs to github code.

    Parameters
    ----------
    domain : str
        Domain for resolving linked code.
    info : dict
        Info about linked code.

    Returns
    -------
    str
        The string version URL to linked code.
    """

    def find_source() -> tuple[Any, int, int]:  # numpydoc ignore=RT01
        """Find the file and line number (if possible).

        Implementation is based on numpy.
        """
        # https://github.com/numpy/numpy/blob/main/doc/source/conf.py#L286
        obj = sys.modules[info["module"]]
        for part in info["fullname"].split("."):
            obj = getattr(obj, part)

        fn = inspect.getsourcefile(obj)
        fn = os.path.relpath(fn, start=pathlib.Path(call_report.__file__).parent)
        source, lineno = inspect.getsourcelines(obj)
        return fn, lineno, lineno + len(source) - 1

    if domain != "py" or not info["module"]:
        return None
    try:
        filename = f"{project}/%s#L%d-L%d" % find_source()
    except Exception:
        filename = info["module"].replace(".", "/") + ".py"
    return f"https://github.com/{org}/{project}/blob/{version_match}/{filename}"


html_theme = "pydata_sphinx_theme"

# This uses code from the py-data-sphinx theme's own conf.py
# Define the version we use for matching in the version switcher.
version_match = os.environ.get("READTHEDOCS_VERSION")

# Only fetch the switcher from the live, hosted URL when actually building on
# ReadTheDocs; every other build (local dev, CI checks pre-deployment) uses
# the relative in-tree path so it never depends on a remote fetch that isn't
# guaranteed to be reachable (or up to date) from an arbitrary build machine.
if env_rtd == "True":
    json_url = "https://call-report.readthedocs.io/en/latest/_static/switcher.json"
else:
    json_url = "_static/switcher.json"

# If READTHEDOCS_VERSION doesn't exist, we're not on RTD
# If it is an integer, we're in a PR build and the version isn't correct.
if not version_match or version_match.isdigit():
    # For local development, infer the version to match from the package.
    version_match = "latest" if ("dev" in release or "rc" in release) else "v" + release

html_show_sourcelink = False

html_theme_options = {
    "logo": {
        "text": "call-report",
        "alt_text": f"{project}",
    },
    "icon_links": [
        {
            "name": "GitHub",
            "url": f"https://github.com/{org}/{project}",
            "icon": "fab fa-github",
        },
        {
            "name": "PyPI",
            "url": f"https://pypi.org/project/{project}",
            "icon": "fa-solid fa-box",
        },
    ],
    "icon_links_label": "Quick Links",
    "show_nav_level": 1,
    "show_prev_next": False,
    "navigation_with_keys": False,
    "use_edit_page_button": False,
    "navbar_start": ["navbar-logo", "version-switcher"],
    "navbar_center": ["navbar-nav"],
    "switcher": {
        "json_url": json_url,
        "version_match": version_match,
    },
    "header_links_before_dropdown": 6,
    "secondary_sidebar_items": {
        "**": ["page-toc", "edit-this-page"],
        "about": [],
        "contribute": [],
        "users": [],
    },
}

html_context = {
    "github_user": f"{org}",
    "github_repo": f"{project}",
    "github_version": "main",
    "doc_path": "docs/source/",
    "default_mode": "light",
}
html_sidebars = {
    "**": ["sidebar-nav-bs.html", "sidebar-ethical-ads.html"],
    "get_started": [],
    "get_involved": [],
    "changelog": [],
}

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ["_static"]

# False because it's a third, independent member-enumeration switch (on top
# of autodoc's inherited-members and the autosummary class.rst template):
# when True, numpydoc injects its own auto-generated Methods/Attributes
# summary into each class docstring via its own dir()-based introspection,
# regardless of autodoc_default_options or the autosummary template -- which
# reintroduces every inherited str method for the StrEnum classes (see the
# autodoc_default_options comment above) and crashes the build the same way.
# Only explicit `.. doctest::` directives are executed by `-b doctest`.
# The default ('default') also runs any literal block that merely looks
# like a doctest, which sweeps in every docstring Examples section autodoc
# renders into the API reference. Those are already executed by pytest's
# --doctest-modules, one namespace per docstring; Sphinx instead shares one
# namespace per document, so a page rendering several methods' examples
# fails on names defined in a sibling example. Docstrings belong to pytest,
# narrative pages to this builder.
doctest_test_doctest_blocks = ""

numpydoc_show_class_members = False
numpydoc_class_members_toctree = False
autosummary_generate = True

# Mirrors [tool.numpydoc_validation] in pyproject.toml (the pre-commit hook's
# config) -- kept in sync deliberately, since this is a separate validator
# with its own config surface: this one runs only at Sphinx build time,
# against whatever autodoc/autosummary actually imports and renders, rather
# than pyproject.toml's static, AST-based pass over the repo's own .py files.
numpydoc_validation_checks = {"all", "GL01", "SA01", "EX01"}
numpydoc_validation_exclude = {
    r"^test_",
    r"\.test_",
    r"\.__init__$",
}

# -- Options for sphinx-copybutton extension----------------------------------
copybutton_prompt_text = r">>> |\.\.\. |\$ |In \[\d*\]: | {2,5}\.\.\.: | {5,8}: "
copybutton_prompt_is_regexp = True
copybutton_selector = ":not(.prompt) > div.highlight pre"

# One entry per dataframe library named in a rendered signature. The
# overloads on every dataframe-returning function annotate the backend types
# by their full import path (pandas.DataFrame, pyarrow.Table, ...), so each
# renders as a link into that library's own reference rather than as inert
# text. narwhals is deliberately absent: it publishes its API reference with
# mkdocs, whose inventory does not carry the module paths this package
# annotates with, so its targets are handled by nitpick_ignore below.
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pandas": ("https://pandas.pydata.org/docs/", None),
    "polars": ("https://docs.pola.rs/api/python/stable/", None),
    "pyarrow": ("https://arrow.apache.org/docs/", None),
}
