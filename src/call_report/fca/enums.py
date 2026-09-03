"""The FCA Call Report schedule and domain dataset enums, with their coercion."""

from __future__ import annotations

from enum import StrEnum

from call_report.exceptions import DomainDatasetNotFoundError, ScheduleNotFoundError


class FCASchedule(StrEnum):
    """A Farm Credit Administration Call Report schedule.

    Spans every schedule root name observed across FCA's published history
    (2000-present), including both retired, pre-2018 combined schedules
    (``RCI``, ``RIE``) and the current, split schedules that replaced them
    (``RCI1``/``RCI2A``-``RCI2D``, ``RIE1``/``RIE2``). The institution
    roster is handled separately via ``load_institutions`` rather than
    through this enum.

    Examples
    --------
    >>> FCASchedule.RCB
    <FCASchedule.RCB: 'RCB'>
    """  # numpydoc ignore=PR01

    RC = "RC"
    RC1 = "RC1"
    RCB = "RCB"
    RCB2 = "RCB2"
    RCB3 = "RCB3"
    RCB4 = "RCB4"
    RCB5 = "RCB5"
    RCF = "RCF"
    RCF1 = "RCF1"
    RCG = "RCG"
    RCH = "RCH"
    RCI = "RCI"
    RCI1 = "RCI1"
    RCI2A = "RCI2A"
    RCI2B = "RCI2B"
    RCI2C = "RCI2C"
    RCI2D = "RCI2D"
    RCK = "RCK"
    RCL = "RCL"
    RCM = "RCM"
    RCO = "RCO"
    RCR1 = "RCR1"
    RCR2 = "RCR2"
    RCR3 = "RCR3"
    RCR4 = "RCR4"
    RCR5 = "RCR5"
    RCR6 = "RCR6"
    RCR7 = "RCR7"
    RI = "RI"
    RIA = "RIA"
    RIB = "RIB"
    RIC = "RIC"
    RIC1 = "RIC1"
    RID = "RID"
    RIE = "RIE"
    RIE1 = "RIE1"
    RIE2 = "RIE2"

    @classmethod
    def coerce(cls, *, value: FCASchedule | str) -> FCASchedule:
        """Coerce a schedule value into an FCASchedule member.

        Accepts either an existing `FCASchedule` member (returned
        unchanged) or a schedule name string, matched case-insensitively.

        Parameters
        ----------
        value : FCASchedule or str
            The schedule to coerce.

        Returns
        -------
        FCASchedule
            The matching enum member.

        Raises
        ------
        ScheduleNotFoundError
            If `value` is a string that does not match any known
            schedule name.

        Examples
        --------
        >>> FCASchedule.coerce(value="rcb")
        <FCASchedule.RCB: 'RCB'>
        """
        if isinstance(value, cls):
            return value
        try:
            return cls(value.upper())
        except ValueError:
            valid = sorted(member.value for member in cls)
            raise ScheduleNotFoundError(
                f"Unknown FCA schedule {value!r}; valid schedules are {valid}."
            ) from None


class FCADomainDataset(StrEnum):
    """A curated domain dataset this package ships for FCA Call Report data.

    Each member names a view assembled from several schedules, with the
    row key and every output column chosen rather than derived
    mechanically from the source layout. That curation is what separates
    a domain dataset from `FCACallReport.to_code_grain_format`, which
    reshapes whatever schedules it is given under the source's own names.

    Examples
    --------
    >>> FCADomainDataset.LOAN_PORTFOLIO
    <FCADomainDataset.LOAN_PORTFOLIO: 'loan_portfolio'>
    """  # numpydoc ignore=PR01

    LOAN_PORTFOLIO = "loan_portfolio"

    @classmethod
    def coerce(cls, *, value: FCADomainDataset | str) -> FCADomainDataset:
        """Coerce a domain dataset value into an FCADomainDataset member.

        Accepts either an existing `FCADomainDataset` member (returned
        unchanged) or a dataset name string, matched case-insensitively.
        The domain dataset counterpart to `FCASchedule.coerce`.

        Parameters
        ----------
        value : FCADomainDataset or str
            The domain dataset to coerce.

        Returns
        -------
        FCADomainDataset
            The matching enum member.

        Raises
        ------
        DomainDatasetNotFoundError
            If `value` is a string that does not match any shipped
            dataset name.

        Examples
        --------
        >>> FCADomainDataset.coerce(value="LOAN_PORTFOLIO")
        <FCADomainDataset.LOAN_PORTFOLIO: 'loan_portfolio'>
        """
        if isinstance(value, cls):
            return value
        try:
            return cls(value.lower())
        except ValueError:
            valid = sorted(member.value for member in cls)
            raise DomainDatasetNotFoundError(
                f"Unknown FCA domain dataset {value!r}; valid datasets are {valid}."
            ) from None
