"""Parse FCA ``D_<ROOT>.TXT`` layout files into typed variable metadata.

FCA layout files are free-text, windows-1252-encoded, and mix three
structural scenarios in one shared regex-based grammar: fields are always
``NAME  Numeric|Alphanum.  DECIMAL_POSITION  Definition text...``, and a
``**`` prefix on a name marks it as a *multiple-occurrence* column that
repeats once per reported code, in a contiguous run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import narwhals as nw

from call_report._backend import build_frame, finalize
from call_report.exceptions import LayoutParseError

LayoutScenario = Literal["single", "single_multiple", "single_multiple_single"]

ENCODING = "windows-1252"
"""str: The encoding used by every FCA Call Report file, layout or data."""

_TYPE_LOOKAHEAD = r"(?=(\s+Alphanum\.|\s+Numeric))"
_TOKEN_RE = re.compile(rf"(\*\*)?\b(\w+){_TYPE_LOOKAHEAD}")
_NOTE_STRIP_RE = re.compile(r"\*\*\s+NOTE.*$")
_STAR_COLLAPSE_RE = re.compile(r"\*\*\s+")
_WHITESPACE_RE = re.compile(r"\s+")
# Matches "numeric" only when used as the field-type keyword (followed by
# the whitespace-then-digit decimal position), not an incidental occurrence
# of the word elsewhere in a variable's definition text.
_LOWERCASE_NUMERIC_KEYWORD_RE = re.compile(r"\bnumeric(?=\s+\d)")


@dataclass(frozen=True, kw_only=True)
class FCALayout:
    """A parsed FCA layout: its scenario, variable metadata, and column groups.

    Returned by `parse_layout`; the column groups tell :mod:`call_report.fca.reader`
    how to split each data row into its single- and multiple-occurrence parts.

    Attributes
    ----------
    scenario : {"single", "single_multiple", "single_multiple_single"}
        Which of the three structural scenarios this layout follows.
    variables : Any
        A native dataframe (of the configured backend) with one row per
        variable and columns ``name``, ``type``, ``decimal_position``,
        ``definition``, ``is_multi``.
    leading_columns : tuple[str, ...]
        Single-occurrence column names appearing before the multi-occurrence
        run, if any.
    multi_columns : tuple[str, ...]
        Multiple-occurrence column names, in layout order; the first entry
        is always the code column. Empty for the ``"single"`` scenario.
    trailing_columns : tuple[str, ...]
        Single-occurrence column names appearing after the multi-occurrence
        run. Only ever non-empty for ``"single_multiple_single"``.
    """

    scenario: LayoutScenario
    variables: Any
    leading_columns: tuple[str, ...]
    multi_columns: tuple[str, ...]
    trailing_columns: tuple[str, ...]

    def variables_as_dicts(self) -> list[dict[str, Any]]:
        """Return the `variables` frame as a list of plain dicts.

        A convenience for inspecting `variables` without depending on
        which dataframe backend is currently configured.

        Returns
        -------
        list[dict[str, Any]]
            One dict per variable, in layout order.
        """
        frame = nw.from_native(self.variables)
        if isinstance(frame, nw.LazyFrame):
            frame = frame.collect()
        return frame.rows(named=True)


def parse_layout(*, path: Path) -> FCALayout:
    """Parse a ``D_<ROOT>.TXT`` layout file into an FCALayout.

    Handles the real-world quirks confirmed against FCA's published
    releases: windows-1252 encoding, a literal tab before at least one
    field, one file's lowercase ``"numeric"`` keyword, and a trailing
    ``** NOTE ...`` block describing the multi-occurrence columns.

    Parameters
    ----------
    path : pathlib.Path
        Path to the ``D_<ROOT>.TXT`` file.

    Returns
    -------
    FCALayout
        The parsed layout.

    Raises
    ------
    LayoutParseError
        If the file's multi-occurrence-column pattern does not match one of
        the three known scenarios.

    Examples
    --------
    >>> layout = parse_layout(path=Path("D_RCB.TXT"))  # doctest: +SKIP
    >>> layout.scenario  # doctest: +SKIP
    'single_multiple'
    """
    raw_text = path.read_bytes().decode(ENCODING)
    raw_text = " ".join(raw_text.splitlines())
    raw_text = raw_text.replace("\t", " ")
    raw_text = _LOWERCASE_NUMERIC_KEYWORD_RE.sub("Numeric", raw_text)
    raw_text = _NOTE_STRIP_RE.sub("", raw_text)
    raw_text = _STAR_COLLAPSE_RE.sub("**", raw_text)
    raw_text = _TOKEN_RE.sub(r"\n\1\2", raw_text)

    # The text before the first matched variable is a title/header block.
    variable_lines = [line for line in raw_text.split("\n")[1:] if line.strip()]

    rows: list[dict[str, Any]] = []
    for line in variable_lines:
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        raw_name, var_type, decimal_position_text = parts[0], parts[1], parts[2]
        definition = _WHITESPACE_RE.sub(" ", parts[3]).strip() if len(parts) > 3 else ""
        rows.append(
            {
                "name": raw_name.lstrip("*"),
                "type": var_type,
                "decimal_position": int(decimal_position_text),
                "definition": definition,
                "is_multi": raw_name.startswith("**"),
            }
        )

    scenario, leading, multi, trailing = _classify_scenario(rows=rows, path=path)

    frame = build_frame(
        data={
            "name": [row["name"] for row in rows],
            "type": [row["type"] for row in rows],
            "decimal_position": [row["decimal_position"] for row in rows],
            "definition": [row["definition"] for row in rows],
            "is_multi": [row["is_multi"] for row in rows],
        }
    )
    return FCALayout(
        scenario=scenario,
        variables=finalize(frame=frame),
        leading_columns=leading,
        multi_columns=multi,
        trailing_columns=trailing,
    )


def _classify_scenario(
    *, rows: list[dict[str, Any]], path: Path
) -> tuple[LayoutScenario, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Classify a layout's parsed rows into a scenario and column groups.

    The scenario is determined by the run-length encoding of each row's
    ``is_multi`` flag: one run means every column is single-occurrence, two
    runs means a single-occurrence prefix followed by a multi-occurrence
    run, and three runs means a multi-occurrence run sandwiched between two
    single-occurrence runs.

    Parameters
    ----------
    rows : list[dict[str, Any]]
        The parsed variable rows, in layout order.
    path : pathlib.Path
        The source file's path, included in the error message if the
        pattern doesn't match a known scenario.

    Returns
    -------
    tuple[LayoutScenario, tuple[str, ...], tuple[str, ...], tuple[str, ...]]
        The scenario, followed by its leading, multi, and trailing column
        name groups.

    Raises
    ------
    LayoutParseError
        If the ``is_multi`` flags form more than three runs.
    """
    runs: list[tuple[bool, int]] = []
    for row in rows:
        flag = row["is_multi"]
        if runs and runs[-1][0] == flag:
            previous_flag, previous_length = runs[-1]
            runs[-1] = (previous_flag, previous_length + 1)
        else:
            runs.append((flag, 1))

    names = [row["name"] for row in rows]
    if len(runs) == 1:
        return "single", tuple(names), (), ()
    if len(runs) == 2:
        n_leading = runs[0][1]
        return "single_multiple", tuple(names[:n_leading]), tuple(names[n_leading:]), ()
    if len(runs) == 3:
        n_leading = runs[0][1]
        n_multi = runs[1][1]
        return (
            "single_multiple_single",
            tuple(names[:n_leading]),
            tuple(names[n_leading : n_leading + n_multi]),
            tuple(names[n_leading + n_multi :]),
        )
    raise LayoutParseError(
        f"{path}: unrecognized multi-occurrence-column pattern ({len(runs)} runs); "
        "expected a single run (no ** columns), two runs (a single_multiple "
        "layout), or three runs (a single_multiple_single layout)."
    )
