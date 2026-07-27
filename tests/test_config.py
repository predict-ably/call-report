"""Tests for call_report.config.

The config follows the sklearn-style package configuration.
"""

from __future__ import annotations

import threading

import pytest

from call_report.config import config_context, get_config, set_config


def test_default_config() -> None:
    """Defaults are pandas backend, eager (non-lazy) frames."""
    config = get_config()
    assert config["dataframe_backend"] == "pandas"
    assert config["lazy"] is False


def test_set_config_updates_backend() -> None:
    """set_config mutates only the keys it is given."""
    set_config(dataframe_backend="pyarrow")
    try:
        assert get_config()["dataframe_backend"] == "pyarrow"
        assert get_config()["lazy"] is False
    finally:
        set_config(dataframe_backend="pandas")


def test_set_config_rejects_unknown_backend() -> None:
    """An unsupported backend name raises a clear, actionable error."""
    with pytest.raises(ValueError, match="dataframe_backend"):
        set_config(dataframe_backend="not-a-real-backend")  # type: ignore[arg-type]


def test_set_config_rejects_lazy_for_non_lazy_capable_backend() -> None:
    """lazy=True is rejected for backends that narwhals can't make lazy (yet)."""
    with pytest.raises(ValueError, match="lazy"):
        set_config(dataframe_backend="pandas", lazy=True)


def test_set_config_accepts_lazy_for_polars() -> None:
    """Polars is the one shipped lazy-capable backend."""
    set_config(dataframe_backend="polars", lazy=True)
    try:
        config = get_config()
        assert config["dataframe_backend"] == "polars"
        assert config["lazy"] is True
    finally:
        set_config(dataframe_backend="pandas", lazy=False)


def test_set_config_switching_backend_away_from_polars_requires_clearing_lazy() -> None:
    """Switching to a non-lazy-capable backend while lazy=True is still set errors."""
    set_config(dataframe_backend="polars", lazy=True)
    try:
        with pytest.raises(ValueError, match="lazy"):
            set_config(dataframe_backend="pandas")
    finally:
        set_config(dataframe_backend="pandas", lazy=False)


def test_config_context_restores_previous_config() -> None:
    """The context manager restores the prior config on normal exit."""
    before = get_config()
    with config_context(dataframe_backend="pyarrow"):
        assert get_config()["dataframe_backend"] == "pyarrow"
    assert get_config() == before


def test_config_context_restores_previous_config_on_exception() -> None:
    """The context manager restores the prior config even if the body raises."""
    before = get_config()

    class _BoomError(Exception):
        pass

    with pytest.raises(_BoomError), config_context(dataframe_backend="pyarrow"):
        assert get_config()["dataframe_backend"] == "pyarrow"
        raise _BoomError

    assert get_config() == before


def test_config_context_supports_nesting() -> None:
    """Nested config_context calls restore each outer layer correctly."""
    set_config(dataframe_backend="pandas")
    with config_context(dataframe_backend="pyarrow"):
        with config_context(dataframe_backend="polars"):
            assert get_config()["dataframe_backend"] == "polars"
        assert get_config()["dataframe_backend"] == "pyarrow"
    assert get_config()["dataframe_backend"] == "pandas"


def test_config_is_thread_local() -> None:
    """Config changes on one thread must not leak into another thread."""
    set_config(dataframe_backend="pandas")
    seen: dict[str, str] = {}

    def worker() -> None:
        set_config(dataframe_backend="polars")
        seen["worker"] = get_config()["dataframe_backend"]

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen["worker"] == "polars"
    assert get_config()["dataframe_backend"] == "pandas"


def test_set_config_no_args_is_a_noop() -> None:
    """Calling set_config with no arguments changes nothing."""
    set_config(dataframe_backend="pyarrow")
    try:
        before = get_config()
        set_config()
        assert get_config() == before
    finally:
        set_config(dataframe_backend="pandas")


def test_config_functions_are_keyword_only() -> None:
    """set_config takes no positional arguments."""
    with pytest.raises(TypeError):
        set_config("pandas")  # type: ignore[call-arg]
