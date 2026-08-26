"""Tests for the Sphinx configuration in ``docs/source/conf.py``.

These guard the inherited-member handling, which is easy to break silently:
autodoc simply omits the members and the build still succeeds. See issue
#47, where ``FCACallReport.load`` and ``FCACallReport.load_all`` were
missing from the rendered API page because both are defined on
``BaseCallReport`` rather than on the subclass.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from call_report.core import BaseCallReport

DOCS_SOURCE = Path(__file__).resolve().parents[2] / "docs" / "source"
CLASS_TEMPLATE = DOCS_SOURCE / "_templates" / "autosummary" / "class.rst"

# An autosummary entry is an indented dotted path inside a directive body.
_AUTOSUMMARY_ENTRY = re.compile(r"^\s+(call_report\.[\w.]+)\s*$", re.MULTILINE)


def _resolve(dotted_path: str) -> object:
    """Resolve a dotted path to the object an autosummary entry names.

    Parameters
    ----------
    dotted_path : str
        Full dotted path, such as ``call_report.fca.FCACallReport``.

    Returns
    -------
    object
        The module or module attribute the path names.
    """
    try:
        return importlib.import_module(dotted_path)
    except ImportError:
        module_name, _, attr_name = dotted_path.rpartition(".")
        return getattr(importlib.import_module(module_name), attr_name)


@pytest.fixture
def sphinx_conf(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import ``docs/source/conf.py`` without leaving it in ``sys.modules``.

    ``READTHEDOCS`` is set so the import takes the branch that leaves
    ``sys.path`` alone, keeping the import free of global side effects.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Used to set ``READTHEDOCS`` for the duration of the test.

    Returns
    -------
    ModuleType
        The executed configuration module.
    """
    monkeypatch.setenv("READTHEDOCS", "True")
    spec = importlib.util.spec_from_file_location(
        "call_report_docs_conf", DOCS_SOURCE / "conf.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_api_reference_classes_opt_in_exactly_when_they_subclass_the_base(
    sphinx_conf: ModuleType,
) -> None:
    """Every API-reference class is opted in iff it subclasses BaseCallReport.

    Stated as an equivalence so it holds for a source added later: a new
    entry point is covered as soon as it subclasses ``BaseCallReport``,
    and the StrEnum classes stay out, since documenting their inherited
    ``str`` members crashes the numpydoc validation pass.
    """
    opted_in = sphinx_conf.autosummary_context["inherited_member_classes"]
    api_reference = (DOCS_SOURCE / "api_reference.rst").read_text(encoding="utf-8")

    documented_classes = {
        dotted_path: obj
        for dotted_path in _AUTOSUMMARY_ENTRY.findall(api_reference)
        if isinstance(obj := _resolve(dotted_path), type)
    }
    assert documented_classes, "no classes found in api_reference.rst"

    for dotted_path, obj in documented_classes.items():
        assert (dotted_path in opted_in) is issubclass(obj, BaseCallReport), dotted_path


def test_inherited_public_methods_are_reachable_on_the_subclass(
    sphinx_conf: ModuleType,
) -> None:
    """A subclass inheriting public methods is opted in.

    Without this, methods such as ``load`` and ``load_all`` render on
    ``BaseCallReport`` only, so a user reading ``FCACallReport``'s page
    never sees them.
    """
    from call_report.fca import FCACallReport

    inherited = {
        name
        for name in vars(BaseCallReport)
        if not name.startswith("_") and name not in vars(FCACallReport)
    }
    assert {"load", "load_all"} <= inherited
    assert (
        "call_report.fca.FCACallReport"
        in sphinx_conf.autosummary_context["inherited_member_classes"]
    )


def test_class_template_reads_the_configured_context_variable(
    sphinx_conf: ModuleType,
) -> None:
    """The template's condition names a key ``autosummary_context`` supplies.

    A rename on either side fails open: autosummary renders the stub with
    an undefined name, which Jinja evaluates as falsy, so the option is
    dropped and the build still succeeds.
    """
    template = CLASS_TEMPLATE.read_text(encoding="utf-8")
    assert ":inherited-members:" in template

    referenced = set(re.findall(r"{%-?\s*if\s+\S+\s+in\s+(\w+)\s*%}", template))
    assert referenced
    assert referenced <= set(sphinx_conf.autosummary_context)
