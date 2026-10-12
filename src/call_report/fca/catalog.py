"""FCA Call Report release names, download URLs, and release catalogs.

FCA's own publication page is
`<https://www.fca.gov/bank-oversight/call-report-data-for-download>`_. This
project also mirrors every release as assets of its GitHub releases. A
rolling release holds the current files, and a snapshot release holds the
files as they stood on each date the data changed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from types import MappingProxyType
from typing import Any, Self
from urllib.parse import unquote, urljoin, urlsplit

from call_report.core import Quarter, ReportingPeriod
from call_report.core._http import HttpClient, fetch_bytes
from call_report.exceptions import (
    DownloadError,
    InvalidPeriodError,
    PeriodNotAvailableError,
)

DOWNLOAD_PAGE_URL = "https://www.fca.gov/bank-oversight/call-report-data-for-download"
"""The URL of FCA's call report download page.

`FCAReleaseCatalog.from_fca` reads the link to every release zip from this
page.
"""

MIRROR_BASE_URL = "https://github.com/predict-ably/call-report/releases/download/"
"""The base URL of this project's GitHub release assets.

A file in a mirror release is at ``<MIRROR_BASE_URL><tag>/<file name>``.
"""

_MODERN_ERA_BASE_URL = "https://www.fca.gov/template-fca/bank/"
_LEGACY_ERA_BASE_URL = "https://www.fca.gov/template-fca/download/CallReportData/"

_MODERN_ERA_FIRST_YEAR = 2015

_MONTH_NAME_BY_QUARTER_END_MONTH: dict[int, str] = {
    3: "March",
    6: "June",
    9: "September",
    12: "December",
}
_LEGACY_ABBREVIATION_BY_QUARTER_END_MONTH: dict[int, str] = {
    3: "Mar",
    6: "Jun",
    9: "Sept",
    12: "Dec",
}

# A release zip's name is four digits of year and some letters, in either
# order. parse_fca_release_filename then keeps it only if it is the exact
# stem fca_release_stem gives that year's quarter.
_RELEASE_FILENAME = re.compile(
    r"(?:[a-z]+(?P<legacy_year>\d{4})|(?P<modern_year>\d{4})[a-z]+)\.zip",
    re.IGNORECASE,
)

# The mirror's release tags. The rolling release always holds the current
# files. Each snapshot release holds the files as they stood on one date and
# is never changed.
_ROLLING_TAG = "fca-call-report-data"
_SNAPSHOT_TAG_PREFIX = "fca-data-"
_MANIFEST_NAME = "manifest.json"
# The manifest format this version of the package reads. It changes only when
# the format does.
_MANIFEST_SCHEMA_VERSION = 1
# A snapshot is the date the data changed, with a "-2", "-3", and so on for a
# second or third change on the same date.
_SNAPSHOT = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})(?:-[1-9]\d*)?")
_SHA256 = re.compile(r"[0-9a-f]{64}")

# FCA's first release. The local transports check each quarter from here for a
# release, and construct_fca_download_url rejects anything earlier.
_FIRST_RELEASE_PERIOD = ReportingPeriod.from_period_end(value="2000-03-31")


# A function rather than a direct date.today() call, so tests can replace it to
# fix the current quarter.
def _today() -> date:
    """Return today's date.

    The current quarter, the upper bound on every period, is the quarter
    containing this date.

    Returns
    -------
    datetime.date
        Today's date.
    """
    return date.today()


def _current_period() -> ReportingPeriod:
    """Return the quarter containing today's date.

    No source can publish a quarter later than this one, so it is the upper
    bound on every period this package builds a URL or a folder name for.

    Returns
    -------
    ReportingPeriod
        The quarter containing today.
    """
    today = _today()
    return ReportingPeriod(year=today.year, quarter=Quarter((today.month - 1) // 3 + 1))


def fca_release_stem(*, period: ReportingPeriod) -> str:
    """Return the stem of a period's FCA release zip, such as ``"2026March"``.

    FCA changed its filename convention between the legacy and modern eras.
    Periods from 2000 through 2014 put an abbreviated month name before the
    year, such as ``"Sept2014"``. Periods from 2015 onward put the year
    before the full month name, such as ``"2026March"``. Unzipping a
    release gives a folder with this name, which is the folder name the
    transports in `call_report.fca.transport` look for. Any period is
    accepted.

    Parameters
    ----------
    period : ReportingPeriod
        The period to name.

    Returns
    -------
    str
        The release zip's filename without its ``.zip`` extension.

    See Also
    --------
    parse_fca_release_filename : Turn a release zip's name back into its period.
    construct_fca_download_url : Build the FCA URL of a period's release zip.

    Examples
    --------
    >>> fca_release_stem(period=ReportingPeriod.from_period_end(value="2026-03-31"))
    '2026March'
    >>> fca_release_stem(period=ReportingPeriod.from_period_end(value="2014-09-30"))
    'Sept2014'
    """
    if period.year >= _MODERN_ERA_FIRST_YEAR:
        return f"{period.year}{_MONTH_NAME_BY_QUARTER_END_MONTH[period.month]}"
    abbreviation = _LEGACY_ABBREVIATION_BY_QUARTER_END_MONTH[period.month]
    return f"{abbreviation}{period.year}"


def parse_fca_release_filename(name: str) -> ReportingPeriod | None:
    """Return the period an FCA release zip filename names, or None.

    A name is read only if it is the name `fca_release_stem` gives a
    quarter, followed by ``.zip``. Letter case does not matter, and
    September may be ``Sep`` as well as ``Sept``. Any other name gives
    ``None``, including a name in the other era's convention, such as
    ``"Mar2020.zip"`` for a 2020 quarter.

    Parameters
    ----------
    name : str
        A filename, such as ``"2026March.zip"`` or ``"Sept2014.zip"``, with
        no folder.

    Returns
    -------
    ReportingPeriod or None
        The period the file holds, or ``None`` if the name is not an FCA
        release zip.

    See Also
    --------
    fca_release_stem : Return the name of a period's release zip.
    construct_fca_download_url : Build the FCA URL of a period's release zip.

    Examples
    --------
    >>> parse_fca_release_filename("2026March.zip").label
    '2026Q1'
    >>> parse_fca_release_filename("sep2014.ZIP").label
    '2014Q3'
    >>> parse_fca_release_filename("Mar2020.zip") is None
    True
    >>> parse_fca_release_filename("instructions.pdf") is None
    True
    """
    match = _RELEASE_FILENAME.fullmatch(name)
    if match is None:
        return None
    year = int(match["legacy_year"] or match["modern_year"])
    stem = name[: -len(".zip")].lower()
    if stem.startswith("sep") and not stem.startswith("sept"):
        stem = "sept" + stem.removeprefix("sep")
    for quarter in Quarter:
        period = ReportingPeriod(year=year, quarter=quarter)
        if fca_release_stem(period=period).lower() == stem:
            return period
    return None


def construct_fca_download_url(*, period: ReportingPeriod) -> str:
    """Construct the FCA download URL for a period's Call Report zip archive.

    FCA changed its URL and filename convention between the legacy and
    modern eras. Periods from 2000 through 2014 use an abbreviated month
    name under ``/template-fca/download/CallReportData/``. Periods from
    2015 onward use the full month name under ``/template-fca/bank/``.

    Parameters
    ----------
    period : ReportingPeriod
        The period to build a URL for.

    Returns
    -------
    str
        The full URL to that period's zip archive.

    Raises
    ------
    PeriodNotAvailableError
        If `period` is before FCA's first release (2000Q1) or after the
        quarter containing today's date.

    See Also
    --------
    fca_release_stem : Return the name of a period's release zip.
    parse_fca_release_filename : Turn a release zip's name back into its period.
    construct_fca_mirror_url : Build the GitHub mirror URL of a period's zip.

    Examples
    --------
    >>> construct_fca_download_url(
    ...     period=ReportingPeriod.from_period_end(value="2026-03-31")
    ... )
    'https://www.fca.gov/template-fca/bank/2026March.zip'
    >>> construct_fca_download_url(
    ...     period=ReportingPeriod.from_period_end(value="2014-09-30")
    ... )
    'https://www.fca.gov/template-fca/download/CallReportData/Sept2014.zip'
    """
    _check_release_period(period)
    base_url = (
        _MODERN_ERA_BASE_URL
        if period.year >= _MODERN_ERA_FIRST_YEAR
        else _LEGACY_ERA_BASE_URL
    )
    return f"{base_url}{fca_release_stem(period=period)}.zip"


def construct_fca_mirror_url(
    *, period: ReportingPeriod, snapshot: str = "latest"
) -> str:
    """Construct the GitHub mirror URL for a period's Call Report zip archive.

    The mirror holds each release zip under the same name FCA gives it. The
    URL is built from the snapshot and the period alone, so it does not say
    whether the mirror holds that period. `FCAReleaseCatalog.from_github`
    lists the periods it does hold.

    Parameters
    ----------
    period : ReportingPeriod
        The period to build a URL for.
    snapshot : str, default "latest"
        Which mirror snapshot to download from. ``"latest"`` is the current
        files. A date such as ``"2026-10-12"`` is the mirror as it stood on
        that date. Only ``source="github"`` has snapshots.

    Returns
    -------
    str
        The full URL to that period's zip archive on the mirror.

    Raises
    ------
    PeriodNotAvailableError
        If `period` is before FCA's first release (2000Q1) or after the
        quarter containing today's date.
    ValueError
        If `snapshot` is neither ``"latest"`` nor a snapshot date.

    See Also
    --------
    construct_fca_download_url : Build the FCA URL of a period's release zip.
    FCAReleaseCatalog.from_github : List the periods the mirror holds.

    Examples
    --------
    >>> construct_fca_mirror_url(
    ...     period=ReportingPeriod.from_period_end(value="2026-03-31")
    ... )
    'https://github.com/predict-ably/call-report/releases/download/fca-call-report-data/2026March.zip'
    >>> construct_fca_mirror_url(
    ...     period=ReportingPeriod.from_period_end(value="2014-09-30"),
    ...     snapshot="2026-10-12",
    ... )
    'https://github.com/predict-ably/call-report/releases/download/fca-data-2026-10-12/Sept2014.zip'
    """
    _check_release_period(period)
    return _mirror_asset_url(f"{fca_release_stem(period=period)}.zip", snapshot)


def _check_release_period(period: ReportingPeriod) -> None:
    """Raise if no source can hold a release for a period.

    FCA's first release is 2000Q1, and no release can be later than the quarter
    containing today.

    Parameters
    ----------
    period : ReportingPeriod
        The period to check.

    Raises
    ------
    PeriodNotAvailableError
        If `period` is before FCA's first release (2000Q1) or after the
        quarter containing today's date.
    """
    latest = _current_period()
    if period < _FIRST_RELEASE_PERIOD or period > latest:
        raise PeriodNotAvailableError(
            f"FCA has no release for {period.label}. Releases start at "
            f"{_FIRST_RELEASE_PERIOD.label}, and none can be later than the "
            f"current quarter, {latest.label}."
        )


def _mirror_tag(snapshot: str) -> str:
    """Return the GitHub release tag that holds a mirror snapshot.

    The rolling release holds the current files, and each snapshot release holds the
    files as they stood on one date.

    Parameters
    ----------
    snapshot : str
        ``"latest"``, or a snapshot such as ``"2026-10-12"``.

    Returns
    -------
    str
        The rolling release's tag for ``"latest"``, or the snapshot
        release's tag.

    Raises
    ------
    ValueError
        If `snapshot` is neither ``"latest"`` nor a snapshot date.
    """
    if snapshot == "latest":
        return _ROLLING_TAG
    if not _is_snapshot(snapshot):
        raise ValueError(
            f"snapshot must be 'latest' or a date such as '2026-10-12', not "
            f"{snapshot!r}."
        )
    return f"{_SNAPSHOT_TAG_PREFIX}{snapshot}"


def _is_snapshot(value: object) -> bool:
    """Return whether a value names a mirror snapshot, such as ``"2026-10-12"``.

    The date must be a real calendar date.

    Parameters
    ----------
    value : object
        The value to check.

    Returns
    -------
    bool
        ``True`` for a snapshot date on the calendar, with an optional
        ``-<n>`` suffix.
    """
    if not isinstance(value, str):
        return False
    match = _SNAPSHOT.fullmatch(value)
    if match is None:
        return False
    try:
        date.fromisoformat(match["date"])
    except ValueError:
        return False
    return True


def _mirror_asset_url(name: str, snapshot: str) -> str:
    """Return the URL of a file in a mirror release.

    The URL is built from the tag and the name, with no lookup.

    Parameters
    ----------
    name : str
        The file's name, such as ``"2026March.zip"`` or ``"manifest.json"``.
    snapshot : str
        ``"latest"``, or a snapshot such as ``"2026-10-12"``.

    Returns
    -------
    str
        The file's download URL.
    """
    return f"{MIRROR_BASE_URL}{_mirror_tag(snapshot)}/{name}"


class _ZipLinkParser(HTMLParser):
    """Collect the ``href`` of every link on an HTML page.

    `FCAReleaseCatalog.from_html` keeps the links that end in ``.zip``.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record a link's ``href``.

        Tags other than ``a``, and links with no ``href``, are skipped.

        Parameters
        ----------
        tag : str
            The tag name, in lower case.
        attrs : list[tuple[str, str or None]]
            The tag's attributes.
        """
        if tag != "a":
            return
        self.hrefs.extend(
            value.strip() for name, value in attrs if name == "href" and value
        )


@dataclass(frozen=True, kw_only=True)
class FCAReleaseCatalog:
    """The FCA release zips a source holds, keyed by period.

    A catalog says which quarters a source holds and where to download each
    one. Build it from this project's GitHub mirror with `from_github`, or
    from FCA's download page with `from_fca`. A catalog from the mirror also
    carries each zip's SHA-256 and the snapshot it describes. A catalog
    from FCA's page carries neither.

    Attributes
    ----------
    urls : Mapping[ReportingPeriod, str]
        The URL of each period's release zip. It must not be empty.
    sha256 : Mapping[ReportingPeriod, str], optional
        The SHA-256 of each period's release zip, as lower case hex. Every
        period in it must also be in `urls`.
    snapshot : str, optional
        The mirror snapshot the catalog describes, such as
        ``"2026-10-12"``.
    unmatched : tuple[str, ...], optional
        The URLs of zip links whose filenames name no FCA release.

    Raises
    ------
    ValueError
        If `urls` is empty, or `sha256` has a period `urls` does not.

    See Also
    --------
    construct_fca_download_url : Build the FCA URL of a period's release zip.
    construct_fca_mirror_url : Build the GitHub mirror URL of a period's zip.

    Examples
    --------
    >>> march = ReportingPeriod.from_period_end(value="2026-03-31")
    >>> june = ReportingPeriod.from_period_end(value="2026-06-30")
    >>> catalog = FCAReleaseCatalog(
    ...     urls={
    ...         june: "https://example.com/2026June.zip",
    ...         march: "https://example.com/2026March.zip",
    ...     }
    ... )
    >>> catalog
    FCAReleaseCatalog(releases=2, earliest=2026Q1, latest=2026Q2)
    >>> catalog.url_for(period=march)
    'https://example.com/2026March.zip'
    """  # numpydoc ignore=PR01

    urls: Mapping[ReportingPeriod, str]
    sha256: Mapping[ReportingPeriod, str] = field(default_factory=dict)
    snapshot: str | None = None
    unmatched: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Check the catalog and store read-only, sorted copies of its mappings.

        Sorting means a catalog lists its periods oldest first. Copying means a
        mapping the caller keeps cannot change it.

        Raises
        ------
        ValueError
            If `urls` is empty, or `sha256` has a period `urls` does not.
        """
        if not self.urls:
            raise ValueError("A release catalog needs at least one release.")
        extra = sorted(set(self.sha256) - set(self.urls))
        if extra:
            raise ValueError(
                "sha256 has periods with no URL: "
                f"{', '.join(period.label for period in extra)}."
            )
        # Sorted, read-only copies, so a catalog lists its periods oldest
        # first and cannot be changed through a mapping the caller keeps.
        object.__setattr__(
            self, "urls", MappingProxyType(dict(sorted(self.urls.items())))
        )
        object.__setattr__(
            self, "sha256", MappingProxyType(dict(sorted(self.sha256.items())))
        )
        object.__setattr__(self, "unmatched", tuple(self.unmatched))

    def __repr__(self) -> str:
        """Return a compact summary of the catalog's size and span.

        The URLs themselves are left out, since a catalog can hold over a
        hundred.

        Returns
        -------
        str
            The number of releases, the first and last periods, and the
            snapshot if there is one.
        """
        snapshot = f", snapshot={self.snapshot}" if self.snapshot else ""
        return (
            f"FCAReleaseCatalog(releases={len(self.urls)}, "
            f"earliest={self.earliest.label}, latest={self.latest.label}{snapshot})"
        )

    @classmethod
    def from_manifest_json(cls, text: str) -> Self:
        """Return a catalog built from the GitHub mirror's manifest.

        Every mirror release holds a ``manifest.json`` that lists each
        release zip with its SHA-256, and names the snapshot it describes.
        Each URL in the catalog points at that snapshot's release, which
        never changes. So a catalog read from the rolling release still
        downloads the files its checksums describe while the rolling
        release is being updated.

        Parameters
        ----------
        text : str
            The manifest's JSON text.

        Returns
        -------
        Self
            The catalog the manifest lists, with checksums and its snapshot.

        Raises
        ------
        DownloadError
            If the text is not a manifest this version of the package can
            read. A manifest in a newer format asks for an upgrade.

        See Also
        --------
        from_github : Download the mirror's manifest and read it.

        Examples
        --------
        >>> import json
        >>> manifest = {
        ...     "schema_version": 1,
        ...     "snapshot": "2026-10-12",
        ...     "releases": [
        ...         {
        ...             "period": "2026-03-31",
        ...             "file": "2026March.zip",
        ...             "sha256": "ab" * 32,
        ...         },
        ...         {
        ...             "period": "2014-09-30",
        ...             "file": "Sept2014.zip",
        ...             "sha256": "cd" * 32,
        ...         },
        ...     ],
        ... }
        >>> catalog = FCAReleaseCatalog.from_manifest_json(json.dumps(manifest))
        >>> catalog
        FCAReleaseCatalog(releases=2, earliest=2014Q3, latest=2026Q1, snapshot=2026-10-12)
        >>> catalog.url_for(period=catalog.earliest)
        'https://github.com/predict-ably/call-report/releases/download/fca-data-2026-10-12/Sept2014.zip'
        """  # noqa: E501
        manifest = _parse_manifest(text)
        snapshot: str = manifest["snapshot"]
        urls: dict[ReportingPeriod, str] = {}
        checksums: dict[ReportingPeriod, str] = {}
        for entry in manifest["releases"]:
            period, name, checksum = _parse_manifest_release(entry)
            if period in urls:
                raise DownloadError(
                    f"The mirror's manifest lists {period.label} more than once."
                )
            urls[period] = _mirror_asset_url(name, snapshot)
            checksums[period] = checksum
        return cls(urls=urls, sha256=checksums, snapshot=snapshot)

    @classmethod
    def from_github(
        cls,
        *,
        snapshot: str = "latest",
        timeout: float = 60.0,
        max_retries: int = 3,
        http_client: HttpClient = "urllib",
    ) -> Self:
        """Download the GitHub mirror's manifest and return the catalog it lists.

        The manifest comes from the rolling release for ``"latest"``, or from
        the snapshot release for a snapshot date. The catalog's URLs point at
        the snapshot release the manifest names.

        Parameters
        ----------
        snapshot : str, default "latest"
            Which mirror snapshot to download from. ``"latest"`` is the current
            files. A date such as ``"2026-10-12"`` is the mirror as it stood on
            that date. Only ``source="github"`` has snapshots.
        timeout : float, default 60.0
            Seconds to wait for the server before a request fails.
        max_retries : int, default 3
            How many times to send a request again after a transient failure.
        http_client : {"urllib", "curl_cffi"}, default "urllib"
            The library used for HTTP requests. ``"urllib"`` uses the standard
            library. ``"curl_cffi"`` needs
            ``pip install "call-report[curl_cffi]"``.

        Returns
        -------
        Self
            The catalog the snapshot's manifest lists.

        Raises
        ------
        DownloadError
            If the manifest cannot be downloaded or read, or it describes a
            different snapshot from the one asked for.
        ValueError
            If `snapshot` is neither ``"latest"`` nor a snapshot date.

        See Also
        --------
        from_manifest_json : Read a manifest already downloaded.
        from_fca : List the releases on FCA's download page.

        Examples
        --------
        >>> catalog = FCAReleaseCatalog.from_github()  # doctest: +SKIP
        >>> catalog.earliest.label  # doctest: +SKIP
        '2000Q1'
        """
        url = _mirror_asset_url(_MANIFEST_NAME, snapshot)
        body = fetch_bytes(
            url, timeout=timeout, max_retries=max_retries, http_client=http_client
        )
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DownloadError(
                f"The mirror's manifest at {url} is not UTF-8 text."
            ) from error
        catalog = cls.from_manifest_json(text)
        if snapshot != "latest" and catalog.snapshot != snapshot:
            raise DownloadError(
                f"The manifest at {url} describes snapshot {catalog.snapshot}, "
                f"not {snapshot}."
            )
        return catalog

    @classmethod
    def from_html(cls, html: str, *, base_url: str = DOWNLOAD_PAGE_URL) -> Self:
        """Return a catalog built from the HTML of FCA's download page.

        Every link whose path ends in ``.zip`` is read. A link whose filename
        names an FCA release becomes that period's entry. Any other zip link
        is kept on `unmatched`.

        Parameters
        ----------
        html : str
            The page's HTML.
        base_url : str, default DOWNLOAD_PAGE_URL
            The page's own URL, which relative links are resolved against.

        Returns
        -------
        Self
            The catalog the page lists, with no checksums and no snapshot.

        Raises
        ------
        DownloadError
            If the page has no link to a release zip, or links two different
            URLs for one period. Either means the page layout may have
            changed.

        See Also
        --------
        from_fca : Download FCA's download page and read it.

        Examples
        --------
        >>> page = (
        ...     '<p><a href="/template-fca/bank/2026June.zip">June 2026</a></p>'
        ...     '<p><a href="/template-fca/download/CallReportData/Mar2000.zip">'
        ...     "March 2000</a></p>"
        ...     '<p><a href="/files/instructions.zip">Instructions</a></p>'
        ... )
        >>> catalog = FCAReleaseCatalog.from_html(page)
        >>> catalog
        FCAReleaseCatalog(releases=2, earliest=2000Q1, latest=2026Q2)
        >>> catalog.url_for(period=catalog.latest)
        'https://www.fca.gov/template-fca/bank/2026June.zip'
        >>> catalog.unmatched
        ('https://www.fca.gov/files/instructions.zip',)
        """
        parser = _ZipLinkParser()
        parser.feed(html)
        parser.close()
        urls: dict[ReportingPeriod, str] = {}
        unmatched: list[str] = []
        for href in parser.hrefs:
            url = urljoin(base_url, href)
            path = urlsplit(url).path
            if not path.lower().endswith(".zip"):
                continue
            period = parse_fca_release_filename(unquote(path.rsplit("/", 1)[-1]))
            if period is None:
                if url not in unmatched:
                    unmatched.append(url)
                continue
            if urls.setdefault(period, url) != url:
                raise DownloadError(
                    f"FCA's download page links two files for {period.label}: "
                    f"{urls[period]} and {url}. The page layout may have changed."
                )
        if not urls:
            raise DownloadError(
                f"FCA's download page at {base_url} has no link to a release zip. "
                "The page layout may have changed."
            )
        return cls(urls=urls, unmatched=tuple(unmatched))

    @classmethod
    def from_fca(
        cls,
        *,
        timeout: float = 60.0,
        max_retries: int = 3,
        http_client: HttpClient = "urllib",
    ) -> Self:
        """Download FCA's download page and return the catalog it lists.

        The page is `DOWNLOAD_PAGE_URL`. Its links are read by `from_html`,
        so the catalog has no checksums and no snapshot.

        Parameters
        ----------
        timeout : float, default 60.0
            Seconds to wait for the server before a request fails.
        max_retries : int, default 3
            How many times to send a request again after a transient failure.
        http_client : {"urllib", "curl_cffi"}, default "urllib"
            The library used for HTTP requests. ``"urllib"`` uses the standard
            library. ``"curl_cffi"`` needs
            ``pip install "call-report[curl_cffi]"``.

        Returns
        -------
        Self
            The catalog the page lists, with no checksums and no snapshot.

        Raises
        ------
        DownloadError
            If the page cannot be downloaded, or it has no link to a release
            zip.

        See Also
        --------
        from_html : Read a copy of the page already downloaded.
        from_github : List the releases on this project's GitHub mirror.

        Examples
        --------
        >>> catalog = FCAReleaseCatalog.from_fca()  # doctest: +SKIP
        >>> catalog.earliest.label  # doctest: +SKIP
        '2000Q1'
        """
        page_url = DOWNLOAD_PAGE_URL
        body = fetch_bytes(
            page_url, timeout=timeout, max_retries=max_retries, http_client=http_client
        )
        return cls.from_html(body.decode("utf-8", errors="replace"), base_url=page_url)

    @property
    def periods(self) -> tuple[ReportingPeriod, ...]:
        """Return every period in the catalog, oldest first.

        A gap between two periods means the source holds no release for the
        quarters in between.

        Returns
        -------
        tuple[ReportingPeriod, ...]
            The catalog's periods.

        Examples
        --------
        >>> catalog = FCAReleaseCatalog.from_html(
        ...     '<a href="2026June.zip"></a><a href="2026March.zip"></a>'
        ... )
        >>> [period.label for period in catalog.periods]
        ['2026Q1', '2026Q2']
        """
        return tuple(self.urls)

    @property
    def earliest(self) -> ReportingPeriod:
        """Return the oldest period in the catalog.

        A catalog is never empty, so it always has an oldest period.

        Returns
        -------
        ReportingPeriod
            The first period.

        Examples
        --------
        >>> catalog = FCAReleaseCatalog.from_html(
        ...     '<a href="Dec2014.zip"></a><a href="2015March.zip"></a>'
        ... )
        >>> catalog.earliest.label
        '2014Q4'
        """
        return self.periods[0]

    @property
    def latest(self) -> ReportingPeriod:
        """Return the newest period in the catalog.

        This is the newest quarter the source holds, so it shows whether a
        source has a quarter before anything is downloaded.

        Returns
        -------
        ReportingPeriod
            The last period.

        Examples
        --------
        >>> catalog = FCAReleaseCatalog.from_html(
        ...     '<a href="Dec2014.zip"></a><a href="2015March.zip"></a>'
        ... )
        >>> catalog.latest.label
        '2015Q1'
        """
        return self.periods[-1]

    def url_for(self, *, period: ReportingPeriod) -> str:
        """Return the URL of a period's release zip.

        A period the catalog does not hold raises, rather than returning a
        URL that would not be found.

        Parameters
        ----------
        period : ReportingPeriod
            The period to look up.

        Returns
        -------
        str
            The URL of the period's release zip.

        Raises
        ------
        PeriodNotAvailableError
            If the catalog does not hold `period`.

        Examples
        --------
        >>> catalog = FCAReleaseCatalog.from_html('<a href="2026March.zip"></a>')
        >>> catalog.url_for(period=ReportingPeriod.from_period_end(value="2026-03-31"))
        'https://www.fca.gov/bank-oversight/2026March.zip'
        >>> catalog.url_for(period=ReportingPeriod.from_period_end(value="2026-06-30"))
        Traceback (most recent call last):
        ...
        call_report.exceptions.PeriodNotAvailableError: The catalog has no release for 2026Q2. It holds 2026Q1 to 2026Q1.
        """  # noqa: E501
        try:
            return self.urls[period]
        except KeyError:
            raise PeriodNotAvailableError(
                f"The catalog has no release for {period.label}. It holds "
                f"{self.earliest.label} to {self.latest.label}."
            ) from None


def _parse_manifest(text: str) -> dict[str, Any]:
    """Parse the mirror's manifest and check its top-level keys.

    Each release entry is checked by `_parse_manifest_release`.

    Parameters
    ----------
    text : str
        The manifest's JSON text.

    Returns
    -------
    dict[str, Any]
        The parsed manifest. Its ``schema_version`` is one this package
        reads, its ``snapshot`` names a snapshot, and its ``releases`` is a
        list that is not empty.

    Raises
    ------
    DownloadError
        If the text is not a manifest this version of the package can read.
    """
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as error:
        raise DownloadError(
            f"The mirror's manifest is not valid JSON: {error}."
        ) from error
    if not isinstance(manifest, dict):
        raise DownloadError("The mirror's manifest is not a JSON object.")
    version = _manifest_value(manifest, "schema_version", "the manifest")
    if isinstance(version, int) and version > _MANIFEST_SCHEMA_VERSION:
        raise DownloadError(
            f"The mirror's manifest uses format version {version}, which is newer "
            f"than this version of call-report reads ({_MANIFEST_SCHEMA_VERSION}). "
            "Upgrade call-report to read it."
        )
    if version != _MANIFEST_SCHEMA_VERSION:
        raise DownloadError(
            f"The mirror's manifest has an unknown schema_version, {version!r}."
        )
    snapshot = _manifest_value(manifest, "snapshot", "the manifest")
    if not _is_snapshot(snapshot):
        raise DownloadError(
            f"The mirror's manifest names an invalid snapshot, {snapshot!r}."
        )
    releases = _manifest_value(manifest, "releases", "the manifest")
    if not isinstance(releases, list) or not releases:
        raise DownloadError("The mirror's manifest lists no releases.")
    return manifest


def _parse_manifest_release(entry: object) -> tuple[ReportingPeriod, str, str]:
    """Read one release entry of the mirror's manifest.

    Keys the package does not read, such as ``size``, are not checked.

    Parameters
    ----------
    entry : object
        One item of the manifest's ``releases`` list.

    Returns
    -------
    tuple[ReportingPeriod, str, str]
        The release's period, its zip's filename, and its SHA-256.

    Raises
    ------
    DownloadError
        If the entry is not an object, misses a key, names an invalid
        period, a filename that is not that period's, or a checksum that is
        not lower case SHA-256 hex.
    """
    if not isinstance(entry, dict):
        raise DownloadError("A release in the mirror's manifest is not a JSON object.")
    value = _manifest_value(entry, "period", "a release")
    try:
        period = ReportingPeriod.from_period_end(value=str(value))
    except InvalidPeriodError as error:
        raise DownloadError(
            f"A release in the mirror's manifest has an invalid period, {value!r}."
        ) from error
    name = _manifest_value(entry, "file", f"the {period.label} release")
    expected = f"{fca_release_stem(period=period)}.zip"
    if name != expected:
        raise DownloadError(
            f"The mirror's manifest names {name!r} for {period.label}, not "
            f"{expected!r}."
        )
    checksum = _manifest_value(entry, "sha256", f"the {period.label} release")
    if not isinstance(checksum, str) or _SHA256.fullmatch(checksum) is None:
        raise DownloadError(
            f"The mirror's manifest has an invalid sha256 for {period.label}."
        )
    return period, expected, checksum


def _manifest_value(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    """Return a required value from the mirror's manifest.

    A missing key raises an error that names it.

    Parameters
    ----------
    mapping : Mapping[str, Any]
        The manifest, or one release entry of it.
    key : str
        The key to read.
    where : str
        What `mapping` is, for the error message, such as ``"the manifest"``.

    Returns
    -------
    Any
        The value.

    Raises
    ------
    DownloadError
        If `key` is missing.
    """
    try:
        return mapping[key]
    except KeyError:
        raise DownloadError(
            f"The mirror's manifest is missing {key!r} in {where}."
        ) from None
