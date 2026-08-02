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

    def _load(self, *, schedule: Any) -> Any:
        import pandas as pd

        return pd.DataFrame({"schedule": [schedule]})

    def _load_all(self) -> dict[Any, Any]:
        import pandas as pd

        return {"RC": pd.DataFrame({"schedule": ["RC"]})}

    def _load_institutions(self) -> Any:
        import pandas as pd

        return pd.DataFrame({"UNINUM": [1]})

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


# ---------------------------------------------------------------------------
# load() / load_all() / load_institutions() -- the @final template methods
# ---------------------------------------------------------------------------


def test_load_delegates_to_load_and_defaults_to_no_conversion() -> None:
    """load() calls the subclass's _load and returns it unchanged by default."""
    import pandas as pd

    stub = _StubCallReport(start="2024-03-31")
    result = stub.load(schedule="RC")
    assert isinstance(result, pd.DataFrame)
    assert result["schedule"].tolist() == ["RC"]


def test_load_applies_dataframe_type_conversion() -> None:
    """load() converts _load's result to dataframe_type as its one final step."""
    import pyarrow as pa

    stub = _StubCallReport(start="2024-03-31")
    result = stub.load(schedule="RC", dataframe_type="pyarrow_table")
    assert isinstance(result, pa.Table)


def test_load_institutions_delegates_and_defaults_to_no_conversion() -> None:
    """load_institutions() calls _load_institutions unchanged by default."""
    import pandas as pd

    stub = _StubCallReport(start="2024-03-31")
    result = stub.load_institutions()
    assert isinstance(result, pd.DataFrame)


def test_load_institutions_applies_dataframe_type_conversion() -> None:
    """load_institutions() converts the result to dataframe_type."""
    import pyarrow as pa

    stub = _StubCallReport(start="2024-03-31")
    result = stub.load_institutions(dataframe_type="pyarrow_table")
    assert isinstance(result, pa.Table)


def test_load_all_delegates_to_load_all_and_defaults_to_no_conversion() -> None:
    """load_all() calls the subclass's _load_all and returns it unchanged."""
    import pandas as pd

    stub = _StubCallReport(start="2024-03-31")
    result = stub.load_all()
    assert isinstance(result["RC"], pd.DataFrame)


def test_load_all_applies_dataframe_type_conversion_to_every_value() -> None:
    """load_all() converts every value in the dict to dataframe_type."""
    import pyarrow as pa

    stub = _StubCallReport(start="2024-03-31")
    result = stub.load_all(dataframe_type="pyarrow_table")
    assert isinstance(result["RC"], pa.Table)
