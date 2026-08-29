"""Lazily loaded, cached, curated FCA domain dataset definitions.

A domain dataset names a view assembled from several schedules, with the
row key and every output column chosen rather than derived mechanically
from the source layout. The definitions are shipped as one JSON file per
dataset under ``call_report/fca/data/domain_datasets/``. This module is
the runtime read side: each file is parsed at most once per process, on
first request, matching `call_report.fca._schedule_metadata`.

Curation, rather than a naming convention, is what resolves collisions
here. Two schedules that measure the same thing map to one output column
deliberately, and two that measure different things are given different
names. `DomainDataset.from_dict` enforces the resulting rule, that no two
source groups declare the same output column.
"""

from __future__ import annotations

import functools
import importlib.resources
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, overload

from call_report.core._backend import DataFrameType, build_frame, finalize_as
from call_report.exceptions import SchemaError
from call_report.fca.enums import FCADomainDataset, coerce_fca_domain_dataset

if TYPE_CHECKING:
    import pandas
    import polars
    import pyarrow

    from call_report.core._backend import NativeDataFrame

DerivedOperation = Literal["sum", "difference"]


@dataclass(frozen=True, kw_only=True)
class DomainDatasetCode:
    """One row key value in a curated domain dataset.

    The codes are the source's own, not a numbering this package invents,
    so a value here can be matched against the raw file. `label` is the
    curation, giving each code a name a reader does not have to look up.

    Attributes
    ----------
    code : int
        The code identifying this row, in the source schedule's own
        vocabulary.
    label : str
        A human-readable name for what `code` identifies.
    is_total : bool
        Whether this code is a subtotal the source reports rather than a
        distinct member of the breakdown. A caller summing over codes has
        to exclude these.
    """  # numpydoc ignore=PR01

    code: int
    label: str
    is_total: bool


@dataclass(frozen=True, kw_only=True)
class DomainDatasetColumn:
    """One source variable's contribution to a curated domain dataset.

    Every declared variable answers two questions: which output column its
    values belong in, and which code its row is keyed by. A code-bearing
    schedule answers the second from its data, so `code` is ``None`` there
    and set only for a schedule that encodes the breakdown in its names.

    Attributes
    ----------
    column : str
        The curated output column this variable's values land in.
    code : int, optional
        The code this variable belongs to, for a source whose breakdown
        is encoded in its variable names. ``None`` for a source that
        reports a code column, where the code comes from the data.
    """  # numpydoc ignore=PR01

    column: str
    code: int | None


@dataclass(frozen=True, kw_only=True)
class DomainDatasetSource:
    """One group of schedules contributing to a curated domain dataset.

    Schedules are grouped rather than listed individually when they carry
    the same fields under different root names, which is how FCA's
    mid-history schedule splits appear. Grouping them is what keeps a
    series continuous across such a split.

    Attributes
    ----------
    schedules : tuple[str, ...]
        The schedule root names this group covers.
    code_column : str, optional
        The schedule's own code column, for a source that reports one.
        ``None`` for a source whose breakdown is encoded in its variable
        names instead.
    columns : dict[str, DomainDatasetColumn]
        Each contributing variable, keyed by its name in the source.
    """  # numpydoc ignore=PR01

    schedules: tuple[str, ...]
    code_column: str | None
    columns: dict[str, DomainDatasetColumn]

    @property
    def output_columns(self) -> frozenset[str]:
        """Return the distinct output column names this group declares.

        Several variables map to one output column, once per code, so this
        collapses the group down to the columns it contributes. That is
        what `DomainDataset.from_dict` compares across groups when it
        checks for a collision.

        Returns
        -------
        frozenset[str]
            Every `DomainDatasetColumn.column` value in `columns`.
        """
        return frozenset(item.column for item in self.columns.values())


@dataclass(frozen=True, kw_only=True)
class DomainDatasetDerived:
    """One output column computed from other output columns.

    Derived columns are part of the curation rather than the source data.
    Declaring the components here keeps two definitions of the same idea
    (a narrow and a wide nonperforming total, say) visible side by side
    instead of buried in the code that computes them.

    Attributes
    ----------
    column : str
        The output column this produces.
    operation : {"sum", "difference"}
        How `components` combine.
    components : tuple[str, ...]
        The output columns this is computed from, in order.
    """  # numpydoc ignore=PR01

    column: str
    operation: DerivedOperation
    components: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class DomainDataset:
    """A curated view assembled from several FCA Call Report schedules.

    Returned by `get_fca_domain_dataset`. Describes which schedules
    compose the view, which code each row is keyed by, and what every
    output column is called.

    Attributes
    ----------
    name : str
        The dataset's own name, matching its `FCADomainDataset` member.
    code_column : str
        The value the reshaped frame's ``code_column`` carries. A curated
        name, not any one schedule's own code column.
    codes : tuple[DomainDatasetCode, ...]
        Every code this dataset keys rows by.
    sources : tuple[DomainDatasetSource, ...]
        The schedule groups contributing columns.
    derived : tuple[DomainDatasetDerived, ...]
        Output columns computed from other output columns.
    """  # numpydoc ignore=PR01

    name: str
    code_column: str
    codes: tuple[DomainDatasetCode, ...]
    sources: tuple[DomainDatasetSource, ...]
    derived: tuple[DomainDatasetDerived, ...]

    @property
    def schedules(self) -> tuple[str, ...]:
        """Return every schedule root name this dataset draws on.

        Flattens the source groups back into one list, which is what a
        caller needs to decide which schedules to load. Grouping matters
        for naming, not for loading.

        Returns
        -------
        tuple[str, ...]
            The root names, in declaration order.

        Examples
        --------
        >>> from call_report.fca import FCADomainDataset, get_fca_domain_dataset
        >>> dataset = get_fca_domain_dataset(
        ...     domain_dataset=FCADomainDataset.LOAN_PORTFOLIO
        ... )
        >>> dataset.schedules
        ('RCF1', 'RIE', 'RIE2')
        """
        return tuple(
            schedule for source in self.sources for schedule in source.schedules
        )

    @property
    def total_codes(self) -> frozenset[int]:
        """Return the codes that are reported subtotals rather than members.

        A subtotal row is a figure the source reports itself, not one this
        package computes, so it is included by default. Aggregating over
        every code without excluding these double counts.

        Returns
        -------
        frozenset[int]
            Every `DomainDatasetCode.code` whose `is_total` is True.

        Examples
        --------
        >>> from call_report.fca import FCADomainDataset, get_fca_domain_dataset
        >>> dataset = get_fca_domain_dataset(
        ...     domain_dataset=FCADomainDataset.LOAN_PORTFOLIO
        ... )
        >>> sorted(dataset.total_codes)
        [155]
        """
        return frozenset(item.code for item in self.codes if item.is_total)

    @classmethod
    def from_dict(cls, *, data: dict[str, Any]) -> DomainDataset:
        """Build a DomainDataset from one shipped definition's parsed JSON.

        Validates that no two source groups declare the same output
        column. Within a group they may and do repeat, which is how a
        schedule split maps to one continuous column.

        Parameters
        ----------
        data : dict[str, Any]
            One dataset definition's parsed JSON.

        Returns
        -------
        DomainDataset
            The parsed definition.

        Raises
        ------
        SchemaError
            If two source groups declare the same output column, which
            would put two different measures in one column.
        """
        sources = tuple(
            DomainDatasetSource(
                schedules=tuple(source["schedules"]),
                code_column=source["code_column"],
                columns={
                    variable: DomainDatasetColumn(
                        column=item["column"], code=item.get("code")
                    )
                    for variable, item in source["columns"].items()
                },
            )
            for source in data["sources"]
        )

        seen: set[str] = set()
        for source in sources:
            collisions = sorted(seen & source.output_columns)
            if collisions:
                raise SchemaError(
                    f"Domain dataset {data['name']!r} declares {collisions} in more "
                    "than one source group, which would put two different measures "
                    "in one column."
                )
            seen |= source.output_columns

        return cls(
            name=data["name"],
            code_column=data["code_column"],
            codes=tuple(
                DomainDatasetCode(
                    code=item["code"], label=item["label"], is_total=item["is_total"]
                )
                for item in data["codes"]
            ),
            sources=sources,
            derived=tuple(
                DomainDatasetDerived(
                    column=item["column"],
                    operation=item["operation"],
                    components=tuple(item["components"]),
                )
                for item in data["derived"]
            ),
        )


@functools.cache
def get_fca_domain_dataset(*, domain_dataset: FCADomainDataset | str) -> DomainDataset:
    """Return one curated domain dataset's shipped definition.

    Parsed from the packaged JSON on the first call for `domain_dataset`,
    then cached for the life of the process.

    Parameters
    ----------
    domain_dataset : FCADomainDataset or str
        The dataset to return. A string is matched case-insensitively.

    Returns
    -------
    DomainDataset
        The dataset's curated definition.

    Raises
    ------
    DomainDatasetNotFoundError
        If `domain_dataset` does not name a shipped dataset.

    Examples
    --------
    >>> from call_report.fca import get_fca_domain_dataset
    >>> dataset = get_fca_domain_dataset(domain_dataset="loan_portfolio")
    >>> dataset.code_column
    'LOAN_PORTFOLIO'
    >>> len(dataset.codes)
    13
    """
    member = coerce_fca_domain_dataset(value=domain_dataset)
    resource = importlib.resources.files("call_report.fca").joinpath(
        "data", "domain_datasets", f"{member.value}.json"
    )
    return DomainDataset.from_dict(
        data=json.loads(resource.read_text(encoding="utf-8"))
    )


@overload
def get_domain_dataset_codes(
    *, domain_dataset: FCADomainDataset | str, dataframe_type: None = None
) -> NativeDataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def get_domain_dataset_codes(
    *, domain_dataset: FCADomainDataset | str, dataframe_type: Literal["pandas"]
) -> pandas.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def get_domain_dataset_codes(
    *, domain_dataset: FCADomainDataset | str, dataframe_type: Literal["pyarrow_table"]
) -> pyarrow.Table:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def get_domain_dataset_codes(
    *,
    domain_dataset: FCADomainDataset | str,
    dataframe_type: Literal["polars_dataframe"],
) -> polars.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def get_domain_dataset_codes(
    *,
    domain_dataset: FCADomainDataset | str,
    dataframe_type: Literal["polars_lazyframe"],
) -> polars.LazyFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
def get_domain_dataset_codes(
    *,
    domain_dataset: FCADomainDataset | str,
    dataframe_type: DataFrameType | None = None,
) -> NativeDataFrame:
    """Return a curated domain dataset's codes and what each one means.

    The reshaped frame keys its rows by code, so this is the lookup that
    turns those codes into names. ``is_total`` marks a code the source
    reports as a subtotal, which a caller aggregating over codes has to
    exclude.

    Parameters
    ----------
    domain_dataset : FCADomainDataset or str
        The dataset to describe. A string is matched case-insensitively.
    dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"}, optional
        The dataframe type to convert the result to as a final step.
        Leave this ``None`` (the default) to get back whatever backend
        `call_report.config.get_config` currently has configured.

    Returns
    -------
    NativeDataFrame
        Columns ``code``, ``label``, and ``is_total``, one row per code,
        of the configured backend or of `dataframe_type` if it was
        supplied.

    Raises
    ------
    DomainDatasetNotFoundError
        If `domain_dataset` does not name a shipped dataset.

    Examples
    --------
    >>> from call_report.fca import get_domain_dataset_codes
    >>> codes = get_domain_dataset_codes(domain_dataset="loan_portfolio")
    >>> codes.shape
    (13, 3)
    >>> row = codes[codes["code"] == 110].iloc[0]
    >>> row["label"], bool(row["is_total"])
    ('Agribusiness', False)
    """
    dataset = get_fca_domain_dataset(domain_dataset=domain_dataset)
    frame = build_frame(
        data={
            "code": [item.code for item in dataset.codes],
            "label": [item.label for item in dataset.codes],
            "is_total": [item.is_total for item in dataset.codes],
        }
    )
    return finalize_as(frame=frame, dataframe_type=dataframe_type)
