"""Property-based tests for the FCA download-URL catalog.

``tests/fca/test_catalog.py`` checks a handful of specific URLs and the
2014/2015 era boundary. These cover the whole published range instead.

The uniqueness property is the one worth having. Two periods mapping to the
same URL would not raise anything, it would quietly hand a caller another
quarter's data, and the legacy convention (``Sept2014.zip``) packs year and
month tightly enough that a collision is a real thing to rule out rather
than an abstract one.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from call_report.core import Quarter, ReportingPeriod
from call_report.fca.catalog import (
    construct_fca_download_url,
    fca_release_stem,
    parse_fca_release_filename,
)
from tests.helpers import archived_periods

MODERN_ERA_FIRST_YEAR = 2015
MODERN_BASE = "https://www.fca.gov/template-fca/bank/"
LEGACY_BASE = "https://www.fca.gov/template-fca/download/CallReportData/"

ALL_PERIODS = archived_periods()
published_periods = st.sampled_from(ALL_PERIODS)


@given(period=published_periods)
def test_every_published_period_gets_a_zip_url(period: ReportingPeriod) -> None:
    """Every period FCA publishes yields a URL naming a zip archive."""
    url = construct_fca_download_url(period=period)
    assert url.startswith("https://www.fca.gov/")
    assert url.endswith(".zip")


@given(period=published_periods)
def test_era_is_decided_by_year_alone(period: ReportingPeriod) -> None:
    """A period uses the modern base from 2015 onward and the legacy one before."""
    url = construct_fca_download_url(period=period)
    if period.year >= MODERN_ERA_FIRST_YEAR:
        assert url.startswith(MODERN_BASE)
        assert str(period.year) in url
    else:
        assert url.startswith(LEGACY_BASE)
        assert str(period.year) in url


def test_no_two_published_periods_share_a_url() -> None:
    """Each published period maps to a distinct URL.

    A collision would not raise. It would silently return a different
    quarter's data, so this checks the whole catalog rather than sampling.
    """
    urls = [construct_fca_download_url(period=period) for period in ALL_PERIODS]
    assert len(set(urls)) == len(urls)


def test_the_era_boundary_falls_between_2014q4_and_2015q1() -> None:
    """The last legacy period and the first modern one sit either side of 2015."""
    last_legacy = ReportingPeriod.from_period_end(value="2014-12-31")
    first_modern = ReportingPeriod.from_period_end(value="2015-03-31")
    assert construct_fca_download_url(period=last_legacy).startswith(LEGACY_BASE)
    assert construct_fca_download_url(period=first_modern).startswith(MODERN_BASE)
    assert last_legacy.next() == first_modern


@given(period=published_periods)
def test_url_filename_has_no_path_separators_or_spaces(
    period: ReportingPeriod,
) -> None:
    """The filename segment is a single safe path component.

    `PackagedArchiveTransport` and `LocalDirectoryTransport` both derive a
    directory name from this filename, so anything that would escape a
    directory or need quoting is a problem.
    """
    filename = construct_fca_download_url(period=period).rsplit("/", 1)[-1]
    assert filename
    assert " " not in filename
    assert ".." not in filename
    assert "\\" not in filename


@given(
    period=st.builds(
        ReportingPeriod,
        year=st.integers(min_value=1000, max_value=9999),
        quarter=st.sampled_from(Quarter),
    )
)
def test_parsing_a_stem_gives_back_its_period(period: ReportingPeriod) -> None:
    """parse_fca_release_filename inverts fca_release_stem for every period.

    The mirror and the catalog both turn filenames back into periods, so a
    stem that parsed to a different quarter would file its data under the
    wrong one.
    """
    assert parse_fca_release_filename(fca_release_stem(period=period) + ".zip") == (
        period
    )
