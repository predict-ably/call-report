# Plan: issue #119, Download FCA call report releases with a downloader and a transport

Status: proposed

Revised in #137, while pull request 2 was built. The catalog moved from pull
request 3 to pull request 2, and the drift check now compares FCA with the
archive. "Pull requests" lists every change.

## Goal

Give an installed user a way to get FCA release files from the package itself.
This is goal 5 of the v0.2 release, and the last goal before 0.2.0 is cut
(#64). It is tracked in #54.

In scope:

1. A mirror of every FCA release zip, published as assets of GitHub releases
   of this repository, with a manifest of checksums. A rolling release always
   holds the current files. A dated snapshot release is cut whenever the data
   changes, so a user can download exactly the files an earlier analysis used.
   A workflow keeps the mirror in step with `data/fca-call-report/`. A nightly
   check finds when FCA has published or changed a file, opens an issue, and
   opens a data pull request linked to it. The same check finds a mirror that
   has fallen out of step with the archive.
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
   quarter never needs a constant changed. `EARLIEST_PERIOD` becomes private.
7. A downloadable copy of the shipped schedule metadata and institution
   registry, published with the mirror, so a user can get a new quarter's
   metadata without upgrading the package.
8. Tests that never touch the internet in the default suite, and opt-in tests
   of the live mirror and the live FCA site. The nightly drift workflow runs
   the live tests once pull request 3 adds them.
9. A user guide page on downloading, and updates to the getting started page,
   the API reference, and the changelog. The page explains where downloaded
   files go, how to choose another folder, how to read them offline, how to
   pin a snapshot, and how to delete them.

Out of scope:

- Merger adjustment (version 0.3).
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
   current files with the mirror's manifest. When they differ, it opens an
   issue and a data pull request, so the repository is never more than a day
   behind FCA waiting for someone to notice. See "The nightly drift check".

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

`EARLIEST_PERIOD` is a different kind of constant. 2000Q1 is FCA's first
release and never changes, so it needs no upkeep. It is still needed in two
places. The local transports find their periods by checking each quarter for
a folder or zip, so they need a quarter to start from. They cannot work
backwards from folder names, because `dirname_for` can be any function.
`construct_fca_download_url` also needs it to reject a quarter before FCA's
first release with a clear error, rather than build a URL that returns 404.
So it stays, renamed `_FIRST_RELEASE_PERIOD`, private to `catalog.py`, and
used only for those two things. It is no longer public API.

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
the changelog records the break, along with the removal of the two public
period constants.

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
and the shipped metadata. It takes the snapshot being published
(`--snapshot`) and, when there is one, the rolling release's current manifest
(`--previous-manifest`). It writes three files.

- `manifest.json`. Its `snapshots` list is the previous manifest's list plus
  this snapshot. A snapshot already in that list fails the build, since a
  snapshot is never changed.
- `metadata.zip`. Its members are stored uncompressed, in name order, with a
  fixed timestamp and fixed permissions. So the same files always give the
  same bytes, and the same SHA-256. Without that, every run would see a
  changed bundle and cut a new snapshot.
- `changes.json`. It says whether anything changed, lists the files the
  rolling release needs uploaded (zips that are new or whose SHA-256 differs,
  and `metadata.zip` when it differs), and lists the zips it needs removed.

The script uses `parse_fca_release_filename`, `fca_release_stem`, and
`construct_fca_download_url` from the package, so the mirror and the package
agree on names. It checks each zip with `check_release_zip`, a private helper
in `call_report.fca._discovery`, which also gives `generated` from the
`_G<YYYYMMDD>` part of a modern era zip's data filenames. The pull request 3
downloader uses the same helper. The script fails if a zip's name is not the
exact name `fca_release_stem` gives its quarter, if a file is not a zip, if a
zip has no `D_*.TXT` file, if one zip's data files carry more than one
generation date, or if the shipped metadata stops before the newest archived
quarter. Before it writes anything, it reads its own manifest back with
`FCAReleaseCatalog.from_manifest_json`, so a manifest the package cannot read
is never published.

**Workflow.** `.github/workflows/fca-mirror.yml` runs on a push to `main` that
changes `data/fca-call-report/**` or `src/call_report/fca/data/**`, and by hand
(`workflow_dispatch`). It:

1. downloads the rolling release's current manifest, if there is one,
2. picks the snapshot name, `<today>`, or `<today>-2` and so on when a
   snapshot with that date already exists,
3. builds the three files with the script, and stops if `changes.json` says
   nothing changed,
4. creates the snapshot release `fca-data-<snapshot>` with `--latest=false`
   and `--target` set to the commit, holding every zip, the manifest, and
   `metadata.zip`,
5. brings the rolling release in line: it creates the release if it does not
   exist, uploads (`gh release upload --clobber`) each zip `changes.json`
   lists, and deletes each asset it says to remove,
6. uploads `metadata.zip` and then `manifest.json` last, so the manifest never
   lists a file that is not there yet.

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
  `snapshot="latest"`, reads the rolling release's manifest. Pinning applies
  only to `source="github"`, because FCA does not keep earlier versions.
  Passing a snapshot with `source="fca"` raises `ValueError`.
- **Zips always come from a snapshot.** A catalog built from a manifest points
  every URL at the snapshot release the manifest names, even when the
  manifest came from the rolling release. The rolling release is updated one
  asset at a time, and `--clobber` deletes an asset before uploading its
  replacement. So for a while its zips and its manifest can disagree, or a
  zip can be missing. A snapshot release never changes, so its zips always
  match the manifest's checksums.
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

`scripts/check_fca_drift.py` has three commands.

- **`check`** reads FCA's download page with `FCAReleaseCatalog.from_fca`,
  downloads every zip it lists (four at a time), and compares each SHA-256
  with the zip in `data/fca-call-report/`. It reports the quarters FCA has
  published that the archive lacks, the quarters whose bytes differ, the
  quarters the archive holds that FCA no longer lists, and any zip link whose
  name is not a release. Each new or changed zip is checked with
  `check_release_zip` and saved to a download folder. A zip that is not a
  release is reported as an error and not saved. The command also reads the
  mirror's manifest with `FCAReleaseCatalog.from_github` and compares it with
  the archive. It reports quarters missing from the mirror, quarters whose
  checksum differs, and quarters the mirror holds that the archive does not.
  A mirror whose manifest cannot be read is reported as unavailable, and
  counts as an error rather than drift, since nothing is known to differ. It
  writes a JSON report and a Markdown summary. It exits 0 when nothing
  differs, 1 when something does, and 2 when the check could not finish,
  such as when FCA refuses a request or the mirror cannot be read. An
  unexpected failure still writes a report holding the error, and exits 2.
- **`apply`** copies the zips a `check` saved into the archive, after checking
  each one's SHA-256 against the report. Every saved zip is checked before
  any is written. A copy that fails leaves no partly written file behind. It
  never deletes a zip, so a quarter FCA stops listing is only reported.
- **`describe`** writes the data pull request's description and adds its
  changelog entry under "Unreleased". An entry it wrote earlier for the same
  issue is replaced, so a second run leaves one entry. A changelog with no
  Unreleased section, as right after a release, gets one before its newest
  release.

`apply` and `describe` exit 0 on success and 2 on any failure. A script
that exited 1 on an uncaught exception would look like drift to the
workflow. Each option of each command has `--help` text.

`check` compares FCA with the archive, not with the mirror. The data pull
request changes the archive, so the archive is what has to match FCA. If the
check compared FCA with the mirror, a mirror that had fallen behind `main`
would produce a data pull request with nothing in it. The separate mirror
comparison still reports a failed publish.

Every request sends the same `User-Agent` as the downloader.

`.github/workflows/fca-drift.yml` runs every night and by hand.

1. It keeps a copy of `src/call_report/fca/data/` from `main`, then runs
   `check` on `main`.
2. If anything differs, or the check could not finish, it opens an issue
   labeled `fca-drift` with the summary, or comments on the issue that is
   already open. A new issue's title says which: "FCA's call report
   releases differ from the archive", or "The FCA drift check could not
   finish". Each comment
   carries a hidden fingerprint of the report, without its timestamp. A night
   whose findings match an earlier comment adds no new one.
3. If FCA's files differ from the archive, it builds the data pull request.
   - If a drift pull request is already open, it checks out that branch and
     merges `main` into it. Otherwise it creates `fca-drift/<YYYY-MM-DD>`
     from `main`.
   - It runs `apply` with the zips `check` saved. If the open drift branch
     already holds them, it stops there. This is decided with
     `git status --porcelain`, not `git diff`, because `git diff` ignores
     untracked files, and a new quarter's zip is untracked.
   - It runs both generation scripts. When only new quarters were added, they
     extend the base as usual. When an archived quarter changed, they run with
     a new `--rebuild` flag, which rebuilds the base from every quarter and
     replaces it, logging each difference instead of refusing to write.
     `--rebuild` and `--full` cannot be combined.
   - It runs `describe`, commits, and pushes.
   - It opens a pull request titled
     `[MNT] Bring the FCA archive up to date with FCA's site (#<issue>)`, with
     `Closes #<issue>` in its description and the `fca-drift` and
     `run-exhaustive` labels. On an open drift pull request, it updates the
     description instead.
4. The run fails only when the check could not finish, or the pull request
   could not be built. A difference alone is reported through the issue, so
   the nightly run does not stay red while a drift pull request waits for
   review.

An open drift pull request gets a new commit rather than a rebuilt branch.
Rebuilding it would mean force-pushing, which would drop any registry
overrides a reviewer has added there.

The pull request description follows the pull request template. It lists the
new quarters, the changed quarters, each schedule whose shipped metadata
changed, and each UNINUM whose name or address history changed in a quarter
the registry already covered. Quarters the regeneration adds are not
compared. That last list is what a reviewer checks for values a regeneration
backfilled into earlier quarters, which belong in the registry's override
file. A person reviews and merges it. On merge, the mirror workflow publishes
a new snapshot, and the next nightly run passes.

A pull request opened with the workflow's own `GITHUB_TOKEN` does not start
other workflows, so CI would never run on it. The workflow therefore opens the
pull request with a token from a repository secret, `FCA_DRIFT_TOKEN`, a
fine-grained token or GitHub App token limited to this repository with
contents, issues, and pull requests write access. A maintainer creates the
secret once. Without it, the workflow still opens the issue, says on it that
the pull request could not be opened, and fails.

If Cloudflare refuses the workflow's addresses, `check` exits 2, the issue
carries the Cloudflare message, and the run fails. That is also a signal worth
having.

`test.yml` gains `scripts/**` in its `paths`, so a change to a script runs
the tests in `tests/scripts/`.

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
  containing today's date.
- `EARLIEST_PERIOD` is renamed `_FIRST_RELEASE_PERIOD` and is no longer
  public. It is the lower bound on a period. The two generation scripts in
  `scripts/` import the private name.
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
- `FCAReleaseCatalog` is added (see "Public API"). It lands in pull request 2,
  because the drift check reads FCA's page and the mirror's manifest through
  it.
- The scrape uses a private `HTMLParser` subclass that collects every `href`
  ending in `.zip`, resolved with `urllib.parse.urljoin`. The filename is
  read from the URL's path, so a query string does not matter. Links whose
  filenames parse become catalog entries. Links that do not parse are kept on
  `unmatched`. A link repeated with the same URL counts once.
- A page with no parsable zip links raises `ReleaseCatalogError` saying the
  page layout may have changed. So does a page that links two different
  URLs for one quarter. An empty catalog is never returned.
- `from_manifest_json` checks the manifest's `schema_version`, `snapshot`,
  and `releases`. For each release, it checks the period, that `file` is the
  exact name `fca_release_stem` gives that period, and that `sha256` is 64
  lower case hex digits. It rejects a period listed twice. A
  `schema_version` must be an integer, not `true`, and a period must be a
  date string. A missing key raises `ReleaseCatalogError` naming the key. A
  `schema_version` newer than the package reads asks the user to upgrade.
  Keys the catalog does not use, such as `size` and `metadata`, are not
  checked.
- The catalog stores sorted, read-only copies of its mappings, so `periods`
  is oldest first and a mapping the caller keeps cannot change it. Its repr
  gives the size, the span, and the snapshot, not every URL. It sets
  `__hash__ = None`, so `hash()` fails at once with a clear message, as it
  does for `pandas.DataFrame`. A frozen dataclass would otherwise generate a
  `__hash__` that fails later, on the mappings.
- Text that cannot be read as a list of releases (a bad manifest, a page
  with no release links, a manifest that is not UTF-8, or one describing a
  different snapshot) raises `ReleaseCatalogError`, a new exception that is
  both a `DownloadError` and a `ValueError`. Code that catches failed
  downloads keeps working, and code that expects `ValueError` for bad input
  text gets one. A request that fails is still a plain `DownloadError`.
- `DOWNLOAD_PAGE_URL` and `MIRROR_BASE_URL` are read when a method runs, not
  when it is defined, so tests point them at the local server.
- The two constants' docstrings cannot pass numpydoc's GL02 check, because
  autodoc hands numpydoc the text without the line holding the closing
  quotes. Both numpydoc configs (`docs/source/conf.py` and
  `pyproject.toml`) therefore exclude these two objects by their full
  names, and nothing else.
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
  `_FIRST_RELEASE_PERIOD` through the quarter containing today, whether
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
hold. A catalog built from the mirror carries `sha256` and `snapshot`, and
every URL in it points at the snapshot release its manifest names. One built
from FCA's page carries neither. `LATEST_KNOWN_PERIOD` is removed from
this module, and `EARLIEST_PERIOD` becomes the private `_FIRST_RELEASE_PERIOD`.

New in `call_report.exceptions`:

```python
class ZipArchiveUnpackingError(CallReportError):
    """A zip archive could not be unpacked.

    Raised for a file that is not a zip, a zip with a corrupt member, or a
    member whose path would land outside the target folder.
    """


class InsecureConnectionWarning(UserWarning):
    """A request was made over plain HTTP to a host that is not this machine."""


class ReleaseCatalogError(DownloadError, ValueError):
    """A list of releases could not be read."""
```

`ReleaseCatalogError` is added in pull request 2.

New in `call_report.core`:

```python
def default_download_dir() -> Path:
    """Return the folder call_report keeps downloaded files in."""
```

`call_report.core._archive` (private) also gains
`sha256_of_bytes(data: bytes) -> str` beside `sha256_of`. Both scripts use
it, so neither defines its own.

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
  date. FCA's own site keeps no snapshots."
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
- A `--rebuild` flag on `scripts/generate_fca_schedule_metadata.py` and
  `scripts/generate_fca_institution_registry.py`.
- The `FCA_DRIFT_TOKEN` repository secret, created by a maintainer.
- `tests/fca/data/download_page.html`, a trimmed copy of FCA's download page
  as of 2026-10-12. It keeps the page's main content. Its Sept2014 link is
  made absolute, and a zip link whose name does not parse and a link that is
  not a zip are added.
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
   Merged in #135.
   `core/_http.py`, `core/_archive.py`, `core/_dirs.py`,
   `ZipArchiveUnpackingError`, `InsecureConnectionWarning`, `fca_release_stem`,
   `parse_fca_release_filename`, `available_periods()` on the protocol and the
   two local transports, the `FCACallReport` validation change, the removal
   of `LATEST_KNOWN_PERIOD`, and making `EARLIEST_PERIOD` private. `PackagedArchiveTransport` moves to
   `extract_zip`. The optional `curl_cffi` extra is added to `pyproject.toml`.
2. **`[ENH] Add the release catalog, FCA mirror, and drift check (#119)`.**
   #137. `construct_fca_mirror_url`, `FCAReleaseCatalog`, `DOWNLOAD_PAGE_URL`,
   `MIRROR_BASE_URL`, the private `check_release_zip` and
   `release_generation_date` helpers in `call_report.fca._discovery`,
   `scripts/build_fca_mirror_manifest.py`, `scripts/check_fca_drift.py`, the
   `--rebuild` flag on both generation scripts, their tests, the saved
   download page and manifest fixtures, `.github/workflows/fca-mirror.yml`,
   `.github/workflows/fca-drift.yml`, `scripts/**` in `test.yml`, the
   changelog and API reference entries for the catalog, and the CLAUDE.md
   repo layout lines. After it merges, a maintainer creates the
   `FCA_DRIFT_TOKEN` secret, runs the mirror workflow once by hand to publish
   all 106 quarters, turns on the repository's release immutability setting,
   then runs the drift workflow once to confirm it passes. The setting
   applies only to releases published after it is on, and it locks a
   release's assets once published. So the rolling release, which the
   mirror workflow keeps editing, must exist before the setting is turned
   on. Every later snapshot is then locked by GitHub, not just by
   convention.
3. **`[ENH] Add FCAReleaseDownloader and DownloadTransport (#119)`.**
   `FCASource`, the downloader, the transport, the live tests and their step
   in `fca-drift.yml`, the user guide page, and the doc, changelog, and API
   reference updates.
4. **`[ENH] Download the shipped metadata and registry (#119)`.** The
   `metadata_dir` argument on the loaders and on `FCACallReport`,
   `FCAReleaseDownloader.download_metadata`, and the user guide section. This
   one closes #119 and sets this plan's status line.

The catalog was first planned for pull request 3. It moved to pull request 2
because the drift check has to read FCA's download page and the mirror's
manifest, and the catalog is what parses both. Moving it avoids throwaway
parsers in the scripts, and tests the manifest writer and the package's
reader in the same pull request. Because it adds public API, pull request 2
is `[ENH]` with a changelog entry, not the `[MNT]` and `no-changelog` first
planned.

The third pull request is not merged until the live tests pass against both
the real mirror and the real FCA site. The author runs them with
`--run-network` after the second pull request's workflow has published the
mirror, and puts the result in the pull request description. From then on the
nightly workflow runs them. Pull requests 1, 3, and 4 carry the
`run-exhaustive` label, because they change how archive releases are resolved
or how metadata is read. Pull request 2 changes neither the archive nor how a
release is parsed, so it does not.

## Tests

No test in the default suite reaches the internet. HTTP tests use a
`http.server.ThreadingHTTPServer` on `127.0.0.1`, started by a fixture in
`tests/conftest.py` and stopped after the test. It serves files from a
temporary folder, can answer a path with a redirect, and can be told to
answer a path with a given sequence of status codes. This runs the real
`urllib` code end to end. Its helpers live in `tests/http_server.py`, not in
`conftest.py`. The mirror and FCA base URLs are module attributes that the
tests point at the local server, through the `mirror` and `fca_site`
fixtures in `tests/conftest.py`.

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
  and for a snapshot, including a `-2` snapshot. A period before
  `_FIRST_RELEASE_PERIOD` or after the quarter containing today (with
  `catalog._today` replaced) raises `PeriodNotAvailableError`. An invalid
  snapshot raises `ValueError`.
- Neither `LATEST_KNOWN_PERIOD` nor `EARLIEST_PERIOD` is importable from
  `call_report.fca.catalog`.

`tests/fca/test_release_catalog.py` (new, for `FCAReleaseCatalog`):

- The catalog sorts its periods, keeps read-only copies of its mappings,
  refuses to be empty or to hold a checksum with no URL, cannot be hashed,
  and has a short repr.
- `from_manifest_json` on the fixture reads URLs, checksums, and the snapshot,
  and every URL points at the snapshot release. Each way a manifest can be
  wrong raises `ReleaseCatalogError` saying why: not JSON, not an object, a
  missing or unknown key value, a `schema_version` of `true`, a newer
  `schema_version` (asking for an upgrade), an invalid snapshot, no releases,
  a bad or non-string period, a file name that is not that period's, a bad
  checksum, and a period listed twice. The same error is a `ValueError`.
- `from_html` on the saved page: every archived period from 2000Q1 is
  present, relative and absolute links are resolved, and an unparsable zip
  link lands in `unmatched`. Other links, repeated links, query strings, and
  letter case are handled. Two URLs for one quarter, and a page with no
  release links, raise `DownloadError`.
- `from_github` reads the rolling or a pinned snapshot's manifest from the
  local server. A manifest for another snapshot, one that is not UTF-8, and a
  missing one raise `DownloadError`. `from_fca` reads the saved page from the
  local server. Both pass `timeout`, `max_retries`, and `http_client` on.
- `url_for` raises `PeriodNotAvailableError` outside the catalog, naming the
  catalog's range.

`tests/fca/test_discovery.py` (added to):

- `release_generation_date` reads a modern release's date, gives `None` for a
  legacy release, and rejects two dates.
- `check_release_zip` returns the date, even for files in a folder inside the
  zip. It raises `ZipArchiveUnpackingError` for a file that is not a zip, and
  `ValueError` for a zip with no layout file or two generation dates.

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

- The manifest built from `data/fca-call-report/` lists every archived
  quarter, with checksums and sizes that match the files, source URLs that
  match `construct_fca_download_url`, and generation dates for modern
  releases only.
- `FCAReleaseCatalog.from_manifest_json` reads the manifest, with URLs in the
  new snapshot's release.
- `metadata.zip` holds the shipped schedule files and registry unchanged,
  plus `metadata.json`, and the manifest's `metadata` entry matches it.
- Two builds from the same files give the same `metadata.zip`, and every
  member has the same timestamp, permissions, and compression.
- `changes.json`: a first build uploads every file, an unchanged build
  uploads nothing, and a changed zip, changed bundle, and dropped zip are
  listed. `snapshots` accumulates, and a snapshot already published fails.
- An invalid snapshot, an empty archive, a misnamed zip (wrong case, `Sep`,
  or not a release), a file that is not a zip, a zip with no `D_*.TXT`, a zip
  with two generation dates, metadata behind the archive, and a metadata
  folder with no schedules each fail the script. The metadata covers the
  earlier of the schedules and the registry.
- `main` writes the three files and returns 0, or logs the reason and
  returns 1 with nothing written.

`tests/scripts/test_check_fca_drift.py`:

- Against a local server standing in for FCA and the mirror, `check` is
  clean when every file matches. It reports a new quarter, a changed quarter,
  a quarter FCA no longer lists, and an unmatched zip link, and saves only
  the zips that differ.
- An unreadable download page, a failed zip download, and a zip that is not a
  release are errors (exit 2). The other quarters are still compared.
- A mirror missing a quarter, or holding a different or an extra one, is
  drift. A mirror with no manifest is an error (exit 2), not drift.
- A report round trips through JSON.
- `apply` writes the new and changed zips and leaves a quarter FCA no longer
  lists in place. A saved zip that is missing or differs from the report
  writes nothing. A copy that fails leaves no `.part` file.
- The summary lists each kind of difference and always describes the mirror.
- `describe`: changed UNINUMs (a backfilled value, a dropped quarter, a
  dropped UNINUM, and not a new quarter), changed schedule files, the
  changelog entry's wording and wrapping, adding then replacing the entry
  for the same issue while keeping another issue's, and adding an
  Unreleased section when there is none, before a release or at the end.
- `main` runs each command and returns its exit status. A `check` that
  fails unexpectedly still writes a report and exits 2, and a `describe`
  that fails exits 2. Every command's `--help` describes its options.

`tests/scripts/test_generate_fca_schedule_metadata.py` and
`tests/scripts/test_generate_fca_institution_registry.py` (added to):

- `--rebuild` with a changed archived quarter replaces the base and logs the
  difference, where `--full` refuses to write. `--full` and `--rebuild`
  cannot be combined.

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
