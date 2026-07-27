"""Tests for the shared, source-agnostic BaseCallReport ABC."""

from __future__ import annotations

from typing import Any, Self

import pytest

from call_report.core import BaseCallReport, ReportingPeriod


def test_base_call_report_cannot_be_instantiated_directly() -> None:
    """BaseCallReport is abstract -- every source must implement its own methods."""
    with pytest.raises(TypeError):
        BaseCallReport()  # type: ignore[abstract]


class _StubCallReport(BaseCallReport):
    """A minimal concrete subclass used only to exercise the shared mixin behavior."""

    def __init__(self, *, start: str, end: str | None = None, extra: int = 1) -> None:
        self.start = start
        self.end = end
        self.extra = extra

    def fetch(self) -> Self:
        return self

    def load(self, *, schedule: Any) -> Any:
        return None

    def load_all(self) -> dict[Any, Any]:
        return {}

    def load_institutions(self) -> Any:
        return None

    def get_layout(self, *, schedule: Any, period: Any = None) -> Any:
        return None

    def available_periods(self) -> tuple[ReportingPeriod, ...]:
        return ()

    def available_schedules(self) -> tuple[Any, ...]:
        return ()


def test_get_params_introspects_constructor_signature() -> None:
    """get_params discovers parameter names from __init__ without redeclaring them."""
    stub = _StubCallReport(start="2024-03-31", end="2025-12-31", extra=5)
    assert stub.get_params() == {"start": "2024-03-31", "end": "2025-12-31", "extra": 5}


def test_set_params_returns_self_and_mutates_attributes() -> None:
    """set_params mutates matching attributes and returns self for chaining."""
    stub = _StubCallReport(start="2024-03-31")
    result = stub.set_params(extra=99)
    assert result is stub
    assert stub.extra == 99


def test_set_params_rejects_unknown_param() -> None:
    """set_params raises ValueError for a name that isn't a constructor parameter."""
    stub = _StubCallReport(start="2024-03-31")
    with pytest.raises(ValueError, match="bogus"):
        stub.set_params(bogus=1)


def test_repr_reflects_current_params() -> None:
    """__repr__ is generated from get_params(), including the class name."""
    stub = _StubCallReport(start="2024-03-31", extra=7)
    text = repr(stub)
    assert text.startswith("_StubCallReport(")
    assert "start='2024-03-31'" in text
    assert "extra=7" in text


def test_get_params_is_keyword_only() -> None:
    """get_params takes no positional arguments."""
    stub = _StubCallReport(start="2024-03-31")
    with pytest.raises(TypeError):
        stub.get_params(True)  # type: ignore[call-arg]
