"""Tests for call_report._dependencies (lazy imports and optional deps)."""

from __future__ import annotations

import sys

import pytest

from call_report._dependencies import (
    ModuleUpgradeRequiredError,
    _lazy_import,
    _LazyModule,
    _parse_version,
    import_optional,
)

MISSING = "definitely_missing_pkg_xyz"


def test_import_optional_returns_installed_module() -> None:
    """A present dependency is imported and returned."""
    module = import_optional("json")
    assert module.loads("{}") == {}


def test_import_optional_missing_raises_with_pip_hint() -> None:
    """A missing dependency raises ModuleNotFoundError naming the pip install."""
    with pytest.raises(ModuleNotFoundError) as excinfo:
        import_optional(MISSING)
    message = str(excinfo.value)
    assert "required package" in message
    assert f"'{MISSING}'" in message
    assert "not found" in message
    assert f"pip install {MISSING}" in message


def test_import_optional_custom_prefix_and_install_message() -> None:
    """Empty prefixes/suffixes and install_message override the defaults."""
    with pytest.raises(ModuleNotFoundError) as excinfo:
        import_optional(
            MISSING,
            err_prefix="",
            err_suffix="",
            install_message="Run `pip install call-report[extra]`.",
        )
    message = str(excinfo.value)
    assert message.startswith(f"'{MISSING}'.")
    assert "Run `pip install call-report[extra]`." in message


def test_import_optional_min_version_satisfied() -> None:
    """A high-enough installed version passes the min_version check."""
    module = import_optional("narwhals", min_version="0.1")
    assert module.__name__ == "narwhals"


def test_import_optional_min_version_too_low_raises() -> None:
    """An installed version below min_version raises ModuleUpgradeRequiredError."""
    with pytest.raises(ModuleUpgradeRequiredError) as excinfo:
        import_optional("narwhals", min_version="9999")
    message = str(excinfo.value)
    assert "narwhals" in message
    assert "9999" in message


def test_module_upgrade_error_is_import_error() -> None:
    """ModuleUpgradeRequiredError is catchable as ImportError."""
    assert issubclass(ModuleUpgradeRequiredError, ImportError)


def test_lazy_import_already_loaded_returns_real_module() -> None:
    """An already-imported module is returned directly, flagged available."""
    module, available = _lazy_import("sys")
    assert available is True
    assert module is sys


def test_lazy_import_available_module_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An available-but-unloaded module is proxied and imported on first use."""
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    module, available = _lazy_import("colorsys")
    assert available is True
    assert getattr(module, "__lazy__", False) is True
    # Attribute access triggers the real, deferred import.
    assert callable(module.rgb_to_hls)


def test_lazy_import_missing_returns_unavailable_proxy() -> None:
    """A missing module yields an unavailable proxy."""
    module, available = _lazy_import(MISSING)
    assert available is False
    assert getattr(module, "__lazy__", False) is True


def test_lazy_import_missing_parent_is_unavailable() -> None:
    """A dotted name whose parent package is missing is unavailable."""
    _module, available = _lazy_import(f"{MISSING}.submodule")
    assert available is False


def test_proxy_missing_module_attribute_raises() -> None:
    """Accessing a real attribute on an unavailable proxy raises with a hint."""
    proxy = _LazyModule("pandas", module_available=False)
    with pytest.raises(ModuleNotFoundError, match=r"pd\.DataFrame requires"):
        _ = proxy.DataFrame


def test_proxy_missing_module_dunder_is_none() -> None:
    """Harmless dunder lookups return None on an unavailable proxy."""
    proxy = _LazyModule(MISSING, module_available=False)
    assert proxy.__totally_fake__ is None


def test_proxy_missing_module_version_raises() -> None:
    """__version__ is not treated as a harmless dunder and raises."""
    proxy = _LazyModule(MISSING, module_available=False)
    with pytest.raises(ModuleNotFoundError):
        _ = proxy.__version__


def test_proxy_wrapped_attribute_raises_attribute_error() -> None:
    """__wrapped__ raises AttributeError so the proxy isn't seen as a wrapper."""
    proxy = _LazyModule(MISSING, module_available=False)
    with pytest.raises(AttributeError):
        _ = proxy.__wrapped__


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.2.3", (1, 2, 3)),
        ("2.1.4.dev0", (2, 1, 4)),
        ("1.0rc1", (1, 0)),
        ("", ()),
        ((3, 1), (3, 1)),
    ],
)
def test_parse_version(
    version: str | tuple[int, ...], expected: tuple[int, ...]
) -> None:
    """Versions parse to integer tuples, ignoring any non-numeric tail."""
    assert _parse_version(version) == expected
