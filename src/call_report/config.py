"""Package-level, sklearn-style configuration for the dataframe backend.

Every reader in this package builds frames through narwhals and returns a
*native* frame of whichever backend is configured here. That means a real
``pandas.DataFrame``, ``polars.DataFrame``/``polars.LazyFrame``, or
``pyarrow.Table``, never a narwhals wrapper.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

DataFrameBackend = Literal["pandas", "polars", "pyarrow"]

_SUPPORTED_BACKENDS: frozenset[str] = frozenset({"pandas", "polars", "pyarrow"})
_LAZY_CAPABLE_BACKENDS: frozenset[str] = frozenset({"polars"})
_DEFAULT_CONFIG: dict[str, Any] = {"dataframe_backend": "pandas", "lazy": False}

_local = threading.local()


def _current_config() -> dict[str, Any]:
    """Return this thread's mutable config dict, creating it on first use.

    The returned dict is the live, mutable store for the calling thread.
    Callers that want an isolated snapshot should use `get_config` instead.

    Returns
    -------
    dict[str, Any]
        The current thread's configuration dict, keyed by
        ``"dataframe_backend"`` and ``"lazy"``.
    """
    if not hasattr(_local, "config"):
        _local.config = dict(_DEFAULT_CONFIG)
    return _local.config


def get_config() -> dict[str, Any]:
    """Return a copy of the current thread's package configuration.

    Every reader in the package consults this configuration to decide which
    dataframe library and eagerness to build its return value with.

    Returns
    -------
    dict[str, Any]
        A copy with keys ``"dataframe_backend"`` and ``"lazy"``. Mutating
        the returned dict has no effect on the package configuration.

    Examples
    --------
    >>> get_config()
    {'dataframe_backend': 'pandas', 'lazy': False}
    """
    return dict(_current_config())


def set_config(
    *, dataframe_backend: DataFrameBackend | None = None, lazy: bool | None = None
) -> None:
    """Update the current thread's package configuration.

    Only the keys explicitly passed are changed. Omitted keys keep their
    current value. The change is thread-local, so it never affects other
    threads.

    Parameters
    ----------
    dataframe_backend : {"pandas", "polars", "pyarrow"}, optional
        The dataframe library that readers should return native frames for.
    lazy : bool, optional
        If ``True``, readers return a lazy frame (currently only supported
        for the ``"polars"`` backend, via ``polars.LazyFrame``).

    Raises
    ------
    ValueError
        If `dataframe_backend` is not a supported backend name, or if the
        resulting `dataframe_backend`/`lazy` combination is unsupported
        (e.g. ``lazy=True`` with the ``"pandas"`` backend).

    Examples
    --------
    >>> set_config(dataframe_backend="polars")
    >>> get_config()["dataframe_backend"]
    'polars'
    >>> set_config(dataframe_backend="pandas")
    """
    config = _current_config()
    new_backend = (
        dataframe_backend
        if dataframe_backend is not None
        else config["dataframe_backend"]
    )
    new_lazy = lazy if lazy is not None else config["lazy"]

    if dataframe_backend is not None and dataframe_backend not in _SUPPORTED_BACKENDS:
        raise ValueError(
            f"dataframe_backend must be one of {sorted(_SUPPORTED_BACKENDS)}, "
            f"got {dataframe_backend!r}"
        )
    if new_lazy and new_backend not in _LAZY_CAPABLE_BACKENDS:
        raise ValueError(
            f"lazy=True is only supported for backends "
            f"{sorted(_LAZY_CAPABLE_BACKENDS)}, got dataframe_backend={new_backend!r}"
        )

    config["dataframe_backend"] = new_backend
    config["lazy"] = new_lazy


@contextmanager
def config_context(**kwargs: Any) -> Iterator[None]:
    """Temporarily change the package configuration within a ``with`` block.

    The previous configuration is restored on exit, including when the
    block raises.

    Parameters
    ----------
    **kwargs : Any
        Same keyword arguments as `set_config`.

    Yields
    ------
    None
        Nothing is yielded. The block runs with the updated configuration
        active.

    Examples
    --------
    >>> with config_context(dataframe_backend="polars"):
    ...     get_config()["dataframe_backend"]
    'polars'
    >>> get_config()["dataframe_backend"]
    'pandas'
    """
    before = get_config()
    set_config(**kwargs)
    try:
        yield
    finally:
        set_config(**before)
