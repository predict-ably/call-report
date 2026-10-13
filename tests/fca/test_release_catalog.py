"""Tests for FCAReleaseCatalog (call_report.fca.catalog).

No test reaches the internet. The catalog's network constructors read from
the local server through the ``mirror`` and ``fca_site`` fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from call_report.core import ReportingPeriod
from call_report.exceptions import (
    DownloadError,
    PeriodNotAvailableError,
    ReleaseCatalogError,
)
from call_report.fca import catalog
from call_report.fca.catalog import FCAReleaseCatalog
from tests.helpers import archived_periods
from tests.http_server import LocalHTTPServer

_DATA = Path(__file__).parent / "data"
_PAGE = (_DATA / "download_page.html").read_text(encoding="utf-8")
_MANIFEST = (_DATA / "manifest.json").read_text(encoding="utf-8")
_MIRROR = "https://github.com/predict-ably/call-report/releases/download/"

MAR2000 = ReportingPeriod.from_period_end(value="2000-03-31")
SEP2014 = ReportingPeriod.from_period_end(value="2014-09-30")
JUN2026 = ReportingPeriod.from_period_end(value="2026-06-30")


def _manifest(**changes: Any) -> dict[str, Any]:
    """Return the fixture manifest as a dict, with top-level keys replaced."""
    manifest: dict[str, Any] = json.loads(_MANIFEST)
    manifest.update(changes)
    return manifest


def _with_release(**changes: Any) -> str:
    """Return the fixture manifest's text with its first release's keys replaced.

    A key given the value ``None`` is removed.
    """
    manifest = _manifest()
    release = manifest["releases"][0]
    for key, value in changes.items():
        if value is None:
            del release[key]
        else:
            release[key] = value
    return json.dumps(manifest)


# ---------------------------------------------------------------------------
# The catalog itself
# ---------------------------------------------------------------------------


def test_catalog_sorts_its_periods_oldest_first() -> None:
    """periods, earliest, and latest follow the calendar, not insertion order."""
    found = FCAReleaseCatalog(
        urls={JUN2026: "c", MAR2000: "a", SEP2014: "b"},
        sha256={JUN2026: "3", MAR2000: "1"},
    )
    assert found.periods == (MAR2000, SEP2014, JUN2026)
    assert (found.earliest, found.latest) == (MAR2000, JUN2026)
    assert list(found.sha256) == [MAR2000, JUN2026]


def test_catalog_mappings_are_read_only_copies() -> None:
    """Changing the dict passed in, or the catalog's own mapping, cannot alter it."""
    urls = {MAR2000: "a"}
    found = FCAReleaseCatalog(urls=urls)
    urls[JUN2026] = "c"
    assert found.periods == (MAR2000,)
    with pytest.raises(TypeError):
        found.urls[JUN2026] = "c"  # type: ignore[index]


def test_catalog_needs_a_release() -> None:
    """An empty catalog is never built, so earliest and latest always exist."""
    with pytest.raises(ValueError, match="at least one release"):
        FCAReleaseCatalog(urls={})


def test_catalog_rejects_a_checksum_with_no_url() -> None:
    """Every checksum must belong to a period the catalog can download."""
    with pytest.raises(ValueError, match="2026Q2"):
        FCAReleaseCatalog(urls={MAR2000: "a"}, sha256={JUN2026: "3"})


def test_catalog_cannot_be_hashed() -> None:
    """hash() raises at once, since a catalog holds mappings.

    A frozen dataclass invites use as a set member or a cache key. Without
    an explicit opt out, the generated __hash__ would fail later, on the
    mappings, with a message about a dict.
    """
    found = FCAReleaseCatalog(urls={MAR2000: "a"})
    with pytest.raises(TypeError, match="FCAReleaseCatalog"):
        hash(found)
    assert found == FCAReleaseCatalog(urls={MAR2000: "a"})


def test_bad_text_is_also_a_value_error() -> None:
    """A caller that expects ValueError for bad input text gets one."""
    with pytest.raises(ValueError, match="not valid JSON"):
        FCAReleaseCatalog.from_manifest_json("{not json")


def test_catalog_repr_summarizes_it() -> None:
    """The repr gives the size, the span, and the snapshot when there is one."""
    plain = FCAReleaseCatalog(urls={MAR2000: "a", JUN2026: "c"})
    assert repr(plain) == (
        "FCAReleaseCatalog(releases=2, earliest=2000Q1, latest=2026Q2)"
    )
    pinned = FCAReleaseCatalog(urls={MAR2000: "a"}, snapshot="2026-10-12")
    assert repr(pinned).endswith("snapshot=2026-10-12)")


def test_url_for_names_the_catalog_range_for_a_missing_period() -> None:
    """A period the catalog lacks raises PeriodNotAvailableError with its range."""
    found = FCAReleaseCatalog(urls={MAR2000: "a", JUN2026: "c"})
    with pytest.raises(PeriodNotAvailableError, match="2000Q1 to 2026Q2"):
        found.url_for(period=SEP2014)


# ---------------------------------------------------------------------------
# from_manifest_json
# ---------------------------------------------------------------------------


def test_from_manifest_json_reads_urls_checksums_and_snapshot() -> None:
    """Each URL points at the snapshot the manifest names, never the rolling tag.

    The rolling release's files are replaced one at a time while the mirror
    updates. The snapshot release never changes, so its files always match
    the manifest's checksums.
    """
    found = FCAReleaseCatalog.from_manifest_json(_MANIFEST)
    assert found.periods == (MAR2000, SEP2014, JUN2026)
    assert found.snapshot == "2026-10-12"
    assert found.url_for(period=SEP2014) == (
        f"{_MIRROR}fca-data-2026-10-12/Sept2014.zip"
    )
    assert found.sha256[JUN2026] == "3" * 64
    assert found.unmatched == ()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "not a JSON object"),
        (json.dumps(_manifest(schema_version="1")), "unknown schema_version"),
        (json.dumps(_manifest(schema_version=True)), "unknown schema_version"),
        (json.dumps(_manifest(snapshot="latest")), "invalid snapshot"),
        (json.dumps(_manifest(snapshot=20261012)), "invalid snapshot"),
        (json.dumps(_manifest(releases=[])), "lists no releases"),
        (json.dumps(_manifest(releases={})), "lists no releases"),
        (json.dumps(_manifest(releases=["2026June.zip"])), "not a JSON object"),
        (_with_release(period=None), "missing 'period' in a release"),
        (_with_release(period="2000-04-30"), "invalid period"),
        (_with_release(period=20000331), "invalid period"),
        (_with_release(file=None), "missing 'file' in the 2000Q1 release"),
        (_with_release(file="2000March.zip"), "names '2000March.zip' for 2000Q1"),
        (_with_release(sha256=None), "missing 'sha256' in the 2000Q1 release"),
        (_with_release(sha256="A" * 64), "invalid sha256 for 2000Q1"),
        (_with_release(sha256="1" * 63), "invalid sha256 for 2000Q1"),
        (_with_release(period="2026-06-30", file="2026June.zip"), "more than once"),
    ],
)
def test_from_manifest_json_rejects_a_bad_manifest(text: str, message: str) -> None:
    """A manifest the package cannot trust raises ReleaseCatalogError saying why."""
    with pytest.raises(ReleaseCatalogError, match=message):
        FCAReleaseCatalog.from_manifest_json(text)


@pytest.mark.parametrize("key", ["schema_version", "snapshot", "releases"])
def test_from_manifest_json_names_a_missing_top_level_key(key: str) -> None:
    """A manifest without one of its required keys names the key."""
    manifest = _manifest()
    del manifest[key]
    with pytest.raises(ReleaseCatalogError, match=f"missing '{key}' in the manifest"):
        FCAReleaseCatalog.from_manifest_json(json.dumps(manifest))


def test_from_manifest_json_asks_for_an_upgrade_for_a_newer_format() -> None:
    """A manifest in a newer format says to upgrade call-report."""
    text = json.dumps(_manifest(schema_version=2))
    with pytest.raises(ReleaseCatalogError, match="Upgrade call-report"):
        FCAReleaseCatalog.from_manifest_json(text)


# ---------------------------------------------------------------------------
# from_github
# ---------------------------------------------------------------------------


def _publish(mirror: Path, tag: str, text: str) -> None:
    """Publish a manifest as an asset of a mirror release on the local server."""
    folder = mirror / tag
    folder.mkdir()
    (folder / "manifest.json").write_text(text, encoding="utf-8")


def test_from_github_reads_the_rolling_manifest(
    mirror: Path, http_server: LocalHTTPServer
) -> None:
    """With no snapshot, the manifest comes from the rolling release."""
    _publish(mirror, "fca-call-report-data", _MANIFEST)
    found = FCAReleaseCatalog.from_github()
    assert found.snapshot == "2026-10-12"
    assert found.url_for(period=MAR2000) == http_server.url(
        "releases/fca-data-2026-10-12/Mar2000.zip"
    )
    assert http_server.paths_requested() == [
        "/releases/fca-call-report-data/manifest.json"
    ]


def test_from_github_reads_a_pinned_snapshot(
    mirror: Path, http_server: LocalHTTPServer
) -> None:
    """A snapshot reads that snapshot release's own manifest."""
    text = json.dumps(_manifest(snapshot="2026-08-31"))
    _publish(mirror, "fca-data-2026-08-31", text)
    found = FCAReleaseCatalog.from_github(snapshot="2026-08-31")
    assert found.snapshot == "2026-08-31"
    assert http_server.paths_requested() == [
        "/releases/fca-data-2026-08-31/manifest.json"
    ]


def test_from_github_rejects_a_manifest_for_another_snapshot(mirror: Path) -> None:
    """A snapshot release whose manifest names another snapshot is not trusted."""
    _publish(mirror, "fca-data-2026-08-31", _MANIFEST)
    with pytest.raises(ReleaseCatalogError, match="describes snapshot 2026-10-12"):
        FCAReleaseCatalog.from_github(snapshot="2026-08-31")


def test_from_github_rejects_a_manifest_that_is_not_utf8(mirror: Path) -> None:
    """Bytes that are not UTF-8 text raise ReleaseCatalogError naming the URL."""
    folder = mirror / "fca-call-report-data"
    folder.mkdir()
    (folder / "manifest.json").write_bytes(b"\xff\xfe{")
    with pytest.raises(ReleaseCatalogError, match="not UTF-8 text"):
        FCAReleaseCatalog.from_github()


def test_from_github_raises_when_the_mirror_has_no_manifest(mirror: Path) -> None:
    """A missing manifest is a DownloadError, never an empty catalog.

    It is not a ReleaseCatalogError, since nothing was read.
    """
    with pytest.raises(DownloadError, match="404") as caught:
        FCAReleaseCatalog.from_github()
    assert not isinstance(caught.value, ReleaseCatalogError)


def test_from_github_rejects_an_invalid_snapshot() -> None:
    """An invalid snapshot raises ValueError before any request is made."""
    with pytest.raises(ValueError, match="snapshot must be 'latest'"):
        FCAReleaseCatalog.from_github(snapshot="last week")


def test_from_github_and_from_fca_pass_their_request_options_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout, max_retries, and http_client reach the HTTP helper."""
    calls: list[dict[str, Any]] = []

    def fake_fetch(url: str, **kwargs: Any) -> bytes:
        calls.append(kwargs)
        return _MANIFEST.encode() if url.endswith(".json") else _PAGE.encode()

    monkeypatch.setattr(catalog, "fetch_bytes", fake_fetch)
    options = {"timeout": 5.0, "max_retries": 1, "http_client": "curl_cffi"}
    FCAReleaseCatalog.from_github(**options)  # type: ignore[arg-type]
    FCAReleaseCatalog.from_fca(**options)  # type: ignore[arg-type]
    assert calls == [options, options]


# ---------------------------------------------------------------------------
# from_html and from_fca
# ---------------------------------------------------------------------------


def test_from_html_lists_every_quarter_on_the_saved_page() -> None:
    """The saved page lists one link per archived quarter, from 2000Q1 on."""
    found = FCAReleaseCatalog.from_html(_PAGE)
    assert found.periods == archived_periods()
    assert found.sha256 == {}
    assert found.snapshot is None


def test_from_html_resolves_relative_and_absolute_links() -> None:
    """Relative links resolve against the page URL, and absolute ones are kept."""
    found = FCAReleaseCatalog.from_html(_PAGE)
    assert found.url_for(period=JUN2026) == (
        "https://www.fca.gov/template-fca/bank/2026June.zip"
    )
    assert found.url_for(period=SEP2014) == (
        "https://www.fca.gov/template-fca/download/CallReportData/Sept2014.zip"
    )


def test_from_html_keeps_zip_links_that_name_no_release() -> None:
    """A zip link whose name is not a release lands on unmatched, once."""
    page = _PAGE + '<a href="/template-fca/bank/CallReportInstructions.zip">x</a>'
    found = FCAReleaseCatalog.from_html(page)
    assert found.unmatched == (
        "https://www.fca.gov/template-fca/bank/CallReportInstructions.zip",
    )


def test_from_html_ignores_other_links_and_repeats() -> None:
    """Links that are not zips are skipped, and a repeated link counts once."""
    page = (
        '<a href="2026June.zip">June</a><a href="2026June.zip">June again</a>'
        '<a href="notes.pdf">Notes</a><a name="anchor">No href</a><a href="">x</a>'
        "<p>2026March.zip</p>"
    )
    found = FCAReleaseCatalog.from_html(page, base_url="https://example.com/data/")
    assert found.urls == {JUN2026: "https://example.com/data/2026June.zip"}


def test_from_html_ignores_a_query_string_and_letter_case() -> None:
    """The filename is read from the URL path, in any letter case."""
    page = '<A HREF="/files/2026JUNE.ZIP?download=1">June</A>'
    found = FCAReleaseCatalog.from_html(page)
    assert found.url_for(period=JUN2026) == (
        "https://www.fca.gov/files/2026JUNE.ZIP?download=1"
    )


def test_from_html_rejects_two_files_for_one_quarter() -> None:
    """Two different links for one quarter mean the page layout has changed."""
    page = '<a href="/a/2026June.zip">June</a><a href="/b/2026June.zip">June</a>'
    with pytest.raises(ReleaseCatalogError, match="two files for 2026Q2"):
        FCAReleaseCatalog.from_html(page)


def test_from_html_rejects_a_page_with_no_release_links() -> None:
    """A page with no release zip raises rather than returning an empty catalog."""
    page = '<a href="instructions.zip">Instructions</a>'
    with pytest.raises(ReleaseCatalogError, match="layout may have changed"):
        FCAReleaseCatalog.from_html(page)


def test_from_fca_reads_the_download_page(
    fca_site: Path, http_server: LocalHTTPServer
) -> None:
    """The page is downloaded, and its links resolve against its own URL."""
    page = fca_site / "bank-oversight" / "call-report-data-for-download"
    page.parent.mkdir()
    page.write_text(_PAGE, encoding="utf-8")
    found = FCAReleaseCatalog.from_fca()
    assert found.periods == archived_periods()
    assert found.url_for(period=JUN2026) == http_server.url(
        "template-fca/bank/2026June.zip"
    )
