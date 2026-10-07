# Plan: issue #119, Download FCA call report releases with a downloader and a transport

Status: proposed

## Goal

Give an installed user a way to get FCA release files from the package itself.
This is goal 5 of the v0.2 release, and the last goal before 0.2.0 is cut
(#64). It is tracked in #54.

In scope:

1. A shared, standard library HTTP helper with retries, and a shared zip
   extraction helper, in `call_report.core`.
2. `FCAReleaseCatalog`, which maps each published quarter to its zip URL. It
   is built from the hardcoded snapshot by default, or by scraping FCA's
   download page when the user opts in.
3. `FCAReleaseDownloader`, which downloads a range of quarters into a
   directory the user chooses, verifies each zip, and extracts it to
   `data_dir/<zip stem>/`. It records failures on its own state and can retry
   them.
4. `DownloadTransport`, a thin `FCATransport` that asks a downloader for each
   period and then reads it with `LocalDirectoryTransport`.
5. An `available_periods()` method on every transport. `FCACallReport`
   validates a requested range against the transport rather than against
   `LATEST_KNOWN_PERIOD`.
6. Tests that never touch the internet in the default suite, and an opt-in
   test of the live FCA site.
7. A user guide page on downloading, and updates to the getting started page,
   the API reference, and the changelog.

Out of scope:

- Merger adjustment (version 0.3).
- Regenerating the shipped schedule metadata or institution registry for a
  quarter found by discovery. Those still come with a package release.
- Downloading in parallel from inside `FCACallReport.fetch`. A user who wants
  parallel downloads calls the downloader first.
- Checksums. FCA does not publish any. See "Open questions".
- FFIEC, FDIC, and NCUA.

## Why

### The gap

Two transports ship today. `LocalDirectoryTransport` reads releases the user
has already downloaded and extracted. `PackagedArchiveTransport` reads the zips
in `data/fca-call-report/`, which are not in the wheel. So an installed user
has to download and unzip every quarter by hand. `construct_fca_download_url`
already builds each URL and `DownloadError` already exists, but nothing
fetches a file.

### Cloudflare findings

PENDING. The development container's network policy blocks `www.fca.gov`, so
the probe has not run yet. This section will record, for the modern era
(`2026March.zip`) and the legacy era (`Sept2014.zip`) URLs and for the
download page:

- the status code and headers from plain `urllib.request`,
- whether a browser-like `User-Agent` and `Accept` header change the result,
- whether the response is the zip or a Cloudflare challenge page
  (`cf-mitigated: challenge`, or HTML where a zip is expected).

The plan then follows one of three branches.

| Probe result | What the implementation does |
|---|---|
| Plain `urllib` gets the zip | Use it as is. |
| Only browser-like headers get the zip | Send those headers from the shared HTTP helper. |
| Challenged or blocked either way | Keep the downloader, but point the catalog at a mirror we control (see "Open questions"). |

In every branch the shared HTTP helper detects a challenge page and raises a
`DownloadError` that names Cloudflare, rather than writing an HTML page to
disk as if it were a zip.

### Two layers over one implementation

The issue asks for a downloader and a transport, with only the downloader
fetching files. A downloader alone would make every user manage two objects
for one task. A transport alone would leave no clean way to download ahead of
time, fill a cache in CI or on an air-gapped machine, or recover from a
blocked download. With both, the downloader does the work and
`DownloadTransport` only calls it.

Both write the same folder layout that `LocalDirectoryTransport` reads
(`data_dir/<zip stem>/`). So downloaded and hand-extracted releases are read
by one code path, and only that path needs regression testing against
`PackagedArchiveTransport`.

### Standard library only

| Client | New dependencies | Concurrency | Retries |
|---|---|---|---|
| `urllib.request` | none | `concurrent.futures` threads | written here |
| `requests` | urllib3, certifi, idna, charset-normalizer | threads | through urllib3 |
| `httpx` | httpcore, h11, anyio, certifi, idna | threads or async | connection errors only |
| `curl_cffi` | one binary wheel | threads or async | written here |

Downloads are a few dozen files of about 100 KB each (the whole archive is
12 MB). Connection pooling and HTTP/2 do not matter at that size. If
Cloudflare blocks by TLS fingerprint, `urllib`, `requests`, and `httpx` all
fail the same way, so a third party pure Python client would add dependencies
without changing the outcome. Only `curl_cffi`, which copies a browser's TLS
fingerprint, would. It is not proposed here. If the probe shows it is needed,
it is raised as a new optional dependency and loaded through
`import_optional`.

Python releases the GIL during network reads, so a thread pool downloads
several files at once without async code. The pool is small by default (4
workers) so the package does not hammer a government site.

### Discovery is an object the user asks for

`LATEST_KNOWN_PERIOD` is a hardcoded snapshot. A user who wants a quarter FCA
published after the package was released needs a way to find it.

The page can be scraped with `html.parser.HTMLParser` and a regular expression,
so discovery needs no dependency. It is still kept separate and opt in, as a
catalog object, rather than a `discover=True` flag on the downloader, because:

- one scrape serves the downloader, the transport, and the report,
- parsing (`from_html`) is pure, so tests feed it a saved page,
- the user can look at `catalog.latest` before downloading anything,
- a failed scrape raises where the user asked for it, instead of a flag
  quietly falling back to the snapshot,
- a mirror, if one is needed, is one more way to build a catalog, and nothing
  downstream changes,
- a maintainer can compare a scraped catalog with the snapshot to see when a
  new quarter has appeared.

The default stays the snapshot, so the default behavior makes no network call
beyond the zips themselves.

### Each transport says which periods it can supply

`FCACallReport.fetch` rejects a range that ends after `LATEST_KNOWN_PERIOD`,
and `FCACallReport.available_periods()` returns the snapshot. That is wrong
for every transport once quarters can come from anywhere other than the
snapshot. A user who drops a new zip into `data/fca-call-report/`, or extracts
one into their own folder, cannot read it until a package release bumps the
constant.

So every transport gains `available_periods()`, and the report asks the
transport.

| Transport | `available_periods()` comes from |
|---|---|
| `PackagedArchiveTransport` | the zips present in `archive_root` |
| `LocalDirectoryTransport` | the release folders present in `data_dir` |
| `DownloadTransport` | its catalog |

The catalog stays a download concept. It maps periods to URLs, and a local
transport has no URLs. What every transport shares is the answer to "which
periods can you supply", not a catalog.

Adding a method to the `FCATransport` protocol breaks a transport a user has
written. The package is pre-1.0 alpha and the protocol shipped in 0.1.0, so
this plan accepts that and records it in the changelog. See "Open questions"
for the softer alternative.

### Failures are recorded, and can be retried

A failed quarter in a range does not stop the others. It is recorded on the
downloader, the same way `FCACallReport.fetch` records an unresolvable period
in `errors_`. Nothing in `src/` retries today. This plan adds two layers.

- Automatic retries inside the HTTP helper, for transient failures only.
- A `retry_failed()` method that downloads again only the quarters in
  `errors_`.

### Alternatives considered

- **Ship the zips in the wheel.** Simple and offline, but the wheel grows
  every quarter, and a user who wants one quarter installs all of them.
- **A mirror we control from the start.** Reliable, but someone must keep it
  in step with FCA. It is the fallback if the probe shows direct download is
  not reliable.
- **Keep downloads manual.** It works today and stays supported, because the
  downloader uses the same folder layout.

## Approach

### New shared module: `src/call_report/core/_http.py`

Standard library only. FFIEC, FDIC, and NCUA will reuse it.

```python
def fetch_bytes(
    url: str,
    *,
    timeout: float = 60.0,
    max_retries: int = 3,
    headers: Mapping[str, str] | None = None,
) -> bytes: ...


def download_file(
    url: str,
    *,
    destination: Path,
    timeout: float = 60.0,
    max_retries: int = 3,
    headers: Mapping[str, str] | None = None,
) -> Path: ...
```

- Only `http` and `https` URLs are accepted, checked before `urlopen` is
  called. That check is what makes the narrow `# noqa: S310` on the
  `urlopen` line correct.
- `download_file` streams to a temporary file in the destination's folder and
  renames it into place, so a failed download never leaves a partial file.
- Retries cover connection errors, timeouts, HTTP 429, and HTTP 5xx, with
  exponential backoff starting at 1 second. A `Retry-After` header on a 429 or
  503 is honored. A 404 or other 4xx is not retried.
- A response that looks like a Cloudflare challenge raises `DownloadError`
  that names Cloudflare, and is not retried.
- The sleep function is a module attribute, so tests replace it and never
  wait.
- Every failure is raised as `DownloadError`, with the URL and the reason.

### New shared module: `src/call_report/core/_archive.py`

```python
def extract_zip(*, archive: Path | bytes, target_dir: Path) -> Path: ...
```

- Checks the file is a zip (`zipfile.is_zipfile`) and that `testzip()` finds
  no corrupt member, and raises `DownloadError` otherwise.
- Refuses a member whose path is absolute or climbs out of the target with
  `..`.
- Extracts into a temporary sibling folder and renames it to `target_dir`,
  so a folder that exists is always a complete extraction.
- `PackagedArchiveTransport.resolve` switches to this helper. Its behavior
  does not change.

### Changes in `src/call_report/fca/catalog.py`

- `fca_release_stem(*, period)` returns a period's zip stem (`"2026March"`,
  `"Sept2014"`) with no bounds check. `_default_dirname` in `transport.py`
  calls it, so a local transport can name a folder for a quarter newer than
  the snapshot.
- `construct_fca_download_url` keeps its bounds check and its output. It is
  the snapshot's URL rule.
- `parse_fca_release_filename(name)` turns a zip filename into a period, or
  returns `None`. It accepts both eras, any letter case, and `Sep` as well as
  `Sept`.
- `FCAReleaseCatalog` is added (see "Public API").
- The scrape uses a private `HTMLParser` subclass that collects every `href`
  ending in `.zip`, resolved with `urllib.parse.urljoin`. Links whose
  filenames parse become catalog entries. Links that do not parse are kept on
  `unmatched`.
- A scraped catalog is the snapshot with the scraped entries laid over it.
  A scraped URL replaces the predicted one for the same quarter, and a quarter
  the page lists after the snapshot is added. A quarter the snapshot has but
  the page omits keeps its predicted URL.
- A page with no parsable zip links raises `DownloadError` saying the page
  layout may have changed. An empty catalog is never returned.
- PENDING: the parser is checked against the real page markup once the probe
  runs.

### New public module: `src/call_report/fca/download.py`

Holds `FCAReleaseDownloader`. In outline:

```python
def download(self, *, start, end) -> Self:
    periods = PeriodRange(start=start, end=end)
    catalog = self.catalog or FCAReleaseCatalog.from_snapshot()
    for period in (periods[0], periods[-1]):
        catalog.url_for(period=period)  # raises PeriodNotAvailableError

    to_fetch, skipped = [], []
    for period in periods:
        if not self.overwrite and self._target(period).is_dir():
            skipped.append(period)
        else:
            to_fetch.append(period)

    with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
        outcomes = pool.map(self._download_one, to_fetch)  # each returns ok or FCAIssue

    self.periods_, self.skipped_ = periods, tuple(skipped)
    self.downloaded_, self.errors_ = ...
    if to_fetch and not self.downloaded_:
        raise DownloadError(
            "None of the ... quarter(s) could be downloaded. See errors_."
        )
    return self
```

- `_download_one` downloads the zip to a temporary file in `data_dir`, checks
  it holds at least one `D_*.TXT` layout file, and extracts it with
  `extract_zip`.
- With `overwrite=True`, the new release is extracted to a temporary folder
  first. Only once it is complete is the old folder moved aside, the new one
  renamed into place, and the old one removed. A failed download leaves the
  quarter already on disk untouched.
- Each worker writes only its own quarter's folder, and results are collected
  in the calling thread, so no lock is needed.
- `errors_` uses the existing `FCAIssue` with `schedule=None`. `FCAIssue`
  moves from `report.py` to a small private module so `download.py` can
  import it without importing the report. It stays exported where it is
  today.
- The state is set before the "every quarter failed" error is raised, so
  `retry_failed()` still works after it.
- `data_dir` is expanded with `Path.expanduser()` and created if missing.

### Changes in `src/call_report/fca/transport.py`

- `FCATransport` gains `available_periods(self) -> tuple[ReportingPeriod, ...]`.
- `LocalDirectoryTransport.available_periods()` checks, for each quarter from
  `EARLIEST_PERIOD` through the quarter containing today, whether
  `data_dir / dirname_for(period)` is a folder. That is about 107 checks, and
  it works for any `dirname_for` because it never reverses a folder name.
- `PackagedArchiveTransport.available_periods()` does the same for
  `archive_root / f"{dirname_for(period)}.zip"`.
- `DownloadTransport` is added (see "Public API"). It builds one
  `FCAReleaseDownloader` with `max_workers=1` and the same `cache_dir`,
  `overwrite`, and `catalog`. `resolve` downloads the period if needed and
  then returns `LocalDirectoryTransport(data_dir=cache_dir).resolve(period=...)`.
- `DownloadTransport` keeps a set of periods it has already resolved. With
  `overwrite=True`, a period is downloaded again only on its first `resolve`
  on that transport instance. So one `fetch()` never downloads a quarter
  twice, and `fetch()` again on the same report reads the folder already on
  disk. A new transport instance downloads again.
- The module docstring loses its sentence about a future network transport.

### Changes in `src/call_report/fca/report.py`

- `fetch` keeps the lower bound check against `EARLIEST_PERIOD`. It replaces
  the upper bound check against `LATEST_KNOWN_PERIOD` with the transport's
  latest available period. A range ending after it raises
  `PeriodNotAvailableError`. A transport with no available periods raises
  `DownloadError` naming where it looked.
- A gap inside the range, such as a missing folder, is still recorded in
  `errors_`, as today.
- `available_periods()` returns `self.transport.available_periods()`. It
  still does not need `fetch` to have run.
- `get_schema` reads the shipped metadata, which stops at the snapshot. For a
  quarter newer than the snapshot it raises a clear `ScheduleNotFoundError`
  saying the shipped metadata does not cover that quarter yet, and pointing
  to `get_layout`, which reads the release itself. `load`, the reshaping
  methods, and `institutions()` read the release's own files and work
  unchanged.

### FCA call report disclosures

PENDING. The disclosures page is blocked from the development container too.
This section will list each disclosed issue that changes what a downloaded
release contains or how it parses, and say whether the downloader, the
parser, or the documentation handles it.

## Public API

New in `call_report.fca.catalog`, and `FCAReleaseCatalog` also exported from
`call_report.fca`:

```python
def fca_release_stem(*, period: ReportingPeriod) -> str:
    """Return the stem of a period's FCA release zip, such as "2026March"."""


def parse_fca_release_filename(name: str) -> ReportingPeriod | None:
    """Return the period an FCA release zip filename names, or None."""


DOWNLOAD_PAGE_URL: str
"""str: FCA's call report download page."""


@dataclass(frozen=True, kw_only=True)
class FCAReleaseCatalog:
    """The FCA release zips available for download, keyed by period."""

    urls: Mapping[ReportingPeriod, str]
    unmatched: tuple[str, ...] = ()

    @classmethod
    def from_snapshot(cls) -> Self:
        """Return the catalog of quarters known when this package was released."""

    @classmethod
    def from_html(cls, html: str, *, base_url: str = DOWNLOAD_PAGE_URL) -> Self:
        """Return a catalog built from the HTML of FCA's download page."""

    @classmethod
    def from_download_page(
        cls,
        *,
        url: str = DOWNLOAD_PAGE_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> Self:
        """Download FCA's download page and return the catalog it lists."""

    @property
    def periods(self) -> tuple[ReportingPeriod, ...]:
        """Return every period in the catalog, oldest first."""

    @property
    def latest(self) -> ReportingPeriod:
        """Return the newest period in the catalog."""

    def url_for(self, *, period: ReportingPeriod) -> str:
        """Return the URL of a period's release zip."""
```

`url_for` raises `PeriodNotAvailableError` for a period the catalog does not
hold.

New public module `call_report.fca.download`, with `FCAReleaseDownloader`
also exported from `call_report.fca`:

```python
class FCAReleaseDownloader:
    """Download FCA call report releases and extract them into a directory."""

    def __init__(
        self,
        *,
        data_dir: str | Path,
        overwrite: bool = False,
        catalog: FCAReleaseCatalog | None = None,
        max_workers: int = 4,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None: ...

    def download(self, *, start: str | date, end: str | date) -> Self:
        """Download every quarter from start to end into data_dir."""

    def retry_failed(self) -> Self:
        """Download again every quarter the last download recorded in errors_."""

    def download_period(self, *, period: ReportingPeriod) -> Path:
        """Download one quarter, unless it is already on disk, and return its folder."""

    # Set by download and retry_failed:
    periods_: PeriodRange
    downloaded_: tuple[ReportingPeriod, ...]
    skipped_: tuple[ReportingPeriod, ...]
    errors_: list[FCAIssue]
```

`__init__` only stores its parameters, following the scikit-learn convention
`FCACallReport` uses. `download_period` raises `DownloadError` on failure
rather than recording it, because it is the call `DownloadTransport` makes and
`FCACallReport.fetch` records the error itself. `retry_failed` with nothing to
retry returns at once.

New in `call_report.fca.transport`:

```python
@dataclass(kw_only=True)
class DownloadTransport:
    """A transport that downloads each period's release on first use."""

    cache_dir: Path
    overwrite: bool = False
    catalog: FCAReleaseCatalog | None = None
    timeout: float = 60.0
    max_retries: int = 3

    def resolve(self, *, period: ReportingPeriod) -> Path: ...
    def available_periods(self) -> tuple[ReportingPeriod, ...]: ...
```

Changed:

- `FCATransport` gains `available_periods(self) -> tuple[ReportingPeriod, ...]`,
  "Return every period this transport can supply, oldest first."
- `LocalDirectoryTransport` and `PackagedArchiveTransport` gain
  `available_periods()`.
- `FCACallReport.fetch` validates the range against the transport.
- `FCACallReport.available_periods()` returns the transport's periods.

The `overwrite` parameter has one description, copied verbatim to the
downloader and the transport: "If False, a quarter whose folder already
exists is skipped. If True, every quarter is downloaded again and replaces the
folder on disk."

## Data

No shipped data changes.

A trimmed copy of FCA's download page is checked in as a test fixture at
`tests/fca/data/download_page.html`. It is saved from the live page once the
probe runs, cut down to the markup around the zip links, and includes one
link that does not parse and one relative link.

## Pull requests

Two pull requests, both `Part of #119`, landing in this order.

1. **`[ENH] Add shared HTTP and archive helpers and transport periods (#119)`.**
   `core/_http.py`, `core/_archive.py`, `fca_release_stem`,
   `parse_fca_release_filename`, `FCAReleaseCatalog`, `available_periods()`
   on the protocol and the two local transports, and the `FCACallReport`
   validation change. `PackagedArchiveTransport` moves to `extract_zip`.
2. **`[ENH] Add FCAReleaseDownloader and DownloadTransport (#119)`.**
   The downloader, the transport, the user guide page, and the doc,
   changelog, and API reference updates. This one closes #119 and sets this
   plan's status line.

The first can merge on its own because it changes no behavior a user relies
on except the range check, which it covers with tests. Both carry the
`run-exhaustive` label, because both change how archive releases are
resolved.

## Tests

No test in the default suite reaches the internet. HTTP tests use a
`http.server.ThreadingHTTPServer` on `127.0.0.1`, started by a fixture in
`tests/conftest.py` and stopped after the test. It serves files from a
temporary folder and can be told to answer a path with a given sequence of
status codes. This runs the real `urllib` code end to end. Its helpers live in
`tests/http_server.py`, not in `conftest.py`.

`tests/core/test_http.py`:

- A 200 response returns the body, and `download_file` writes it.
- A 503 then a 200 succeeds after one retry. A 429 with `Retry-After: 2`
  sleeps 2 seconds (the sleep is replaced, so the test does not wait).
- Retries stop at `max_retries`, and the last error is raised as
  `DownloadError`.
- A 404 raises at once, with no retry.
- A connection refused and a timeout are retried.
- An HTML challenge page, and a `cf-mitigated: challenge` header, raise a
  `DownloadError` naming Cloudflare.
- A `file://` or `ftp://` URL raises before any request is made.
- A failed `download_file` leaves no file behind.

`tests/core/test_archive.py`:

- A valid zip extracts to `target_dir`.
- A file that is not a zip, and a zip with a corrupt member, raise
  `DownloadError`.
- A member named `../evil.txt` or `/etc/evil.txt` raises, and nothing is
  written.
- A failed extraction leaves no `target_dir`.

`tests/fca/test_catalog.py` (added to):

- `fca_release_stem` for both eras, and for a quarter after the snapshot.
- `parse_fca_release_filename` for both eras, mixed case, `Sep`, and names
  that do not parse. A `hypothesis` round trip:
  `parse(stem(p) + ".zip") == p` for every period.
- `from_snapshot` matches `construct_fca_download_url` for every period.
- `from_html` on the saved page: every snapshot period is present, relative
  links are resolved, an unparsable zip link lands in `unmatched`, a page
  listing a newer quarter adds it, and a scraped URL replaces the predicted
  one.
- `from_html` on a page with no zip links raises `DownloadError`.
- `from_download_page` against the local server.
- `url_for` raises `PeriodNotAvailableError` outside the catalog. `periods`
  and `latest` are correct.

`tests/fca/test_download.py`:

- `download` over a range fetches every quarter from the local server and
  extracts it to `data_dir/<stem>/`.
- With `overwrite=False`, a quarter already on disk is in `skipped_` and is
  not requested from the server.
- With `overwrite=True`, it is downloaded again and its contents replaced.
- With `overwrite=True` and a failing download, the old folder is untouched.
- One quarter returning 404 lands in `errors_`, and the others download.
- Every quarter failing raises `DownloadError`, and `errors_` is set.
- `retry_failed` downloads only the failed quarters, and empties `errors_`
  once the server answers. With nothing to retry, it makes no request.
- A zip with no `D_*.TXT` file is recorded as an error.
- A range outside the catalog raises `PeriodNotAvailableError` before any
  request.
- `max_workers=1` and `max_workers=4` give the same result.
- `data_dir` with `~` is expanded, and a missing `data_dir` is created.

`tests/fca/test_transport.py` (added to):

- `available_periods()` on `LocalDirectoryTransport` with folders for some
  quarters, with a custom `dirname_for`, and with an empty folder.
- `available_periods()` on `PackagedArchiveTransport` against the real
  archive returns 2000Q1 through the newest zip.
- `DownloadTransport.resolve` downloads on first use and reads the folder
  after that. With `overwrite=True`, a second `resolve` of the same period
  does not request it again. With `overwrite=False`, a folder already on disk
  is not requested at all.
- `DownloadTransport.available_periods()` returns its catalog's periods.

`tests/fca/test_report.py` (added to):

- `fetch` raises `PeriodNotAvailableError` for an `end` after the transport's
  latest period, and accepts an `end` after `LATEST_KNOWN_PERIOD` when the
  transport has that quarter.
- `fetch` with a transport that has no periods raises `DownloadError`.
- `available_periods()` follows the transport.
- `get_schema` for a quarter newer than the shipped metadata raises
  `ScheduleNotFoundError` with the message above.

`tests/fca/test_download_regression.py`:

- The local server serves real zips from `data/fca-call-report/`. For a
  legacy quarter (2003Q1), both sides of the era boundary (2014Q4 and
  2015Q1), and the newest quarter, `FCACallReport` with a `DownloadTransport`
  gives the same `load_all()`, `to_long_format()`, and `institutions()` as
  with `PackagedArchiveTransport`. Four quarters keep this fast enough to run
  without the `slow` marker.

`tests/fca/test_live_site.py`:

- Marked `network`, a new marker registered in `pyproject.toml`, and skipped
  unless `--run-network` is passed, the same way `--run-exhaustive` works.
- Scrapes the real download page and checks every snapshot period is listed.
- Downloads one legacy and one modern quarter and compares them with the
  checked-in zips.
- Not run by CI on pull requests. A maintainer runs it by hand, or a weekly
  job runs it (see "Open questions").

Branch coverage stays at 100%, including both values of `overwrite` on the
downloader and on the transport.

## Open questions

1. **Cloudflare outcome.** PENDING the probe. If downloads are blocked, which
   mirror? A GitHub release of this repository holding the zips is free,
   versioned, and needs no new account. An Azure Blob container needs an
   account and credentials for uploads. The plan leans toward GitHub release
   assets, built as `FCAReleaseCatalog.from_mirror(...)`.
2. **Protocol change.** This plan adds `available_periods()` to
   `FCATransport`, which breaks a user's own transport. The alternative is for
   `fetch` to fall back to the snapshot when a transport has no such method.
   That keeps old transports working at the cost of a `getattr` check.
3. **Live test schedule.** Should `weekly-maintenance.yml` run the `network`
   tests, so the team learns when FCA changes its page or blocks downloads?
   It would also flag a new quarter, as a reminder to bump the snapshot.
4. **Checksums.** FCA publishes none. Should the snapshot ship a SHA-256 for
   each known zip, generated from `data/fca-call-report/`, so the downloader
   can tell a corrupted or changed file from a good one? FCA sometimes
   republishes a quarter, so a mismatch could be a warning rather than an
   error.
5. **Default worker count.** 4 is proposed. A lower number is kinder to
   FCA's site, and a higher one is faster for a first full download of about
   105 quarters.
