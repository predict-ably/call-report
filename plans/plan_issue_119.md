# Plan: issue #119, Download FCA call report releases with a downloader and a transport

Status: proposed

## Goal

Give an installed user a way to get FCA release files from the package itself.
This is goal 5 of the v0.2 release, and the last goal before 0.2.0 is cut
(#64). It is tracked in #54.

In scope:

1. A mirror of every FCA release zip, published as assets of GitHub releases
   of this repository, with a manifest of checksums. A rolling release always
   holds the current files. A dated snapshot release is cut whenever the data
   changes, so a user can download exactly the files an earlier analysis used.
   A workflow keeps the mirror in step with `data/fca-call-report/`, and a
   nightly check reports when FCA has published or changed a file.
2. Two shared, standard library helpers in `call_report.core`. The HTTP helper
   retries transient failures and warns about plain `http` URLs. It can use
   `curl_cffi` instead of `urllib` through an optional extra. The zip
   extraction helper does not import the HTTP helper, so each can be used and
   tested alone.
3. `FCAReleaseCatalog`, which maps each quarter to its zip URL. It is built
   from the mirror's manifest or from FCA's download page.
4. `FCAReleaseDownloader`, which downloads a range of quarters into a download
   folder, verifies each zip, and extracts it to
   `<download folder>/fca/<zip stem>/`. It downloads from the GitHub mirror by
   default, and from the FCA website when asked. A quarter already downloaded is
   not downloaded again unless the user asks. Failures are recorded on the
   downloader and can be retried.
5. `DownloadTransport`, a thin `FCATransport` that asks a downloader for each
   period and then reads it with `LocalDirectoryTransport`.
6. An `available_periods()` method on every transport. `FCACallReport`
   validates both ends of a requested range against the transport.
   `LATEST_KNOWN_PERIOD` is removed from the package's own code, so adding a
   quarter never needs a constant changed.
7. A downloadable copy of the shipped schedule metadata and institution
   registry, published with the mirror, so a user can get a new quarter's
   metadata without upgrading the package.
8. Tests that never touch the internet in the default suite, and opt-in tests
   of the live mirror and the live FCA site. A nightly workflow runs both.
9. A user guide page on downloading, and updates to the getting started page,
   the API reference, and the changelog. The page explains where downloaded
   files go, how to choose another folder, how to read them offline, how to
   pin a snapshot, and how to delete them.

Out of scope:

- Merger adjustment (version 0.3).
- Opening a data pull request automatically when FCA changes a file. The
  nightly check opens an issue, and a maintainer refreshes the data.
- Generating schedule metadata or the institution registry from a user's own
  folder of releases. The generation scripts stay in `scripts/`. The nightly
  drift check keeps the published metadata bundle current, so a user gets a
  new quarter's metadata by downloading the bundle.
- Downloading in parallel from inside `FCACallReport.fetch`. A user who wants
  parallel downloads calls the downloader first.
- FFIEC, FDIC, and NCUA, though the shared helpers are written for them too.

## Why

### The gap

Two transports ship today. `LocalDirectoryTransport` reads releases the user
has already downloaded and extracted. `PackagedArchiveTransport` reads the zips
in `data/fca-call-report/`, which are not in the wheel. So an installed user
has to download and unzip every quarter by hand. `construct_fca_download_url`
already builds each URL and `DownloadError` already exists, but nothing
fetches a file.

### A mirror we control, and FCA as an option

FCA's site sits behind Cloudflare, which may block automated downloads, and a
government site can change its URLs or page layout without notice. This
repository already holds every release in `data/fca-call-report/`, 12 MB in
all. Publishing those zips as assets of a GitHub release gives every user a
source that:

- does not depend on Cloudflare's view of the client,
- has stable, predictable URLs,
- carries a SHA-256 for every file, so a download is verified, not trusted,
- costs nothing and needs no new account or credentials,
- is updated by a workflow when a quarter's zip is added to the repo.

So the mirror is the default source. The FCA website stays available as
`source="fca"`, for a user who wants the file straight from the publisher, or
a quarter FCA has published before the mirror is updated.

The mirror lives in a rolling release with the tag `fca-call-report-data`,
plus an immutable snapshot release for each date the data changed (see
"Versioning the mirror"). None of these tags match `publish.yml`'s
`v[0-9]+.[0-9]+.[0-9]+` pattern, so none triggers a PyPI publish. Each is
created with `--latest=false` so the package's own release stays the one GitHub
shows as latest. A new quarter adds one asset to the rolling release. FCA can
also replace earlier quarters, so a refresh can change older assets too.

Alternatives considered for the mirror:

- **One release per quarter** (`fca-data-2026Q1`). A download URL then needs
  a lookup of which tag holds which quarter, and the release list fills with
  data tags.
- **A snapshot release per package version.** A quarter added between package
  releases is not mirrored until the next one, and the mirror's history would
  follow the package rather than the data.
- **Azure Blob Storage.** Reliable, but it needs an account, credentials for
  uploads, and someone to pay for it.
- **Zips in the wheel.** Offline, but the wheel grows every quarter, and a
  user who wants one quarter installs all of them.

### Cloudflare findings

The probe ran on 2026-10-12 with Python 3.13's `urllib.request`, against two
modern era zips (`2026March.zip`, `2026June.zip`), two legacy era zips
(`Sept2014.zip`, `Mar2000.zip`), and the download page.

| Request | Result |
|---|---|
| Default `urllib` (`User-Agent: Python-urllib/3.13`) | 403, `text/plain`, body `error code: 1010`, for every URL |
| No `User-Agent` header at all | 403, `error code: 1010` |
| Default `User-Agent` with a browser `Accept` header | 403, an HTML error page |
| A browser `User-Agent` | 200, the zip (`application/zip`) or the page |
| `call-report/0.2 (+https://github.com/predict-ably/call-report)` | 200 |
| `curl/8.5.0` or `python-requests/2.32.3` | 200 |

Cloudflare error 1010 means the request was refused because of its client
signature. Here the signature is only the `User-Agent` header. Cloudflare
refuses Python's default value and an empty one, and accepts any other value
tried. There is no JavaScript challenge and no `cf-mitigated` header. Each
request took under a second.

So `source="fca"` works with the standard library, as long as the HTTP
helper sends its own `User-Agent`. It sends
`call-report/<version> (+https://github.com/predict-ably/call-report)`, which
names the package honestly rather than posing as a browser. A 403 whose body
is `error code: 1010`, or any HTML response where a zip is expected, still
raises a `DownloadError` that names Cloudflare and suggests
`source="github"`, in case Cloudflare's rules change. It is not retried.

Cloudflare could one day refuse by TLS fingerprint instead, which `urllib`
cannot get past. For that case the HTTP helper can use `curl_cffi`, which copies
a browser's TLS fingerprint. It is an optional extra and is used only when the
caller passes `http_client="curl_cffi"`. See "Standard library first, with one
optional extra".

### FCA republishes past quarters

The probe also downloaded all 106 zips FCA lists and compared them with
`data/fca-call-report/`.

- 93 are byte for byte the same.
- 2026June.zip (2026Q2) is new.
- 12 quarters, 2023Q2 through 2026Q1, were regenerated on 2026-08-31. Their
  data filenames carry `_G20260831` in place of the original generation date.
  The layout (`D_*.TXT`) files did not change. The data changed in a handful
  of rows per quarter. Some values were amended, some zeros became empty
  fields, and the INST roster now shows six institutions' current addresses
  in every regenerated quarter, rather than the address they reported at the
  time.

The FCA call report disclosures page also describes FCA removing and
reposting corrected releases (see "FCA call report disclosures" below). So a
quarter's release is not fixed once published. This changes the design in
four places.

1. **A download can fall behind.** The downloader writes a small
   `.call_report_release.json` into each folder it extracts. It records the
   source URL, the zip's SHA-256, the file's generation date, the mirror
   snapshot it came from, and the download time. With `source="github"`, a
   downloaded quarter whose recorded checksum differs from the current
   manifest is listed in `outdated_`. It is replaced only when the user passes
   `overwrite="outdated"` or `overwrite="all"`. A hand-extracted folder has no
   record, so it is never reported as outdated.
2. **The mirror records which generation it holds.** Each manifest entry
   carries `generated`, the `_G<YYYYMMDD>` date from the modern era data
   filenames, or null for the legacy era, which has none.
3. **The mirror keeps every version.** Each change to the data is published
   as a dated snapshot release, so a superseded file stays downloadable. See
   "Versioning the mirror".
4. **Drift is checked every night.** A scheduled workflow compares FCA's
   current files with the mirror's manifest, so a maintainer hears of a change
   within a day. See "The nightly drift check".

Bringing the 13 changed or new zips into `data/fca-call-report/`, and
regenerating the shipped metadata and registry, is a separate data pull
request. It is not part of #119. The two generation scripts assume an
archived quarter never changes, and resume from their last quarter by
default. That assumption is now false, so changing their default is a
follow-up issue.

### Two layers over one implementation

The issue asks for a downloader and a transport, with only the downloader
fetching files. A downloader alone would make every user manage two objects
for one task. A transport alone would leave no clean way to download ahead of
time, fill a download folder in CI or on an air-gapped machine, or recover from a failed
download. With both, the downloader does the work and `DownloadTransport` only
calls it.

Both write the folder layout that `LocalDirectoryTransport` reads
(`<download folder>/fca/<zip stem>/`). So downloaded and hand-extracted
releases are read by one code path, and only that path needs regression
testing against `PackagedArchiveTransport`.

### A default download folder

Downloaded files are data the user keeps and returns to. They belong in a
permanent location, not in a cache folder that the operating system or a
cleanup tool may empty. Reading them later with no network access is a main
use. So `download_dir` is optional. When it is omitted, the downloader uses a
documented default.

1. The `CALL_REPORT_DOWNLOAD_DIR` environment variable, if set.
2. Otherwise the platform's user data folder: `%LOCALAPPDATA%\call-report`
   on Windows, `~/Library/Application Support/call-report` on macOS, and
   `$XDG_DATA_HOME/call-report` (or `~/.local/share/call-report`) elsewhere.

FCA files go in an `fca/` subfolder of it. The folder is a plain folder of
extracted releases, so `LocalDirectoryTransport(data_dir=<same folder>)` reads
it later with no network access, as the issue asks. A small stdlib function
does this. `platformdirs` would do the same, but it is a dependency for about
fifteen lines.

The user guide gives the default location for each platform, the environment
variable, how to pass a different folder, how to read the files with
`LocalDirectoryTransport`, and how to delete them.

### Standard library first, with one optional extra

| Client | New dependencies | Concurrency | Retries |
|---|---|---|---|
| `urllib.request` | none | `concurrent.futures` threads | written here |
| `requests` | urllib3, certifi, idna, charset-normalizer | threads | through urllib3 |
| `httpx` | httpcore, h11, anyio, certifi, idna | threads or async | connection errors only |
| `curl_cffi` | one binary wheel | threads or async | written here |

Downloads are about a hundred files of roughly 100 KB each. Connection pooling
and HTTP/2 do not matter at that size. If Cloudflare blocks by TLS
fingerprint, `urllib`, `requests`, and `httpx` all fail the same way, so a
third party pure Python client would add dependencies without changing the
outcome. Only `curl_cffi`, which copies a browser's TLS fingerprint, would.

So `urllib` is the default, and `curl_cffi` is an optional extra,
`call-report[curl_cffi]`. The extra is named after the library, as the
`pandas`, `polars`, and `pyarrow` extras are. pip normalizes extra names, so
`call-report[curl-cffi]` installs the same thing. Its floor is the oldest
`curl_cffi` release with wheels for every Python version the package supports,
confirmed in the first pull request. The HTTP helper loads it with
`import_optional`, as the dependency rules require, and uses it only when the
caller passes
`http_client="curl_cffi"`. Choosing by an argument rather than by what happens
to be installed keeps behavior the same on every machine. Without the extra,
that argument raises an error that gives the `pip install` line. The mirror is
the default source, so most users never need the extra.

Python releases the GIL during network reads, so a thread pool downloads
several files at once without async code. The pool is small by default (4
workers).

### The catalog says which quarters a source holds

The package no longer carries a hardcoded latest quarter. `LATEST_KNOWN_PERIOD`
is removed from `src/`. Tests and maintainer scripts find the newest quarter
from the newest zip in `data/fca-call-report/`, so adding a quarter never needs
a constant changed.

What a source holds is read from the source itself.

- **The mirror's manifest.** `manifest.json` in the mirror release lists every
  quarter it holds, with checksums. Reading it is one small request.
- **FCA's download page.** It can be scraped with `html.parser.HTMLParser` and
  a regular expression, so this needs no dependency either.

Each is a way to build an `FCAReleaseCatalog`, rather than a flag on the
downloader, because:

- one lookup serves the downloader, the transport, and the report,
- parsing (`from_manifest_json`, `from_html`) is pure, so tests feed it saved
  input,
- the user can look at `catalog.latest` before downloading anything,
- a failed lookup raises, instead of a flag quietly falling back to a guess,
- a maintainer can compare a catalog with the mirror to see when a new quarter
  has appeared.

When the user passes no catalog, the downloader builds one from its own source
on first use. That is `from_github` for `source="github"` and `from_fca` for
`source="fca"`, one request beyond the zips. A user who has already downloaded
everything reads it with `LocalDirectoryTransport`, which needs no catalog and
no network.

### Each transport says which periods it can supply

`FCACallReport.fetch` rejects a range that ends after `LATEST_KNOWN_PERIOD`,
and `FCACallReport.available_periods()` returns that fixed range. That is wrong
once quarters can come from anywhere other than a fixed list. A user who drops
a new zip into `data/fca-call-report/`, or extracts one into their own folder,
cannot read it until a package release changes the constant.

So every transport gains `available_periods()`, and the report asks the
transport. The tuple is sorted, so its first period is the transport's
earliest and its last is its latest. `fetch` checks both ends of the requested
range against them, in place of the fixed `EARLIEST_PERIOD` and
`LATEST_KNOWN_PERIOD`.

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
this plan makes the change cleanly. The two transports in the package are
updated to match, the report calls the method directly with no fallback, and
the changelog records the break.

### Failures are recorded, and can be retried

A failed quarter in a range does not stop the others. It is recorded on the
downloader, the same way `FCACallReport.fetch` records an unresolvable period
in `errors_`. Nothing in `src/` retries today. This plan adds two layers.

- Automatic retries inside the HTTP helper, for transient failures only.
- A `retry_failed()` method that downloads again only the quarters in
  `errors_`.

## Approach

### The mirror

**Releases.** Two kinds of GitHub release hold the mirror.

- A rolling release, tag `fca-call-report-data`, titled "FCA call report data".
  It always holds the current files.
- An immutable snapshot release for each date the data changed, tag
  `fca-data-<YYYY-MM-DD>`, titled "FCA call report data, <YYYY-MM-DD>". It
  holds the complete set of files as they stood on that date and is never
  edited. A second change on the same date gets the tag
  `fca-data-<YYYY-MM-DD>-2`, and so on.

Each release holds:

- one zip per quarter, named exactly as FCA names it (`2026March.zip`,
  `Sept2014.zip`), byte for byte the file in `data/fca-call-report/`,
- `manifest.json`,
- `metadata.zip`, the shipped schedule metadata and institution registry (see
  "Metadata outside the wheel").

So a quarter's mirror URL is
`https://github.com/predict-ably/call-report/releases/download/<tag>/<stem>.zip`,
built from the tag and the stem with no lookup. The rolling tag is the default.

**Manifest.** `manifest.json` looks like this.

```json
{
  "schema_version": 1,
  "snapshot": "2026-10-12",
  "snapshots": ["2026-10-12"],
  "releases": [
    {
      "period": "2026-03-31",
      "file": "2026March.zip",
      "sha256": "…",
      "size": 135858,
      "generated": "2026-08-31",
      "source_url": "https://www.fca.gov/template-fca/bank/2026March.zip"
    }
  ],
  "metadata": {
    "file": "metadata.zip",
    "sha256": "…",
    "size": 52114,
    "through": "2026-06-30",
    "schema_version": 1
  }
}
```

`snapshot` names the snapshot release the files came from. `snapshots` lists
every snapshot date, oldest first, so a user can see what can be pinned.

**Script.** `scripts/build_fca_mirror_manifest.py` reads `data/fca-call-report/`
and the shipped metadata. It writes `manifest.json` and `metadata.zip`. It uses
`parse_fca_release_filename` and `construct_fca_download_url` from the package,
so the mirror and the package agree on names. It reads `generated` from the
`_G<YYYYMMDD>` part of a modern era zip's data filenames. It fails if a zip's
name does not parse, if a zip has no `D_*.TXT` file, or if one zip's data files
carry more than one generation date.

**Workflow.** `.github/workflows/fca-mirror.yml` runs on a push to `main` that
changes `data/fca-call-report/**` or `src/call_report/fca/data/**`, and by hand
(`workflow_dispatch`). It:

1. builds `manifest.json` and `metadata.zip` with the script,
2. compares them with the rolling release's current manifest and stops if no
   zip and no metadata differ,
3. creates the snapshot release `fca-data-<today>` with `--latest=false`,
   holding every zip, the manifest, and `metadata.zip`,
4. brings the rolling release in line: it creates the release if it does not
   exist, uploads every zip whose name is not already an asset, and replaces
   (`gh release upload --clobber`) any whose SHA-256 differs,
5. uploads `manifest.json` and `metadata.zip` last, so the manifest never lists
   a file that is not there yet.

It needs `contents: write` and uses the workflow's own `GITHUB_TOKEN`, as
`publish.yml` does. The first run, by hand after the implementation merges,
uploads all 106 quarters.

### Versioning the mirror

An analysis should be repeatable. A user who ran it in March must be able to
find out which files it read, and download those same files in October.

- **Snapshots are the versions.** Each snapshot release is the whole mirror on
  one date. It is immutable, so `fca-data-2026-10-12` always holds the same
  bytes. The data changes a few times a year, and one snapshot is about 12 MB.
- **A user pins a version.** `snapshot="2026-10-12"` on the catalog, the
  downloader, or the transport downloads from that snapshot. The default,
  `snapshot="latest"`, uses the rolling release. Pinning applies only to
  `source="github"`, because FCA does not keep earlier versions. Passing a
  snapshot with `source="fca"` raises `ValueError`.
- **A download remembers where it came from.** The record in each folder holds
  the snapshot, the checksum, and the generation date. The manifest's
  `snapshot` field says which snapshot "latest" meant at the time. So a folder
  downloaded from `latest` records a concrete date.
- **A user can report it.** `FCAReleaseDownloader.release_record(period=...)`
  returns the record for a downloaded quarter. The user guide shows how to log
  the records with an analysis, and how to pin the same snapshot to repeat it.
- **History is listed.** The manifest's `snapshots` list and the release list
  show every version. `FCAReleaseCatalog.from_github(snapshot=...)` reads any
  one's manifest.

### Metadata outside the wheel

The schedule metadata and institution registry in the wheel stop at the
quarter the package was released with. A user who downloads a newer quarter
should not have to wait for a package release to use its schema or its
institution history.

`metadata.zip` holds the same files the wheel holds, in the same layout:
`schedules/<root>.json` and `institutions/registry.json`, plus a
`metadata.json` giving the newest quarter covered (`through`) and a
`schema_version`. The script builds it from `src/call_report/fca/data/`, so
the wheel and the bundle always agree for the quarters they share.

- `FCAReleaseDownloader.download_metadata()` downloads the bundle for its
  snapshot, checks it against the manifest, and extracts it to
  `<download folder>/fca-metadata/`.
- The metadata loaders take an optional `metadata_dir`. Given one, they read
  from it instead of the wheel. `FCACallReport` takes the same argument and
  passes it on, so `get_schema` and the institution registry can cover a quarter
  newer than the installed package.
- A bundle whose `schema_version` is newer than the installed package can read
  is refused with an error that says to upgrade `call-report`. The version
  changes only when the file format changes.
- The wheel's copy stays the default, so the package still works offline.

### The nightly drift check

`scripts/check_fca_drift.py` reads FCA's download page, downloads every zip it
lists, and compares each SHA-256 with the rolling manifest. It prints the
quarters FCA has published that the mirror lacks, the quarters whose bytes
differ, and the quarters the mirror holds that FCA no longer lists. It exits
nonzero if there is any difference. It sends the same `User-Agent` as the
downloader.

`.github/workflows/fca-drift.yml` runs it every night and by hand. It then runs
the opt-in live tests. If either fails, it opens an issue labeled `fca-drift`
with the report, or adds a comment to the issue that is already open. It needs
`contents: read` and `issues: write`. A maintainer then adds the zips to
`data/fca-call-report/` and runs both generation scripts, as for any new
quarter. That data pull request merges, the mirror workflow publishes a new
snapshot, and the drift check passes again.

If Cloudflare refuses the workflow's addresses, the run fails with the
Cloudflare message and the issue says so. That is also a signal worth having.

### The default source and the manifest

With `source="github"` (the default), the downloader reads `manifest.json`
once per downloader instance, and uses it for three things.

- **Verification.** Each zip's SHA-256 must match the manifest, or the quarter
  is recorded as an error and nothing is extracted.
- **Freshness.** A downloaded quarter whose recorded checksum differs from the
  manifest is listed in `outdated_`.
- **Range.** If the user passed no catalog, the manifest is the catalog. So a
  quarter added to the mirror after a package release is downloadable with no
  other step. This is the one request the default makes beyond the zips.

If the manifest cannot be read, after the same retries as any other request,
`download` raises `DownloadError`. A `source="github"` download is therefore
always verified. A user who is offline reads files already downloaded with
`LocalDirectoryTransport`, which needs no manifest.

With `source="fca"`, there is no manifest. The catalog comes from FCA's
download page, and each zip is checked for structure only.

### New shared module: `src/call_report/core/_http.py`

Standard library only, with `curl_cffi` as an optional alternative.

```python
HttpClient = Literal["urllib", "curl_cffi"]


def fetch_bytes(
    url: str,
    *,
    timeout: float = 60.0,
    max_retries: int = 3,
    headers: Mapping[str, str] | None = None,
    http_client: HttpClient = "urllib",
) -> bytes: ...


def download_file(
    url: str,
    *,
    destination: Path,
    timeout: float = 60.0,
    max_retries: int = 3,
    headers: Mapping[str, str] | None = None,
    http_client: HttpClient = "urllib",
) -> Path: ...
```

- Every request sends
  `User-Agent: call-report/<version> (+https://github.com/predict-ably/call-report)`.
  Without it, FCA's Cloudflare refuses every request (see "Cloudflare
  findings").
- Only `http` and `https` URLs are accepted, checked before `urlopen` is
  called. That check is what makes the narrow `# noqa: S310` on the `urlopen`
  line correct.
- A plain `http` URL whose host is not a loopback address (`localhost`,
  `127.0.0.1`, `::1`) issues an `InsecureConnectionWarning`, a new
  `UserWarning` subclass in `call_report.exceptions`. The request still goes
  ahead. Loopback hosts are exempt so that local servers, including the test
  server, do not warn. A redirect to a plain `http` URL gets the same warning.
- Redirects are followed. GitHub serves release assets through a redirect to
  its download host.
- `download_file` streams to a temporary file in the destination's folder and
  renames it into place, so a failed download never leaves a partial file.
- Retries cover connection errors, timeouts, HTTP 429, and HTTP 5xx, with
  exponential backoff starting at 1 second. A `Retry-After` header on a 429 or
  503 is honored. A 404 or other 4xx is not retried.
- A response that looks like a Cloudflare challenge raises `DownloadError`
  that names Cloudflare, and is not retried.
- With `http_client="curl_cffi"`, requests go through `curl_cffi.requests`
  with `impersonate="chrome"`. The library is loaded with `import_optional`,
  so a missing one raises an error with the `pip install "call-report[curl_cffi]"`
  line. Status handling, retries, and the Cloudflare check are the same code
  for both clients.
- The sleep function is a module attribute, so tests replace it and never
  wait.
- Every failure is raised as `DownloadError`, with the URL and the reason.

### New shared module: `src/call_report/core/_archive.py`

```python
def extract_zip(*, archive: Path, target_dir: Path) -> Path: ...
def sha256_of(path: Path) -> str: ...
```

This module does not import `_http`. Unpacking a zip is not a download, so its
failures have their own error, `ZipArchiveUnpackingError`, a new subclass of
`CallReportError` in `call_report.exceptions`.

- Checks the file is a zip (`zipfile.is_zipfile`) and that `testzip()` finds
  no corrupt member, and raises `ZipArchiveUnpackingError` otherwise.
- Refuses a member whose path is absolute or climbs out of the target with
  `..`, with the same error.
- Extracts into a temporary sibling folder and renames it to `target_dir`, so
  a folder that exists is always a complete extraction.
- `PackagedArchiveTransport.resolve` switches to `extract_zip`. A corrupt
  packaged zip now raises `ZipArchiveUnpackingError`, and `FCACallReport.fetch`
  records it in `errors_` as it does any other failure to resolve a period.
- The downloader catches both `DownloadError` and `ZipArchiveUnpackingError`.
  It records the second with a message that says the file downloaded but could
  not be unpacked.

### New shared module: `src/call_report/core/_dirs.py`

```python
def default_download_dir() -> Path: ...
```

Returns the folder described under "A default download folder". It does not
create it. The downloader creates `<download folder>/fca/` on first use.

### Changes in `src/call_report/fca/catalog.py`

- `LATEST_KNOWN_PERIOD` is removed. The upper bound on a period is the quarter
  containing today's date, and the lower bound stays `EARLIEST_PERIOD`.
- `fca_release_stem(*, period)` returns a period's zip stem (`"2026March"`,
  `"Sept2014"`) with no upper bound check. `_default_dirname` in `transport.py`
  calls it, so a local transport can name a folder for any quarter.
- `construct_fca_download_url` keeps its output. Its bounds check uses the
  rule above.
- `construct_fca_mirror_url(*, period, snapshot="latest")` is the same rule
  for the mirror.
- `parse_fca_release_filename(name)` turns a zip filename into a period, or
  returns `None`. It accepts both eras, any letter case, and `Sep` as well as
  `Sept`.
- `FCAReleaseCatalog` is added (see "Public API").
- The scrape uses a private `HTMLParser` subclass that collects every `href`
  ending in `.zip`, resolved with `urllib.parse.urljoin`. Links whose
  filenames parse become catalog entries. Links that do not parse are kept on
  `unmatched`.
- A page with no parsable zip links raises `DownloadError` saying the page
  layout may have changed. An empty catalog is never returned.
- The page as of 2026-10-12 fits this. It lists 106 zip links, one per
  quarter from 2000Q1 to 2026Q2. Every `href` is relative
  (`/template-fca/bank/2026June.zip`,
  `/template-fca/download/CallReportData/Mar2000.zip`), every filename
  matches one of the two era patterns, and each link's text names its
  quarter ("June 2026"). The page also gives a file count per quarter
  ("(72 files)") and a "Page updated" date. The scraper does not depend on
  either.

### New public module: `src/call_report/fca/download.py`

Holds `FCAReleaseDownloader` and `FCAReleaseRecord`. In outline:

```python
def download(self, *, start, end) -> Self:
    periods = PeriodRange(start=start, end=end)
    catalog = self._resolve_catalog()  # the catalog passed in, or one from the source
    for period in (periods[0], periods[-1]):
        catalog.url_for(period=period)  # raises PeriodNotAvailableError

    outdated = self._find_outdated(periods, catalog)  # needs checksums
    to_fetch, skipped = [], []
    for period in periods:
        if self._wanted(period, outdated):
            to_fetch.append(period)
        else:
            skipped.append(period)

    with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
        outcomes = pool.map(self._download_one, to_fetch)  # ok, or an FCAIssue

    self.periods_, self.skipped_ = periods, tuple(skipped)
    self.downloaded_, self.errors_ = ...
    self.outdated_ = ...
    if to_fetch and not self.downloaded_:
        raise DownloadError(
            "None of the ... quarter(s) could be downloaded. See errors_."
        )
    return self


def _wanted(self, period, outdated) -> bool:
    if not self._target(period).is_dir():
        return True
    if self.overwrite == "all":
        return True
    return self.overwrite == "outdated" and period in outdated
```

- `overwrite="outdated"` needs checksums to compare. With `source="fca"`, or a
  catalog that has none, `download` raises `ValueError` saying so.
- `_download_one` downloads the zip to a temporary file in the download folder,
  checks its SHA-256 against the manifest when there is one, checks it holds at
  least one `D_*.TXT` layout file, and extracts it with `extract_zip`.
- When it replaces a folder that exists, the new release is extracted to a
  temporary folder first. Only once it is complete is the old folder moved
  aside, the new one renamed into place, and the old one removed. A failed
  download leaves the quarter already downloaded untouched.
- Each worker writes only its own quarter's folder, and results are collected
  in the calling thread, so no lock is needed.
- `errors_` uses the existing `FCAIssue` with `schedule=None`. `FCAIssue`
  moves from `report.py` to a small private module so `download.py` can import
  it without importing the report. It stays exported where it is today.
- The state is set before the "every quarter failed" error is raised, so
  `retry_failed()` still works after it.
- `download_dir` is expanded with `Path.expanduser()`. The folder is created if
  missing.
- `release_record(*, period)` reads `.call_report_release.json` from the
  quarter's folder and returns an `FCAReleaseRecord`, or `None` for a folder
  with no record.
- `download_metadata()` downloads `metadata.zip` for the downloader's snapshot,
  checks it against the manifest, and extracts it to `metadata_dir`. It raises
  `DownloadError` for `source="fca"`, which has no metadata bundle.

### Changes in `src/call_report/fca/transport.py`

- `FCATransport` gains `available_periods(self) -> tuple[ReportingPeriod, ...]`.
- `LocalDirectoryTransport.available_periods()` checks, for each quarter from
  `EARLIEST_PERIOD` through the quarter containing today, whether
  `data_dir / dirname_for(period)` is a folder. That is about 107 checks, and
  it works for any `dirname_for` because it never reverses a folder name.
- `PackagedArchiveTransport.available_periods()` does the same for
  `archive_root / f"{dirname_for(period)}.zip"`.
- `DownloadTransport` is added (see "Public API"). It builds one
  `FCAReleaseDownloader` with `max_workers=1` and the same `download_dir`,
  `source`, `snapshot`, `overwrite`, `catalog`, and `http_client`. `resolve`
  downloads the period if needed and then returns
  `LocalDirectoryTransport(data_dir=<its download folder>).resolve(period=...)`.
- `DownloadTransport` keeps a set of periods it has already resolved. With
  `overwrite="all"`, a period is downloaded again only on its first `resolve`
  on that transport instance. With `overwrite="outdated"`, the same holds for
  a period that is outdated. So one `fetch()` never downloads a quarter twice,
  and `fetch()` again on the same report reads the folder already there. A new
  transport instance downloads again.
- The module docstring loses its sentence about a future network transport.

### Changes in `src/call_report/fca/report.py`

- `fetch` checks the range against the transport. A `start` before the first
  period in `available_periods()`, or an `end` after the last, raises
  `PeriodNotAvailableError` naming the transport's range. A transport with no
  available periods raises `DownloadError` naming where it looked.
- A gap inside the range, such as a missing folder, is still recorded in
  `errors_`, as today.
- `available_periods()` returns `self.transport.available_periods()`. It
  still does not need `fetch` to have run.
- `FCACallReport` takes an optional `metadata_dir` and passes it to the
  metadata loaders. `get_schema` reads the bundle there when it is given. For a
  quarter newer than the metadata it can read, it raises a clear
  `ScheduleNotFoundError` saying the metadata does not cover that quarter yet.
  The message points to `FCAReleaseDownloader.download_metadata` and to
  `get_layout`, which reads the release itself. `load`, the reshaping
  methods, and `institutions()` read the release's own files and work
  unchanged.

### FCA call report disclosures

The [disclosures page](https://www.fca.gov/bank-oversight/call-report-disclosures)
(last updated 2019-08-13) lists four issues.

| Disclosure | Quarters | Effect on this work |
|---|---|---|
| AgFirst Farm Credit Bank revised its young, beginning, and small farmer (YBS) data, reposted 2019-08-09 | 2009 to 2017 | None. YBS data is not one of the schedules in the call report releases. |
| Lone Star ACA. FCA removed the affected call reports, then posted corrected ones | 2016Q1 to 2017Q1 | Shows that FCA replaces published releases. Handled by the outdated check and the manifest's checksums. |
| FCS Southwest ACA corrected its call reports. Earlier periods should not be relied on | 2013Q1 to 2015Q3, and earlier | None for downloading. The user guide notes it. |
| Loan classification errors. The corrected values are only in a separate spreadsheet, not in the releases | Before 2015Q4, schedules RC.1 (lines 1.a.i to 1.a.xii), RC-F1, RC-F2, and RI-E | None for downloading. The downloader fetches the releases as published. The user guide notes it and links the spreadsheet. |

None of them changes how a release parses. The user guide's downloading page
gets a short "Known issues in FCA's data" section that links the
disclosures page and lists the last three rows above. Applying the
spreadsheet's corrections is out of scope.

## Public API

New in `call_report.fca.catalog`. These names are documented and imported
from `call_report.fca.catalog` only, as the existing catalog functions are,
and are not re-exported from `call_report.fca`:

```python
FCASource = Literal["github", "fca"]

DOWNLOAD_PAGE_URL: str
"""str: FCA's call report download page."""

MIRROR_BASE_URL: str
"""str: The base URL of this project's GitHub releases."""


def fca_release_stem(*, period: ReportingPeriod) -> str:
    """Return the stem of a period's FCA release zip, such as "2026March"."""


def parse_fca_release_filename(name: str) -> ReportingPeriod | None:
    """Return the period an FCA release zip filename names, or None."""


def construct_fca_mirror_url(
    *, period: ReportingPeriod, snapshot: str = "latest"
) -> str:
    """Construct the GitHub mirror URL for a period's Call Report zip archive."""


@dataclass(frozen=True, kw_only=True)
class FCAReleaseCatalog:
    """The FCA release zips a source holds, keyed by period."""

    urls: Mapping[ReportingPeriod, str]
    sha256: Mapping[ReportingPeriod, str] = field(default_factory=dict)
    snapshot: str | None = None
    unmatched: tuple[str, ...] = ()

    @classmethod
    def from_manifest_json(cls, text: str) -> Self:
        """Return a catalog built from the GitHub mirror's manifest."""

    @classmethod
    def from_github(
        cls,
        *,
        snapshot: str = "latest",
        timeout: float = 60.0,
        max_retries: int = 3,
        http_client: HttpClient = "urllib",
    ) -> Self:
        """Download the GitHub mirror's manifest and return the catalog it lists."""

    @classmethod
    def from_html(cls, html: str, *, base_url: str = DOWNLOAD_PAGE_URL) -> Self:
        """Return a catalog built from the HTML of FCA's download page."""

    @classmethod
    def from_fca(
        cls,
        *,
        timeout: float = 60.0,
        max_retries: int = 3,
        http_client: HttpClient = "urllib",
    ) -> Self:
        """Download FCA's download page and return the catalog it lists."""

    @property
    def periods(self) -> tuple[ReportingPeriod, ...]:
        """Return every period in the catalog, oldest first."""

    @property
    def earliest(self) -> ReportingPeriod:
        """Return the oldest period in the catalog."""

    @property
    def latest(self) -> ReportingPeriod:
        """Return the newest period in the catalog."""

    def url_for(self, *, period: ReportingPeriod) -> str:
        """Return the URL of a period's release zip."""
```

`url_for` raises `PeriodNotAvailableError` for a period the catalog does not
hold. A catalog built from the mirror carries `sha256` and `snapshot`. One
built from FCA's page carries neither. `LATEST_KNOWN_PERIOD` is removed from
this module.

New in `call_report.exceptions`:

```python
class ZipArchiveUnpackingError(CallReportError):
    """A zip archive could not be unpacked.

    Raised for a file that is not a zip, a zip with a corrupt member, or a
    member whose path would land outside the target folder.
    """


class InsecureConnectionWarning(UserWarning):
    """A request was made over plain HTTP to a host that is not this machine."""
```

New in `call_report.core`:

```python
def default_download_dir() -> Path:
    """Return the folder call_report keeps downloaded files in."""
```

New public module `call_report.fca.download`. `FCAReleaseDownloader` and
`FCAReleaseRecord` are documented and imported from there only:

```python
@dataclass(frozen=True, kw_only=True)
class FCAReleaseRecord:
    """Where a downloaded release came from."""

    period: ReportingPeriod
    source: FCASource
    url: str
    sha256: str
    generated: date | None
    snapshot: str | None
    downloaded_at: datetime


class FCAReleaseDownloader:
    """Download FCA call report releases and extract them into a download folder."""

    def __init__(
        self,
        *,
        download_dir: str | Path | None = None,
        source: FCASource = "github",
        snapshot: str = "latest",
        overwrite: Literal["outdated", "all"] | None = None,
        catalog: FCAReleaseCatalog | None = None,
        max_workers: int = 4,
        timeout: float = 60.0,
        max_retries: int = 3,
        http_client: HttpClient = "urllib",
    ) -> None: ...

    def download(self, *, start: str | date, end: str | date) -> Self:
        """Download every quarter from start to end into the download folder."""

    def retry_failed(self) -> Self:
        """Download again every quarter the last download recorded in errors_."""

    def download_period(self, *, period: ReportingPeriod) -> Path:
        """Download one quarter, unless it is already there, and return its folder."""

    def download_metadata(self) -> Path:
        """Download the schedule metadata and institution registry bundle."""

    def release_record(self, *, period: ReportingPeriod) -> FCAReleaseRecord | None:
        """Return where a downloaded quarter came from, or None if unrecorded."""

    @property
    def data_dir(self) -> Path:
        """Return the folder the extracted releases are written to."""

    @property
    def metadata_dir(self) -> Path:
        """Return the folder the metadata bundle is extracted to."""

    # Set by download and retry_failed:
    periods_: PeriodRange
    downloaded_: tuple[ReportingPeriod, ...]
    skipped_: tuple[ReportingPeriod, ...]
    errors_: list[FCAIssue]
    outdated_: tuple[ReportingPeriod, ...]
```

`data_dir` is `<download_dir>/fca`, the folder to hand to
`LocalDirectoryTransport` for offline reading. `metadata_dir` is
`<download_dir>/fca-metadata`. `__init__` only stores its parameters,
following the scikit-learn convention `FCACallReport` uses. Passing both
`source` and `catalog` is allowed. The catalog decides the URLs, and `source`
is then only used in error messages. `download_period` raises `DownloadError`
on failure rather than recording it, because it is the call `DownloadTransport`
makes, and `FCACallReport.fetch` records the error itself. `retry_failed` with
nothing to retry returns at once. `snapshot` other than `"latest"` with
`source="fca"` raises `ValueError` from `download`.

New in `call_report.fca.transport`:

```python
@dataclass(kw_only=True)
class DownloadTransport:
    """A transport that downloads each period's release on first use and keeps it."""

    download_dir: Path | None = None
    source: FCASource = "github"
    snapshot: str = "latest"
    overwrite: Literal["outdated", "all"] | None = None
    catalog: FCAReleaseCatalog | None = None
    timeout: float = 60.0
    max_retries: int = 3
    http_client: HttpClient = "urllib"

    def resolve(self, *, period: ReportingPeriod) -> Path: ...
    def available_periods(self) -> tuple[ReportingPeriod, ...]: ...
```

Changed:

- `FCATransport` gains `available_periods(self) -> tuple[ReportingPeriod, ...]`,
  "Return every period this transport can supply, oldest first."
- `LocalDirectoryTransport` and `PackagedArchiveTransport` gain
  `available_periods()`.
- `FCACallReport.fetch` validates both ends of the range against the transport.
- `FCACallReport.available_periods()` returns the transport's periods.
- `FCACallReport` takes `metadata_dir: Path | None = None`.
- `get_fca_file_metadata`, `all_fca_file_metadata`,
  `get_institutions_file_metadata`, and `get_fca_institution_registry` take an
  optional keyword `metadata_dir: Path | None = None`.

Repeated parameters have one description each, copied verbatim wherever the
parameter appears.

- `download_dir`: "The folder to keep downloaded releases in. FCA releases go
  in its `fca` subfolder. If None, `call_report.core.default_download_dir()` is
  used."
- `source`: "Where to download release zips from. `"github"` uses this
  project's mirror of every FCA release and verifies each file's checksum.
  `"fca"` downloads from the FCA website."
- `snapshot`: "Which mirror snapshot to download from. `"latest"` is the
  current files. A date such as `"2026-10-12"` is the mirror as it stood on that
  date. Only `source="github"` has snapshots."
- `overwrite`: "Which downloaded quarters to download again. None downloads
  only quarters not yet in the download folder. `"outdated"` also replaces
  quarters whose files differ from the mirror's. `"all"` replaces every
  quarter in the range."
- `http_client`: "The library used for HTTP requests. `"urllib"` uses the
  standard library. `"curl_cffi"` needs `pip install "call-report[curl_cffi]"`."

Usage, as the user guide will show it:

```python
from call_report.fca import FCACallReport
from call_report.fca.download import FCAReleaseDownloader
from call_report.fca.transport import DownloadTransport, LocalDirectoryTransport

# Download on first use from the GitHub mirror, then read from the download folder.
report = FCACallReport(
    start="2020-03-31", end="2025-12-31", transport=DownloadTransport()
)

# Download ahead of time, straight from FCA, into a folder you choose.
downloader = FCAReleaseDownloader(download_dir="~/fca-data", source="fca").download(
    start="2020-03-31", end="2025-12-31"
)
downloader.errors_  # quarters that failed
downloader.retry_failed()

# Repeat an earlier analysis with the files the mirror held on that date.
pinned = FCAReleaseDownloader(snapshot="2026-10-12").download(
    start="2020-03-31", end="2025-12-31"
)
pinned.release_record(period=pinned.downloaded_[0])  # source, checksum, snapshot

# Refresh only the quarters whose files have changed on the mirror.
FCAReleaseDownloader(overwrite="outdated").download(
    start="2020-03-31", end="2025-12-31"
)

# Read the downloaded files later with no network access, and use the
# downloaded metadata so quarters newer than the installed package work too.
downloader.download_metadata()
report = FCACallReport(
    start="2020-03-31",
    end="2025-12-31",
    transport=LocalDirectoryTransport(data_dir=downloader.data_dir),
    metadata_dir=downloader.metadata_dir,
)
```

## Data

No shipped data changes.

Added:

- The `fca-call-report-data` rolling release and one `fca-data-<date>` snapshot
  release for each change, filled by the new workflow from
  `data/fca-call-report/` and `src/call_report/fca/data/`. `manifest.json` and
  `metadata.zip` are built by `scripts/build_fca_mirror_manifest.py`. None of
  it is in the wheel.
- `scripts/check_fca_drift.py`, which the nightly workflow runs.
- `tests/fca/data/download_page.html`, a trimmed copy of FCA's download page
  as of 2026-10-12. It keeps the markup around the zip links, plus one added
  zip link whose name does not parse and one absolute link.
- `tests/fca/data/manifest.json`, a small manifest for parser tests.

CLAUDE.md's repo layout section gains two lines. Adding a quarter's zip to
`data/fca-call-report/` also updates the mirror, through the workflow. The
newest quarter is found from the newest zip, so no constant needs changing.

Bringing the archive up to date with FCA (2026Q2, and the 12 quarters FCA
regenerated on 2026-08-31) is a separate data pull request. This plan
assumes it lands first, so the mirror starts with 106 current quarters.

## Pull requests

Four pull requests, all `Part of #119` except the last, landing in this
order.

1. **`[ENH] Add shared HTTP, archive, and directory helpers and transport periods (#119)`.**
   `core/_http.py`, `core/_archive.py`, `core/_dirs.py`,
   `ZipArchiveUnpackingError`, `InsecureConnectionWarning`, `fca_release_stem`,
   `parse_fca_release_filename`, `available_periods()` on the protocol and the
   two local transports, the `FCACallReport` validation change, and the removal
   of `LATEST_KNOWN_PERIOD`. `PackagedArchiveTransport` moves to
   `extract_zip`. The optional `curl_cffi` extra is added to `pyproject.toml`.
2. **`[MNT] Mirror FCA release zips as GitHub releases and check for drift (#119)`.**
   `scripts/build_fca_mirror_manifest.py`, `scripts/check_fca_drift.py`, their
   tests, `.github/workflows/fca-mirror.yml`, and
   `.github/workflows/fca-drift.yml`. After it merges, a maintainer runs the
   mirror workflow once by hand to publish all 106 quarters, then runs the
   drift workflow once to confirm it passes.
3. **`[ENH] Add FCAReleaseDownloader and DownloadTransport (#119)`.**
   `construct_fca_mirror_url`, `FCAReleaseCatalog`, the downloader, the
   transport, the live tests, the user guide page, and the doc, changelog,
   CLAUDE.md, and API reference updates.
4. **`[ENH] Download the shipped metadata and registry (#119)`.** The
   `metadata_dir` argument on the loaders and on `FCACallReport`,
   `FCAReleaseDownloader.download_metadata`, and the user guide section. This
   one closes #119 and sets this plan's status line.

The third pull request is not merged until the live tests pass against both
the real mirror and the real FCA site. The author runs them with
`--run-network` after the second pull request's workflow has published the
mirror, and puts the result in the pull request description. From then on the
nightly workflow runs them. Pull requests 1, 3, and 4 carry the
`run-exhaustive` label, because they change how archive releases are resolved
or how metadata is read. Pull request 2 carries `no-changelog`.

## Tests

No test in the default suite reaches the internet. HTTP tests use a
`http.server.ThreadingHTTPServer` on `127.0.0.1`, started by a fixture in
`tests/conftest.py` and stopped after the test. It serves files from a
temporary folder, can answer a path with a redirect, and can be told to
answer a path with a given sequence of status codes. This runs the real
`urllib` code end to end. Its helpers live in `tests/http_server.py`, not in
`conftest.py`. The mirror and FCA base URLs are module attributes that the
tests point at the local server.

`tests/core/test_http.py`:

- A 200 response returns the body, and `download_file` writes it.
- Every request carries the package's `User-Agent` (the server records it).
- A redirect is followed.
- A 503 then a 200 succeeds after one retry. A 429 with `Retry-After: 2`
  sleeps 2 seconds (the sleep is replaced, so the test does not wait).
- Retries stop at `max_retries`, and the last error is raised as
  `DownloadError`.
- A 404 raises at once, with no retry.
- A connection refused and a timeout are retried.
- A 403 with the body `error code: 1010`, an HTML page where a zip is
  expected, and a `cf-mitigated: challenge` header each raise a
  `DownloadError` naming Cloudflare, with no retry.
- A `file://` or `ftp://` URL raises before any request is made.
- A plain `http` URL to a host that is not loopback issues an
  `InsecureConnectionWarning` (`pytest.warns`, with the request itself
  replaced). A loopback `http` URL and an `https` URL issue none. A redirect
  to a plain `http` URL issues the warning.
- A failed `download_file` leaves no file behind.
- `http_client="curl_cffi"` uses a stub module placed in `sys.modules`, so
  this runs in every job whether or not the library is installed. It checks
  the same status handling and retries as `urllib`, and that `impersonate` is
  passed. A real `curl_cffi` test against the local server is skipped with
  `pytest.importorskip` when the library is absent.
- `http_client="curl_cffi"` with the library missing raises an error that
  gives the `pip install "call-report[curl_cffi]"` line.

`tests/core/test_archive.py`:

- A valid zip extracts to `target_dir`.
- A file that is not a zip, and a zip with a corrupt member, raise
  `ZipArchiveUnpackingError`.
- A member named `../evil.txt` or `/etc/evil.txt` raises the same error, and
  nothing is written.
- A failed extraction leaves no `target_dir`.
- `sha256_of` matches `hashlib` on a known file.
- The module imports without importing `call_report.core._http`.

`tests/core/test_dirs.py`:

- `CALL_REPORT_DOWNLOAD_DIR` wins when set.
- Windows, macOS, and other platforms each give their documented folder, with
  `sys.platform` and the environment patched by `monkeypatch`.
- `XDG_DATA_HOME` is honored, and `~/.local/share` is used without it.

`tests/fca/test_catalog.py` (added to):

- `fca_release_stem` for both eras, and for a quarter far past the newest zip.
- `parse_fca_release_filename` for both eras, mixed case, `Sep`, and names
  that do not parse. A `hypothesis` round trip:
  `parse(stem(p) + ".zip") == p` for every period.
- `construct_fca_download_url` and `construct_fca_mirror_url` for both eras
  and for a snapshot. A period before `EARLIEST_PERIOD` or after the quarter
  containing today (with `date.today` patched) raises
  `PeriodNotAvailableError`.
- `LATEST_KNOWN_PERIOD` is no longer importable from `call_report.fca.catalog`.
- `from_manifest_json` on the fixture reads URLs, checksums, and the snapshot,
  and rejects a manifest with an unknown `schema_version` or a missing key with
  `DownloadError`.
- `from_html` on the saved page: every period from 2000Q1 to 2026Q2 is
  present, relative links are resolved, and an unparsable zip link lands in
  `unmatched`.
- `from_html` on a page with no zip links raises `DownloadError`.
- `from_github` and `from_fca` against the local server.
- `url_for` raises `PeriodNotAvailableError` outside the catalog. `periods`,
  `earliest`, and `latest` are correct.

`tests/fca/test_download.py`:

- With `source="github"`, `download` over a range fetches the manifest and
  every quarter from the local server, verifies checksums, and extracts each
  quarter to `data_dir/<stem>/`.
- A zip whose checksum does not match the manifest is recorded as an error
  and not extracted.
- A newer quarter in the manifest is downloadable with no catalog passed.
- An unreadable manifest raises `DownloadError` and downloads nothing.
- With `source="fca"`, the catalog comes from the saved page served by the
  local server, quarters come from the FCA base URL, and no manifest is
  requested.
- With `snapshot="2026-10-12"`, files come from that snapshot's URLs. A
  snapshot with `source="fca"` raises `ValueError`.
- With `overwrite=None`, a downloaded quarter is in `skipped_` and is not
  requested.
- With `overwrite="all"`, it is downloaded again and its contents replaced.
- With `overwrite="outdated"`, only quarters whose recorded checksum differs
  from the manifest are downloaded again. With `source="fca"`, or a catalog
  with no checksums, it raises `ValueError`.
- Each extracted folder holds a `.call_report_release.json` with the URL,
  checksum, generation date, and snapshot. A downloaded quarter whose recorded
  checksum differs from the manifest is in `outdated_` and, with
  `overwrite=None`, is not downloaded again. A folder with no record is never
  in `outdated_`. `release_record` returns the record, or `None`.
- With `overwrite="all"` and a failing download, the existing folder is
  untouched.
- One quarter returning 404 lands in `errors_`, and the others download.
- A zip that downloads but cannot be unpacked lands in `errors_` with a message
  that says so.
- Every quarter failing raises `DownloadError`, and `errors_` is set.
- `retry_failed` downloads only the failed quarters, and empties `errors_`
  once the server answers. With nothing to retry, it makes no request.
- A zip with no `D_*.TXT` file is recorded as an error.
- A range outside the catalog raises `PeriodNotAvailableError` before any zip
  is requested.
- `max_workers=1` and `max_workers=4` give the same result.
- `download_dir=None` uses `default_download_dir()` (pointed at `tmp_path`
  through `CALL_REPORT_DOWNLOAD_DIR`). A `download_dir` with `~` is expanded,
  and a missing one is created.
- `http_client` is passed through to every request.

`tests/fca/test_transport.py` (added to):

- `available_periods()` on `LocalDirectoryTransport` with folders for some
  quarters, with a custom `dirname_for`, and with an empty folder.
- `available_periods()` on `PackagedArchiveTransport` against the real
  archive returns 2000Q1 through the newest zip.
- `DownloadTransport.resolve` downloads on first use and reads the download
  folder after that. With `overwrite="all"`, a second `resolve` of the same
  period does not request it again. With `overwrite=None`, an existing folder
  is not requested at all.
- `DownloadTransport.available_periods()` returns its catalog's periods.
- A corrupt packaged zip raises `ZipArchiveUnpackingError` from
  `PackagedArchiveTransport.resolve`.

`tests/fca/test_report.py` (added to):

- `fetch` raises `PeriodNotAvailableError` for an `end` after the transport's
  latest period and for a `start` before its earliest, and accepts a range the
  transport has even when it is newer than the shipped metadata.
- `fetch` with a transport that has no periods raises `DownloadError`.
- `available_periods()` follows the transport.
- `get_schema` for a quarter newer than the metadata it can read raises
  `ScheduleNotFoundError` with the message above. With a `metadata_dir` that
  covers the quarter, it succeeds.

`tests/fca/test_metadata_download.py`:

- `download_metadata` downloads `metadata.zip` from the local server, checks it
  against the manifest, and extracts it to `metadata_dir`.
- The loaders read from `metadata_dir` when it is given, and from the wheel
  when it is not. The per-process cache keeps the two apart.
- A bundle with a newer `schema_version` than the package reads is refused
  with an error that says to upgrade.
- `download_metadata` with `source="fca"` raises `DownloadError`.
- A bundle built by the script from the shipped files gives the same metadata
  and registry as the wheel for every quarter they share.

`tests/scripts/test_build_fca_mirror_manifest.py`:

- The manifest built from `data/fca-call-report/` lists all 106 quarters, with
  checksums that match `hashlib`, and source URLs that match
  `construct_fca_download_url`.
- `metadata.zip` holds the shipped schedule files and registry, and the
  manifest's `metadata` entry matches its checksum.
- A zip whose name does not parse, and a zip with no `D_*.TXT`, fail the
  script.

`tests/scripts/test_check_fca_drift.py`:

- Against a local server standing in for FCA and the mirror, the script exits
  zero when every file matches, and nonzero with a report that names a new
  quarter, a changed quarter, and a quarter FCA no longer lists.

`tests/fca/test_download_regression.py`:

- The local server serves the real zips from `data/fca-call-report/` with a
  manifest built by the script. For a legacy quarter (2003Q1), both sides of
  the era boundary (2014Q4 and 2015Q1), and the newest quarter,
  `FCACallReport` with a `DownloadTransport` gives the same `load_all()`,
  `to_long_format()`, and `institutions()` as with
  `PackagedArchiveTransport`. Four quarters keep this fast enough to run
  without the `slow` marker.

`tests/fca/test_live_sources.py`:

- Marked `network`, a new marker registered in `pyproject.toml`, and skipped
  unless `--run-network` is passed, the same way `--run-exhaustive` works.
- Every test runs once for each source, `"github"` and `"fca"`, so the mirror
  and the real FCA site are tested the same way.
- `FCAReleaseCatalog.from_github()` and `from_fca()` each list every period
  from 2000Q1 through the newest zip in `data/fca-call-report/`.
- For each source, one legacy and one modern quarter are downloaded and match
  the checked-in zips byte for byte.
- A `DownloadTransport` for each source loads a quarter through
  `FCACallReport`.
- A pinned snapshot downloads from the mirror, and `download_metadata` fetches
  a bundle the installed package accepts.
- Every zip FCA lists matches the mirror's checksum. A failure here means FCA
  has published or regenerated a quarter the mirror does not have.
- Not run on pull requests. The nightly workflow runs them.

Branch coverage stays at 100% in every job, including both clients, both
sources, and every value of `overwrite` on the downloader and on the
transport. The `curl_cffi` path is covered through the stub module, so no job
needs the library installed.

## Open questions

None.
