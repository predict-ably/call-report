"""Tests that the CI minimum versions pins match the floors in ``pyproject.toml``.

The minimum versions job in ``.github/workflows/test.yml`` installs the pins
in ``.github/minimum-versions.txt``. These tests fail when a floor is raised
in ``pyproject.toml`` without raising its pin, so the job never tests a
version the package no longer claims to support.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]
PINNED_PACKAGES = ("narwhals", "pandas", "polars", "pyarrow")


def _declared_requirements() -> list[Requirement]:
    """Read every requirement in ``dependencies`` and each optional extra."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    lines = list(project["dependencies"])
    for extra in project["optional-dependencies"].values():
        lines.extend(extra)
    return [Requirement(line) for line in lines]


def _pins() -> dict[str, Requirement]:
    """Read the requirements in ``.github/minimum-versions.txt``, keyed by name."""
    text = (REPO_ROOT / ".github" / "minimum-versions.txt").read_text()
    requirements = [
        Requirement(line)
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    return {requirement.name: requirement for requirement in requirements}


def _floor(requirement: Requirement) -> Version:
    """Return the version of the single ``>=`` specifier in `requirement`."""
    (specifier,) = requirement.specifier
    assert specifier.operator == ">=", requirement
    return Version(specifier.version)


@pytest.mark.parametrize("package", PINNED_PACKAGES)
def test_every_declaration_gives_one_floor(package: str) -> None:
    """Each declaration of `package` is a single ``>=`` floor, the same everywhere.

    A package declared in its extra and in ``dev`` with two different floors
    would leave it unclear which one the minimum versions job should pin.
    """
    floors = {
        _floor(requirement)
        for requirement in _declared_requirements()
        if requirement.name == package
    }
    assert len(floors) == 1, floors


@pytest.mark.parametrize("package", PINNED_PACKAGES)
def test_pin_equals_the_declared_floor(package: str) -> None:
    """The pin for `package` is ``==`` the floor declared in ``pyproject.toml``."""
    (floor,) = {
        _floor(requirement)
        for requirement in _declared_requirements()
        if requirement.name == package
    }
    (specifier,) = _pins()[package].specifier
    assert specifier.operator == "=="
    assert Version(specifier.version) == floor


def test_pins_name_only_the_floored_packages() -> None:
    """The pins file pins exactly the packages whose floors it tracks."""
    assert sorted(_pins()) == sorted(PINNED_PACKAGES)
