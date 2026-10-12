"""Download files over HTTP, retrying transient failures.

These helpers are shared by every call report source. They use
``urllib.request`` from the standard library by default. Passing
``http_client="curl_cffi"`` sends the same requests through the optional
``curl_cffi`` library instead, which presents a browser's TLS fingerprint.
Status handling, redirects, retries, and the Cloudflare check are the same
code for both clients.
"""

from __future__ import annotations

import email.utils
import functools
import http.client
import random
import ssl
import tempfile
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import urljoin, urlsplit

from call_report import __version__
from call_report.core._dependencies import import_optional
from call_report.exceptions import DownloadError, InsecureConnectionWarning

HttpClient = Literal["urllib", "curl_cffi"]

USER_AGENT = f"call-report/{__version__} (+https://github.com/predict-ably/call-report)"
"""str: The ``User-Agent`` header sent with every ``urllib`` request.

With ``http_client="curl_cffi"``, the browser's own ``User-Agent`` is sent
instead, so the header matches the browser's TLS fingerprint.
"""

_CURL_CFFI_MIN_VERSION = "0.6.2"
_CURL_CFFI_INSTALL_MESSAGE = 'Install it with `pip install "call-report[curl_cffi]"`.'

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 10
_RETRY_AFTER_STATUSES = frozenset({429, 503})
_BACKOFF_SECONDS = 1.0
# Each backoff is lengthened by up to this fraction at random, so several
# downloads that fail together do not all retry at the same moment.
_BACKOFF_JITTER = 0.25
_MAX_RETRY_AFTER_SECONDS = 60.0
_CHUNK_SIZE = 64 * 1024
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Cloudflare answers a refused request with a short plain text body such as
# "error code: 1010". Only the start of the body is examined.
_CLOUDFLARE_BODY_PREFIX = b"error code: 1"
_CLOUDFLARE_SNIFF_BYTES = 512

# Replaced by tests, so a retry never waits in the test suite and every wait
# is predictable.
_sleep: Callable[[float], None] = time.sleep
_jitter: Callable[[], float] = random.random

# Request headers that carry credentials. They are not sent on to another
# host when a redirect leaves the one the caller asked for.
_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie"})

# curl's code for a TLS certificate that cannot be verified,
# CURLE_PEER_FAILED_VERIFICATION. A retry fails the same way.
_CURL_CERTIFICATE_ERROR = 60

# How far up the stack an InsecureConnectionWarning points, so it names the
# caller of fetch_bytes or download_file. A test checks both values.
_WARN_STACKLEVEL = 4
_WARN_STACKLEVEL_ON_REDIRECT = 5

_T = TypeVar("_T")


class _TransientError(Exception):
    """A failure that may succeed if the request is sent again.

    Raised for a connection error, a timeout, an HTTP 429, or an HTTP 5xx.

    Parameters
    ----------
    message : str
        What went wrong.
    retry_after : float or None, optional
        How long the server asked the client to wait, in seconds, if it
        said.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True, kw_only=True)
class _Response:
    """One HTTP response, the same shape whichever client received it.

    Each client's own response object is converted to this, so status
    handling, redirects, and retries are written once for both.

    Attributes
    ----------
    status : int
        The HTTP status code.
    headers : dict[str, str]
        The response headers, with lower case names.
    chunks : Iterator[bytes]
        The response body, read in pieces.
    """

    status: int
    headers: dict[str, str]
    chunks: Iterator[bytes]


_Opener = Callable[..., AbstractContextManager[_Response]]


def fetch_bytes(
    url: str,
    *,
    timeout: float = 60.0,
    max_retries: int = 3,
    headers: Mapping[str, str] | None = None,
    http_client: HttpClient = "urllib",
) -> bytes:
    """Download a URL and return its body.

    Redirects are followed. A redirect to another host is sent without the
    ``Authorization`` and ``Cookie`` headers. Connection errors, timeouts,
    HTTP 429, and HTTP 5xx responses are retried. The wait before each retry
    doubles from one second, with up to a quarter added at random, unless a
    ``Retry-After`` header asks for a different wait. Any other failure is
    raised at once.

    Parameters
    ----------
    url : str
        The ``http`` or ``https`` URL to download.
    timeout : float, default 60.0
        Seconds to wait for the server before a request fails.
    max_retries : int, default 3
        How many times to send a request again after a transient failure.
    headers : Mapping[str, str], optional
        Extra request headers. With ``http_client="urllib"``, every request
        also sends `USER_AGENT`. With ``"curl_cffi"``, every request sends
        the impersonated browser's own ``User-Agent`` instead.
    http_client : {"urllib", "curl_cffi"}, default "urllib"
        The library used for HTTP requests. ``"urllib"`` uses the standard
        library. ``"curl_cffi"`` needs
        ``pip install "call-report[curl_cffi]"``.

    Returns
    -------
    bytes
        The response body.

    Raises
    ------
    DownloadError
        If the URL is not ``http`` or ``https``, the server refuses the
        request, or every attempt fails. The message names the URL and the
        reason.
    ModuleNotFoundError
        If `http_client` is ``"curl_cffi"`` and the library is not
        installed.

    Warns
    -----
    InsecureConnectionWarning
        If the URL, or a redirect, is plain ``http`` to a host other than
        this machine.

    Examples
    --------
    >>> fetch_bytes("ftp://example.com/file.zip")
    Traceback (most recent call last):
    ...
    call_report.exceptions.DownloadError: Only http and https URLs can be downloaded, not 'ftp://example.com/file.zip'.
    """  # noqa: E501
    return _request(
        url,
        timeout=timeout,
        max_retries=max_retries,
        headers=headers,
        http_client=http_client,
        consume=lambda response: b"".join(response.chunks),
    )


def download_file(
    url: str,
    *,
    destination: Path,
    timeout: float = 60.0,
    max_retries: int = 3,
    headers: Mapping[str, str] | None = None,
    http_client: HttpClient = "urllib",
) -> Path:
    """Download a URL to a file.

    The body is written to a temporary file in the destination's folder,
    which is renamed to `destination` once the download completes. A
    failed download leaves no file behind, and an existing file at
    `destination` is replaced only by a complete download. Redirects and
    retries work as in `fetch_bytes`.

    Parameters
    ----------
    url : str
        The ``http`` or ``https`` URL to download.
    destination : pathlib.Path
        The file to write. Its folder must exist.
    timeout : float, default 60.0
        Seconds to wait for the server before a request fails.
    max_retries : int, default 3
        How many times to send a request again after a transient failure.
    headers : Mapping[str, str], optional
        Extra request headers. With ``http_client="urllib"``, every request
        also sends `USER_AGENT`. With ``"curl_cffi"``, every request sends
        the impersonated browser's own ``User-Agent`` instead.
    http_client : {"urllib", "curl_cffi"}, default "urllib"
        The library used for HTTP requests. ``"urllib"`` uses the standard
        library. ``"curl_cffi"`` needs
        ``pip install "call-report[curl_cffi]"``.

    Returns
    -------
    pathlib.Path
        `destination`.

    Raises
    ------
    DownloadError
        If the URL is not ``http`` or ``https``, the server refuses the
        request, or every attempt fails. The message names the URL and the
        reason.
    ModuleNotFoundError
        If `http_client` is ``"curl_cffi"`` and the library is not
        installed.

    Warns
    -----
    InsecureConnectionWarning
        If the URL, or a redirect, is plain ``http`` to a host other than
        this machine.

    Examples
    --------
    >>> import tempfile
    >>> from pathlib import Path
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     download_file("file:///etc/hosts", destination=Path(tmp) / "hosts")
    Traceback (most recent call last):
    ...
    call_report.exceptions.DownloadError: Only http and https URLs can be downloaded, not 'file:///etc/hosts'.
    """  # noqa: E501
    destination = Path(destination)

    def write(response: _Response) -> Path:
        """Write a response body to a temporary file, then rename it into place.

        The temporary file is in the destination's folder, so the rename
        never crosses file systems. It is removed if writing fails.

        Parameters
        ----------
        response : _Response
            The successful response to read.

        Returns
        -------
        pathlib.Path
            `destination`.
        """
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
            dir=destination.parent, prefix=f".{destination.name}.", delete=False
        )
        partial = Path(handle.name)
        try:
            with handle:
                for chunk in response.chunks:
                    handle.write(chunk)
            partial.replace(destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return destination

    return _request(
        url,
        timeout=timeout,
        max_retries=max_retries,
        headers=headers,
        http_client=http_client,
        consume=write,
    )


def _request(
    url: str,
    *,
    timeout: float,
    max_retries: int,
    headers: Mapping[str, str] | None,
    http_client: HttpClient,
    consume: Callable[[_Response], _T],
) -> _T:
    """Send a GET request, retrying transient failures, and consume the body.

    The URL is checked and the client is loaded before any request is
    sent. Each retry starts again from `url`, and waits longer than the
    one before.

    Parameters
    ----------
    url : str
        The URL to request.
    timeout : float
        Seconds to wait for the server before a request fails.
    max_retries : int
        How many times to send a request again after a transient failure.
    headers : Mapping[str, str] or None
        Extra request headers.
    http_client : {"urllib", "curl_cffi"}
        The library used for HTTP requests.
    consume : Callable[[_Response], _T]
        Reads the body of a successful response.

    Returns
    -------
    _T
        Whatever `consume` returns.

    Raises
    ------
    DownloadError
        If the request cannot succeed or every attempt fails.
    ValueError
        If `http_client` is not a known client.
    """
    _check_url(url, stacklevel=_WARN_STACKLEVEL)
    opener = _opener_for(http_client)
    # curl_cffi sends the impersonated browser's own User-Agent. Sending ours
    # with a browser's TLS fingerprint would be a mismatch that Cloudflare can
    # treat as a sign of a bot.
    default_headers = {"User-Agent": USER_AGENT} if http_client == "urllib" else {}
    request_headers = {**default_headers, **(headers or {})}

    attempt = 0
    while True:
        try:
            return _request_once(
                url,
                headers=request_headers,
                timeout=timeout,
                opener=opener,
                consume=consume,
            )
        except _TransientError as error:
            if attempt >= max_retries:
                raise DownloadError(
                    f"Could not download {url} after {attempt + 1} attempt(s). "
                    f"The last attempt failed: {error}"
                ) from error
            delay = (
                error.retry_after
                if error.retry_after is not None
                else _BACKOFF_SECONDS * 2**attempt * (1 + _BACKOFF_JITTER * _jitter())
            )
            _sleep(delay)
            attempt += 1


def _request_once(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    opener: _Opener,
    consume: Callable[[_Response], _T],
) -> _T:
    """Send one GET request, follow its redirects, and consume the body.

    Each redirect is checked by `_check_url` before it is followed. A
    redirect to another host is sent without the ``Authorization`` and
    ``Cookie`` headers. The final response is checked by `_check_response`
    before `consume` reads it.

    Parameters
    ----------
    url : str
        The URL to request.
    headers : Mapping[str, str]
        The request headers.
    timeout : float
        Seconds to wait for the server before a request fails.
    opener : Callable[..., AbstractContextManager[_Response]]
        Sends one request and yields its response without following a
        redirect.
    consume : Callable[[_Response], _T]
        Reads the body of a successful response.

    Returns
    -------
    _T
        Whatever `consume` returns.

    Raises
    ------
    DownloadError
        If the response is a failure that a retry would not fix, or the
        redirects do not end.
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        with opener(current, headers=headers, timeout=timeout) as response:
            location = response.headers.get("location")
            if response.status in _REDIRECT_STATUSES and location:
                target = urljoin(current, location)
                _check_url(target, stacklevel=_WARN_STACKLEVEL_ON_REDIRECT)
                if urlsplit(target).netloc.lower() != urlsplit(current).netloc.lower():
                    headers = {
                        name: value
                        for name, value in headers.items()
                        if name.lower() not in _CREDENTIAL_HEADERS
                    }
                current = target
                continue
            _check_response(response, requested=url, current=current)
            return consume(response)
    raise DownloadError(f"{url} redirected more than {_MAX_REDIRECTS} times.")


def _check_url(url: str, *, stacklevel: int) -> None:
    """Refuse a URL that is not ``http`` or ``https``, and warn about plain HTTP.

    Called for the URL a caller asks for and again for every redirect, so a
    redirect cannot lead to a ``file`` URL or to plain HTTP unnoticed.

    Parameters
    ----------
    url : str
        The URL about to be requested.
    stacklevel : int
        Passed to `warnings.warn`, so the warning points at the caller.

    Raises
    ------
    DownloadError
        If the URL's scheme is not ``http`` or ``https``.

    Warns
    -----
    InsecureConnectionWarning
        If the URL is plain ``http`` to a host other than this machine.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise DownloadError(f"Only http and https URLs can be downloaded, not {url!r}.")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
        warnings.warn(
            f"{url} is plain HTTP, so the download is not private and could be "
            "altered on the way. Use an https URL if the server offers one.",
            InsecureConnectionWarning,
            stacklevel=stacklevel,
        )


def _check_response(response: _Response, *, requested: str, current: str) -> None:
    """Raise unless a response is a success whose body should be read.

    A Cloudflare refusal and any 4xx raise `DownloadError`, which is not
    retried. A 429 or 5xx raises `_TransientError`, which is.

    Parameters
    ----------
    response : _Response
        The final response, after any redirects.
    requested : str
        The URL the caller asked for.
    current : str
        The URL that gave `response`.

    Raises
    ------
    DownloadError
        For a Cloudflare refusal, or a 4xx or other status a retry would
        not fix.
    _TransientError
        For an HTTP 429 or 5xx.
    """
    status = response.status
    if "cf-mitigated" in response.headers:
        raise _cloudflare_error(
            requested, f"it answered with a {response.headers['cf-mitigated']!r}"
        )
    if 200 <= status < 300:
        expects_zip = urlsplit(requested).path.lower().endswith(".zip")
        content_type = response.headers.get("content-type", "").lower()
        if expects_zip and content_type.startswith("text/html"):
            raise _cloudflare_error(
                requested, "it returned a web page where a zip file was expected"
            )
        return
    if status == 429 or status >= 500:
        retry_after = (
            _parse_retry_after(response.headers.get("retry-after"))
            if status in _RETRY_AFTER_STATUSES
            else None
        )
        raise _TransientError(
            f"{current} returned HTTP {status}.", retry_after=retry_after
        )
    if status == 403:
        body = next(response.chunks, b"")[:_CLOUDFLARE_SNIFF_BYTES]
        if body.lstrip().startswith(_CLOUDFLARE_BODY_PREFIX):
            code = body.strip().decode("ascii", errors="replace")
            raise _cloudflare_error(requested, f"it answered {code!r}")
    raise DownloadError(
        f"Could not download {requested}: {current} returned HTTP {status}."
    )


def _cloudflare_error(url: str, detail: str) -> DownloadError:
    """Return the error raised when Cloudflare refuses a request.

    The message names Cloudflare and suggests another source or the
    ``curl_cffi`` client, since retrying the same request will not help.

    Parameters
    ----------
    url : str
        The URL the caller asked for.
    detail : str
        How the refusal showed itself.

    Returns
    -------
    DownloadError
        An error naming Cloudflare. It is not retried, because a refused
        client is refused again.
    """
    return DownloadError(
        f"Could not download {url}: Cloudflare refused the request ({detail}). "
        "Sending the request again will not help. Download the file from another "
        'source, or pass http_client="curl_cffi".'
    )


def _parse_retry_after(value: str | None) -> float | None:
    """Return the wait a ``Retry-After`` header asks for, in seconds.

    The wait is never negative and is capped at a minute.

    Parameters
    ----------
    value : str or None
        The header's value, either a number of seconds or an HTTP date.

    Returns
    -------
    float or None
        The wait in seconds, or ``None`` if there is no header or it cannot
        be read.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_SECONDS)


def _opener_for(http_client: HttpClient) -> _Opener:
    """Return the function that sends one request with the chosen client.

    The ``curl_cffi`` library is imported here, before any request is sent,
    so a missing library fails at once.

    Parameters
    ----------
    http_client : {"urllib", "curl_cffi"}
        The library used for HTTP requests.

    Returns
    -------
    Callable[..., AbstractContextManager[_Response]]
        Sends one request and yields its response.

    Raises
    ------
    ValueError
        If `http_client` is not a known client.
    """
    if http_client == "urllib":
        return _urllib_open
    if http_client == "curl_cffi":
        return functools.partial(_curl_open, curl_cffi=_import_curl_cffi())
    raise ValueError(
        f'http_client must be "urllib" or "curl_cffi", not {http_client!r}.'
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect handler that hands every redirect back to the caller.

    `_request_once` follows redirects itself, so that each one is checked
    the same way for both clients.
    """

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Decline to follow a redirect.

        Returning nothing makes `urllib` raise the redirect as an
        `urllib.error.HTTPError`, which `_urllib_open` turns back into a
        response.

        Parameters
        ----------
        *args : object
            The arguments `urllib` passes, ignored here.
        **kwargs : object
            The keyword arguments `urllib` passes, ignored here.
        """
        return


_URLLIB_OPENER = urllib.request.build_opener(_NoRedirectHandler)


@contextmanager
def _urllib_open(
    url: str, *, headers: Mapping[str, str], timeout: float
) -> Iterator[_Response]:
    """Send one GET request with `urllib` and yield the response.

    A 3xx, 4xx, or 5xx response is yielded like any other, so its status
    can be handled by the shared code. The connection is closed when the
    block ends.

    Parameters
    ----------
    url : str
        The ``http`` or ``https`` URL to request.
    headers : Mapping[str, str]
        The request headers.
    timeout : float
        Seconds to wait for the server before the request fails.

    Yields
    ------
    _Response
        The response, whatever its status. A redirect is not followed.

    Raises
    ------
    DownloadError
        If the server's TLS certificate cannot be verified.
    _TransientError
        If no response arrives, or the connection fails while reading.
    """
    request = urllib.request.Request(url, headers=dict(headers))  # noqa: S310
    try:
        # The scheme was checked to be http or https by `_check_url`.
        raw: http.client.HTTPResponse | urllib.error.HTTPError = _URLLIB_OPENER.open(
            request, timeout=timeout
        )
    except urllib.error.HTTPError as error:
        # urllib raises a 3xx, 4xx, or 5xx response as an error. It is still
        # a response, and its status decides what happens next.
        raw = error
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            raise DownloadError(
                f"Could not download {url}: the server's certificate could not "
                f"be verified ({error.reason})."
            ) from error
        raise _TransientError(f"{url} could not be reached: {error.reason}") from error
    except (OSError, http.client.HTTPException) as error:
        raise _TransientError(f"{url} could not be reached: {error!r}") from error
    # An HTTPError's status is typed as optional, but its code is always set.
    status = raw.code if isinstance(raw, urllib.error.HTTPError) else raw.status
    with raw:
        response_headers = {name.lower(): value for name, value in raw.headers.items()}
        yield _Response(
            status=status,
            headers=response_headers,
            chunks=_read_chunks(
                raw.read,
                url=url,
                expected_length=_content_length(response_headers),
            ),
        )


def _content_length(headers: Mapping[str, str]) -> int | None:
    """Return the body length a response's headers promise, if they say.

    `_read_chunks` compares this with the bytes received, to catch a body
    cut short.

    Parameters
    ----------
    headers : Mapping[str, str]
        The response headers, with lower case names.

    Returns
    -------
    int or None
        The ``Content-Length``, or ``None`` if it is missing or not a
        whole number.
    """
    value = headers.get("content-length", "")
    return int(value) if value.isdigit() else None


def _read_chunks(
    read: Callable[[int], bytes], *, url: str, expected_length: int | None
) -> Iterator[bytes]:
    """Yield a response body in pieces until it ends.

    A body that ends before the promised length raises, so a download cut
    short is retried rather than kept.

    Parameters
    ----------
    read : Callable[[int], bytes]
        Reads up to the given number of bytes from the body.
    url : str
        The URL being read, used in error messages.
    expected_length : int or None
        The body length the response promised, if it said.

    Yields
    ------
    bytes
        The next piece of the body.

    Raises
    ------
    _TransientError
        If the connection fails while reading, or the body ends before the
        promised length.
    """
    received = 0
    while True:
        try:
            chunk = read(_CHUNK_SIZE)
        except (OSError, http.client.HTTPException) as error:
            raise _TransientError(
                f"the connection to {url} failed while reading: {error!r}"
            ) from error
        if not chunk:
            break
        received += len(chunk)
        yield chunk
    # http.client returns an empty read, not an error, when the connection
    # closes before the promised length, so a short body is caught here.
    if expected_length is not None and received < expected_length:
        raise _TransientError(
            f"the connection to {url} failed while reading: the body ended after "
            f"{received} of {expected_length} bytes."
        )


def _import_curl_cffi() -> Any:
    """Import the optional ``curl_cffi`` library.

    Its ``requests`` submodule is imported too, and its version is checked
    against the floor this package supports.

    Returns
    -------
    Any
        The ``curl_cffi`` module.

    Raises
    ------
    ModuleNotFoundError
        If the library is not installed. The message gives the
        ``pip install`` line.
    ModuleUpgradeRequiredError
        If the installed library is older than this package supports.
    """
    curl_cffi = import_optional(
        "curl_cffi",
        min_version=_CURL_CFFI_MIN_VERSION,
        install_message=_CURL_CFFI_INSTALL_MESSAGE,
    )
    # Some versions do not import their requests submodule with the package.
    import_optional("curl_cffi.requests", install_message=_CURL_CFFI_INSTALL_MESSAGE)
    return curl_cffi


@contextmanager
def _curl_open(
    url: str, *, headers: Mapping[str, str], timeout: float, curl_cffi: Any
) -> Iterator[_Response]:
    """Send one GET request with ``curl_cffi`` and yield the response.

    The request copies Chrome's TLS fingerprint. A 3xx, 4xx, or 5xx
    response is yielded like any other, so its status can be handled by the
    shared code.

    Parameters
    ----------
    url : str
        The ``http`` or ``https`` URL to request.
    headers : Mapping[str, str]
        The request headers.
    timeout : float
        Seconds to wait for the server before the request fails.
    curl_cffi : Any
        The ``curl_cffi`` module, from `_import_curl_cffi`.

    Yields
    ------
    _Response
        The response, whatever its status. A redirect is not followed.

    Raises
    ------
    DownloadError
        If the server's TLS certificate cannot be verified.
    _TransientError
        If no complete response arrives.
    """
    try:
        # The whole body is read before returning. The files this package
        # downloads are a few megabytes at most, and reading in one call
        # avoids streaming, whose behavior differs across curl_cffi versions.
        raw = curl_cffi.requests.get(
            url,
            headers=dict(headers),
            timeout=timeout,
            allow_redirects=False,
            impersonate="chrome",
        )
    except curl_cffi.CurlError as error:
        if getattr(error, "code", None) == _CURL_CERTIFICATE_ERROR:
            raise DownloadError(
                f"Could not download {url}: the server's certificate could not "
                f"be verified ({error})."
            ) from error
        raise _TransientError(f"{url} could not be reached: {error}") from error
    yield _Response(
        status=raw.status_code,
        headers={name.lower(): value for name, value in raw.headers.items()},
        chunks=iter([raw.content]),
    )
