"""Suite-wide fixtures for the call_report test suite.

Only fixtures and hooks live here. Helpers that a test module imports by
name live in :mod:`tests.helpers`, because pytest loads every
``conftest.py`` itself and importing one as a library gives it two
identities in ``sys.modules``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from call_report.config import config_context, get_config, set_config
from tests.helpers import ALL_BACKENDS


@pytest.fixture(autouse=True)
def reset_config() -> Iterator[None]:
    """Restore the package configuration after every test.

    ``call_report.config`` is process-global (thread-local) mutable state.
    Without this, a test that calls `set_config` and then fails before its
    own cleanup leaves the backend switched for every test that runs after
    it, turning one real failure into a cascade of unrelated ones. Making
    the restore automatic means no test has to remember to do it, and no
    test can be broken by one that ran earlier.
    """
    before = get_config()
    try:
        yield
    finally:
        set_config(**before)


@pytest.fixture(params=ALL_BACKENDS)
def backend(request: pytest.FixtureRequest) -> Iterator[str]:
    """Run the requesting test once per dataframe backend, with it configured.

    Requesting this fixture both parametrizes the test across pandas,
    polars, and pyarrow *and* activates each backend for the whole test
    body, so the code under test sees the same backend the fixture names.
    """
    with config_context(dataframe_backend=request.param):
        yield request.param


@pytest.fixture
def polars_backend() -> Iterator[str]:
    """Configure the polars backend for the whole test body.

    Used by tests that need a backend-specific behavior polars alone
    exhibits, such as an empty column inferring the ``Unknown`` dtype
    rather than a concrete one.
    """
    with config_context(dataframe_backend="polars"):
        yield "polars"


@pytest.fixture
def lazy_polars_backend() -> Iterator[str]:
    """Configure the polars backend in lazy mode for the whole test body."""
    with config_context(dataframe_backend="polars", lazy=True):
        yield "polars"
