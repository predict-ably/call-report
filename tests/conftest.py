"""Suite-wide fixtures for the call_report test suite.

Only fixtures and hooks live here. Helpers that a test module imports by
name live in :mod:`tests.helpers`, because pytest loads every
``conftest.py`` itself and importing one as a library gives it two
identities in ``sys.modules``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, settings

from call_report.config import config_context, get_config, set_config
from tests.helpers import ALL_BACKENDS

# hypothesis fails an example that exceeds a 200ms deadline. That is a poor
# fit here: a property test that builds a dataframe pays the backend's
# import cost on its first example and microseconds thereafter, so the
# deadline fires on the timing spread rather than on anything about the
# code under test. Observed locally at 297ms for a first example against
# 2.76ms for the next, and CI runners are slower and more variable still.
#
# The `too_slow` health check is suppressed for the same reason: building
# frames per example is inherently slower than generating integers, and
# that is the work these tests exist to do.
settings.register_profile(
    "call_report",
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("call_report")


_EXHAUSTIVE_FLAG = "--run-exhaustive"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the flag that opts in to the exhaustive archive regression.

    The exhaustive tests load every archived FCA release against every
    dataframe backend. That is minutes of work rather than seconds, so it
    is gated behind a flag rather than a marker alone: a marker can be
    selected by accident with the wrong ``-m`` expression, while an
    unpassed flag cannot.

    Parameters
    ----------
    parser : pytest.Parser
        The parser pytest passes to this hook.
    """
    parser.addoption(
        _EXHAUSTIVE_FLAG,
        action="store_true",
        default=False,
        help=(
            "Run the exhaustive archive regression: every published FCA "
            "release against every dataframe backend. Takes minutes."
        ),
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip exhaustive-marked tests unless the opt-in flag was passed.

    The tests stay collected either way, so ``--collect-only`` still shows
    what the exhaustive run would cover.

    Parameters
    ----------
    config : pytest.Config
        The active configuration, consulted for the opt-in flag.
    items : list[pytest.Item]
        The collected items, marked in place.
    """
    if config.getoption(_EXHAUSTIVE_FLAG):
        return
    skip_exhaustive = pytest.mark.skip(reason=f"pass {_EXHAUSTIVE_FLAG} to run")
    for item in items:
        if "exhaustive" in item.keywords:
            item.add_marker(skip_exhaustive)


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
