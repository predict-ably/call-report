"""Lazy imports and optional-dependency helpers.

These utilities let the package treat any third-party library other than
``narwhals`` as optional: such a module is imported only when a feature that
needs it is actually used, and a missing or too-old dependency produces a
clear, actionable error naming the ``pip install`` to run rather than an
opaque failure deep in a call stack. The design mirrors the approach polars
uses in its ``polars/_dependencies.py`` module.
"""

from __future__ import annotations

import re
import sys
from importlib import import_module
from importlib.util import find_spec
from types import ModuleType
from typing import Any, ClassVar


class ModuleUpgradeRequiredError(ImportError):
    """An optional dependency is installed but older than the required minimum.

    Raised by :func:`import_optional` when the caller passes ``min_version``
    and the installed module reports a lower version. It subclasses
    :class:`ImportError` so callers can handle import problems uniformly.
    """


class _LazyModule(ModuleType):
    """A proxy module that defers importing the real module until first use.

    Accessing any attribute triggers the real import when the module is
    available, or raises a helpful :class:`ModuleNotFoundError` when it is
    not. This lets calling code hold a reference to an optional module
    unconditionally: no error occurs unless the module is actually used. The
    proxy is deliberately not registered in :data:`sys.modules`, so it never
    shadows the real module elsewhere in the interpreter.

    Parameters
    ----------
    module_name : str
        Import name of the module to proxy, e.g. ``"pandas"``.
    module_available : bool
        Whether the real module is importable. When ``False`` the proxy
        raises on use instead of importing.
    """

    __lazy__ = True

    _module_prefixes: ClassVar[dict[str, str]] = {
        "numpy": "np.",
        "pandas": "pd.",
        "polars": "pl.",
        "pyarrow": "pa.",
    }

    def __init__(self, module_name: str, *, module_available: bool) -> None:
        self._module_available = module_available
        self._module_name = module_name
        self._globals: dict[str, Any] = globals()
        super().__init__(module_name)

    def _import(self) -> ModuleType:
        """Import the real module and adopt its contents.

        Replaces this proxy in the defining module's globals with the real
        module and copies its attributes onto the proxy, so later access is
        served directly without repeating the import.

        Returns
        -------
        types.ModuleType
            The imported real module.
        """
        module = import_module(self._module_name)
        self._globals[self._module_name] = module
        self.__dict__.update(module.__dict__)
        return module

    def __getattr__(self, name: str) -> Any:
        """Resolve an attribute, importing the real module on demand.

        Triggers the deferred import on the first real attribute access, or
        raises a helpful error when the proxied module is not installed.

        Parameters
        ----------
        name : str
            The attribute being accessed on the proxy.

        Returns
        -------
        Any
            The attribute from the real module, or ``None`` for harmless
            dunder lookups when the module is unavailable.

        Raises
        ------
        AttributeError
            For ``__wrapped__`` access, so the proxy is not mistaken for a
            wrapped callable.
        ModuleNotFoundError
            When the module is unavailable and a real attribute is requested.
        """
        if name == "__wrapped__":
            message = f"{self._module_name!r} module has no attribute {name!r}"
            raise AttributeError(message)

        if self._module_available:
            module = self._import()
            return getattr(module, name)

        if re.match(r"^__\w+__$", name) and name != "__version__":
            return None

        prefix = self._module_prefixes.get(self._module_name, "")
        message = (
            f"{prefix}{name} requires the {self._module_name!r} module to be installed"
        )
        raise ModuleNotFoundError(message) from None


def _lazy_import(module_name: str) -> tuple[ModuleType, bool]:
    """Return a lazily-imported module and whether it is available.

    Avoids the up-front cost of importing an optional module: an already
    loaded module is returned directly, otherwise a :class:`_LazyModule`
    proxy is returned that imports the real module on first use (or raises a
    helpful error if it is not installed).

    Parameters
    ----------
    module_name : str
        Import name of the module, e.g. ``"pyarrow"``.

    Returns
    -------
    tuple of (types.ModuleType, bool)
        The module (real or proxy) and a flag indicating whether the real
        module is importable.
    """
    if module_name in sys.modules:
        return sys.modules[module_name], True

    try:
        spec = find_spec(module_name)
        module_available = spec is not None and spec.loader is not None
    except ModuleNotFoundError:
        module_available = False

    proxy = _LazyModule(module_name, module_available=module_available)
    return proxy, module_available


def _parse_version(version: str | tuple[int, ...]) -> tuple[int, ...]:
    """Parse a dotted version into a comparable tuple of integers.

    Leading numeric components are kept and any non-numeric suffix (such as a
    ``"rc1"`` or ``"dev0"`` segment) stops parsing, so ``"2.1.4.dev0"``
    becomes ``(2, 1, 4)``.

    Parameters
    ----------
    version : str or tuple of int
        A dotted version string, or an already-parsed tuple returned as-is.

    Returns
    -------
    tuple of int
        The numeric release components.
    """
    if not isinstance(version, str):
        return tuple(version)

    parts: list[int] = []
    for segment in version.split("."):
        match = re.match(r"\d+", segment)
        if match is None:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def import_optional(
    module_name: str,
    *,
    err_prefix: str = "required package",
    err_suffix: str = "not found",
    min_version: str | tuple[int, ...] | None = None,
    min_err_prefix: str = "requires",
    install_message: str | None = None,
) -> Any:
    """Import an optional dependency, raising a clear error if it is missing.

    Use this instead of a bare ``import`` for any third-party library other
    than ``narwhals``, so an absent or out-of-date dependency yields an
    actionable message telling the user exactly what to install.

    Parameters
    ----------
    module_name : str
        Import name of the dependency, e.g. ``"pandas"``.
    err_prefix : str, optional
        Text placed before the module name in the not-installed message.
    err_suffix : str, optional
        Text placed after the module name in the not-installed message.
    min_version : str or tuple of int, optional
        Minimum acceptable version. When given, the installed version is
        checked against it.
    min_err_prefix : str, optional
        Text placed before the module name in the too-old message.
    install_message : str, optional
        Overrides the default ``pip install`` guidance in the not-installed
        message.

    Returns
    -------
    Any
        The imported module.

    Raises
    ------
    ModuleNotFoundError
        If the module is not installed.
    ModuleUpgradeRequiredError
        If ``min_version`` is given and the installed version is lower.
    """
    module_root = module_name.split(".", 1)[0]
    try:
        module = import_module(module_name)
    except ImportError:
        prefix = f"{err_prefix.strip()} " if err_prefix else ""
        suffix = f" {err_suffix.strip()}" if err_suffix else ""
        guidance = (
            install_message or f"Please install it using `pip install {module_root}`."
        )
        message = f"{prefix}{module_name!r}{suffix}.\n{guidance}"
        raise ModuleNotFoundError(message) from None

    if min_version is not None:
        required = _parse_version(min_version)
        installed = _parse_version(getattr(module, "__version__", ""))
        width = max(len(required), len(installed))
        required_padded = required + (0,) * (width - len(required))
        installed_padded = installed + (0,) * (width - len(installed))
        if installed_padded < required_padded:
            wanted = ".".join(str(part) for part in required)
            found = getattr(module, "__version__", "unknown")
            message = (
                f"{min_err_prefix} {module_root} {wanted} or higher (found {found})"
            )
            raise ModuleUpgradeRequiredError(message)

    return module
