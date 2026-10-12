"""Tests for the shared HTTP helpers in ``call_report.core._http``.

Every request goes to the local server from the ``http_server`` fixture or
to a replaced opener, so none of these tests reaches the internet. Tests
that run against the server are parametrized over both HTTP clients. The
``curl_cffi`` client runs through a stub module placed in `sys.modules`, so
it is covered whether or not the library is installed.
"""

from __future__ import annotations

import email.utils
import http.client
import socket
import ssl
import sys
import tomllib
import types
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from call_report.core import _http
from call_report.core._dependencies import ModuleUpgradeRequiredError
from call_report.core._http import (
    USER_AGENT,
    HttpClient,
    _parse_retry_after,
    _Response,
    download_file,
    fetch_bytes,
)
from call_report.exceptions import DownloadError, InsecureConnectionWarning
from tests.http_server import LocalHTTPServer, ScriptedResponse

ZIP_BYTES = b"PK\x05\x06" + b"\x00" * 18
"""bytes: The smallest valid zip file, an empty one."""


class _StubCurlError(Exception):
    """Stands in for ``curl_cffi.CurlError``, which carries curl's error code."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _stub_curl_cffi(calls: list[dict[str, Any]]) -> types.ModuleType:
    """Build a stand-in for the ``curl_cffi`` module.

    Its ``requests.get`` records its keyword arguments in `calls` and sends
    the request with `urllib`, without following redirects. A failure to
    get a whole response raises the stub's ``CurlError``, as the real
    library does.
    """
    opener = urllib.request.build_opener(_NoRedirect)

    def get(url: str, **kwargs: Any) -> types.SimpleNamespace:
        calls.append({"url": url, **kwargs})
        request = urllib.request.Request(url, headers=kwargs["headers"])  # noqa: S310
        try:
            raw: Any = opener.open(request, timeout=kwargs["timeout"])
        except urllib.error.HTTPError as error:
            raw = error
        except (OSError, http.client.HTTPException) as error:
            raise _StubCurlError(str(error)) from error
        with raw:
            try:
                content = raw.read()
            except (OSError, http.client.HTTPException) as error:
                raise _StubCurlError(str(error)) from error
            return types.SimpleNamespace(
                status_code=raw.status, headers=raw.headers, content=content
            )

    module = types.ModuleType("curl_cffi")
    module.__version__ = "0.16.3"  # type: ignore[attr-defined]
    module.CurlError = _StubCurlError  # type: ignore[attr-defined]
    module.requests = types.ModuleType("curl_cffi.requests")  # type: ignore[attr-defined]
    module.requests.get = get
    return module


@pytest.fixture
def curl_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Install the stub ``curl_cffi`` module and return its record of calls."""
    calls: list[dict[str, Any]] = []
    stub = _stub_curl_cffi(calls)
    monkeypatch.setitem(sys.modules, "curl_cffi", stub)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", stub.requests)
    return calls


@pytest.fixture(params=["urllib", "curl_cffi"])
def client(request: pytest.FixtureRequest) -> HttpClient:
    """Run the requesting test once per HTTP client.

    For ``curl_cffi``, the stub module is installed for the whole test.
    """
    if request.param == "curl_cffi":
        request.getfixturevalue("curl_calls")
    return request.param


def _fake_opener(responses: Mapping[str, _Response]) -> Any:
    """Return an opener that answers from `responses` and sends nothing."""

    @contextmanager
    def opener(url: str, **kwargs: Any) -> Iterator[_Response]:
        yield responses[url]

    return opener


def _ok(body: bytes = b"ok", **headers: str) -> _Response:
    """Return a 200 response for the fake opener."""
    return _Response(status=200, headers=headers, chunks=iter([body]))


def _closed_port() -> int:
    """Return a port on 127.0.0.1 that nothing is listening on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ---------------------------------------------------------------------------
# Successful requests
# ---------------------------------------------------------------------------


def test_fetch_bytes_returns_the_body(
    http_server: LocalHTTPServer, client: HttpClient
) -> None:
    """A 200 response's body is returned whole."""
    (http_server.root / "data.json").write_bytes(b'{"a": 1}')
    assert fetch_bytes(http_server.url("data.json"), http_client=client) == b'{"a": 1}'


def test_download_file_writes_the_body(
    http_server: LocalHTTPServer, client: HttpClient, tmp_path: Path
) -> None:
    """download_file writes the body to the destination and returns its path."""
    (http_server.root / "2026March.zip").write_bytes(ZIP_BYTES)
    destination = tmp_path / "out.zip"
    result = download_file(
        http_server.url("2026March.zip"), destination=destination, http_client=client
    )
    assert result == destination
    assert destination.read_bytes() == ZIP_BYTES
    assert sorted(path.name for path in tmp_path.iterdir()) == ["out.zip", "served"]


def test_download_file_replaces_an_existing_file(
    http_server: LocalHTTPServer, tmp_path: Path
) -> None:
    """A completed download replaces a file already at the destination."""
    (http_server.root / "a.zip").write_bytes(ZIP_BYTES)
    destination = tmp_path / "a.zip"
    destination.write_bytes(b"old")
    download_file(http_server.url("a.zip"), destination=destination)
    assert destination.read_bytes() == ZIP_BYTES


def test_every_urllib_request_sends_the_package_user_agent(
    http_server: LocalHTTPServer,
) -> None:
    """Every urllib request names this package in its User-Agent.

    FCA's Cloudflare refuses Python's default User-Agent, so a request
    without this header fails.
    """
    (http_server.root / "a.txt").write_bytes(b"a")
    fetch_bytes(http_server.url("a.txt"), headers={"X-Extra": "yes"})
    (request,) = http_server.requests
    assert request.headers["user-agent"] == USER_AGENT
    assert request.headers["x-extra"] == "yes"
    assert USER_AGENT.startswith("call-report/")


def test_a_caller_user_agent_replaces_the_package_one(
    http_server: LocalHTTPServer,
) -> None:
    """A User-Agent passed in headers is sent in place of the package's."""
    (http_server.root / "a.txt").write_bytes(b"a")
    fetch_bytes(http_server.url("a.txt"), headers={"User-Agent": "mine/1.0"})
    (request,) = http_server.requests
    assert request.headers["user-agent"] == "mine/1.0"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_followed(
    http_server: LocalHTTPServer, client: HttpClient, status: int
) -> None:
    """Each redirect status is followed to the file it points at."""
    (http_server.root / "real.zip").write_bytes(ZIP_BYTES)
    http_server.redirect("moved.zip", to="/real.zip", status=status)
    assert fetch_bytes(http_server.url("moved.zip"), http_client=client) == ZIP_BYTES
    assert http_server.paths_requested() == ["/moved.zip", "/real.zip"]


def test_a_chain_of_redirects_is_followed(http_server: LocalHTTPServer) -> None:
    """Relative and absolute redirects can follow one another."""
    (http_server.root / "c.txt").write_bytes(b"c")
    http_server.redirect("a.txt", to="b.txt")
    http_server.redirect("b.txt", to=http_server.url("c.txt"))
    assert fetch_bytes(http_server.url("a.txt")) == b"c"


def test_a_redirect_loop_raises(http_server: LocalHTTPServer) -> None:
    """A redirect chain longer than the limit raises instead of looping."""
    for _ in range(12):
        http_server.redirect("loop.txt", to="/loop.txt")
    with pytest.raises(DownloadError, match="redirected more than 10 times"):
        fetch_bytes(http_server.url("loop.txt"))


def test_a_redirect_to_another_scheme_raises(http_server: LocalHTTPServer) -> None:
    """A redirect to a URL that is not http or https is refused."""
    http_server.redirect("a.txt", to="file:///etc/passwd")
    with pytest.raises(DownloadError, match="Only http and https"):
        fetch_bytes(http_server.url("a.txt"))


def test_a_redirect_status_without_a_location_raises(
    http_server: LocalHTTPServer,
) -> None:
    """A 3xx response with no Location header is a failure, not a redirect."""
    http_server.respond("a.txt", ScriptedResponse(status=302))
    with pytest.raises(DownloadError, match="HTTP 302"):
        fetch_bytes(http_server.url("a.txt"))


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def test_a_503_is_retried(
    http_server: LocalHTTPServer, client: HttpClient, recorded_sleeps: list[float]
) -> None:
    """A 503 then a 200 succeeds after one retry and a one second backoff."""
    (http_server.root / "a.txt").write_bytes(b"a")
    http_server.respond("a.txt", ScriptedResponse(status=503))
    assert fetch_bytes(http_server.url("a.txt"), http_client=client) == b"a"
    assert recorded_sleeps == [1.0]
    assert http_server.paths_requested() == ["/a.txt", "/a.txt"]


def test_a_429_honors_retry_after(
    http_server: LocalHTTPServer, client: HttpClient, recorded_sleeps: list[float]
) -> None:
    """A 429 with ``Retry-After: 2`` waits two seconds before retrying."""
    (http_server.root / "a.txt").write_bytes(b"a")
    http_server.respond(
        "a.txt", ScriptedResponse(status=429, headers={"Retry-After": "2"})
    )
    assert fetch_bytes(http_server.url("a.txt"), http_client=client) == b"a"
    assert recorded_sleeps == [2.0]


def test_retry_after_is_ignored_on_a_500(
    http_server: LocalHTTPServer, recorded_sleeps: list[float]
) -> None:
    """Only a 429 or 503 sets the wait. A 500 backs off as usual."""
    (http_server.root / "a.txt").write_bytes(b"a")
    http_server.respond(
        "a.txt", ScriptedResponse(status=500, headers={"Retry-After": "30"})
    )
    fetch_bytes(http_server.url("a.txt"))
    assert recorded_sleeps == [1.0]


def test_retries_stop_at_max_retries(
    http_server: LocalHTTPServer, client: HttpClient, recorded_sleeps: list[float]
) -> None:
    """After max_retries retries, the last failure is raised as DownloadError."""
    http_server.respond("a.txt", *[ScriptedResponse(status=502)] * 3)
    with pytest.raises(DownloadError, match=r"after 3 attempt\(s\).*HTTP 502"):
        fetch_bytes(http_server.url("a.txt"), max_retries=2, http_client=client)
    assert recorded_sleeps == [1.0, 2.0]
    assert len(http_server.requests) == 3


def test_max_retries_zero_sends_one_request(
    http_server: LocalHTTPServer, recorded_sleeps: list[float]
) -> None:
    """With max_retries=0 a transient failure is raised at once."""
    http_server.respond("a.txt", ScriptedResponse(status=503))
    with pytest.raises(DownloadError, match=r"after 1 attempt\(s\)"):
        fetch_bytes(http_server.url("a.txt"), max_retries=0)
    assert recorded_sleeps == []


def test_a_404_is_not_retried(
    http_server: LocalHTTPServer, client: HttpClient, recorded_sleeps: list[float]
) -> None:
    """A 404 raises at once, naming the URL and the status."""
    url = http_server.url("missing.zip")
    with pytest.raises(DownloadError, match="HTTP 404") as error:
        fetch_bytes(url, http_client=client)
    assert url in str(error.value)
    assert recorded_sleeps == []
    assert len(http_server.requests) == 1


def test_a_refused_connection_is_retried(
    client: HttpClient, recorded_sleeps: list[float]
) -> None:
    """A connection refused is a transient failure, so it is retried."""
    url = f"http://127.0.0.1:{_closed_port()}/a.txt"
    with pytest.raises(DownloadError, match="could not be reached"):
        fetch_bytes(url, max_retries=1, http_client=client)
    assert recorded_sleeps == [1.0]


def test_a_timeout_is_retried(
    http_server: LocalHTTPServer, client: HttpClient, recorded_sleeps: list[float]
) -> None:
    """A server that does not answer in time is retried."""
    (http_server.root / "a.txt").write_bytes(b"a")
    http_server.respond("a.txt", ScriptedResponse(delay=1.0))
    assert fetch_bytes(http_server.url("a.txt"), timeout=0.2, http_client=client) == (
        b"a"
    )
    assert recorded_sleeps == [1.0]


def test_a_connection_lost_while_reading_is_retried(
    http_server: LocalHTTPServer, client: HttpClient, recorded_sleeps: list[float]
) -> None:
    """A body cut short is retried rather than returned incomplete."""
    (http_server.root / "a.txt").write_bytes(b"whole")
    http_server.respond("a.txt", ScriptedResponse(body=b"wh", truncate=True))
    assert fetch_bytes(http_server.url("a.txt"), http_client=client) == b"whole"
    assert recorded_sleeps == [1.0]


def test_a_body_that_stalls_is_retried(
    http_server: LocalHTTPServer, client: HttpClient, recorded_sleeps: list[float]
) -> None:
    """A body that stops arriving times out while reading, and is retried."""
    (http_server.root / "a.txt").write_bytes(b"whole")
    http_server.respond("a.txt", ScriptedResponse(body=b"wh", truncate=True, stall=1.0))
    assert fetch_bytes(http_server.url("a.txt"), timeout=0.2, http_client=client) == (
        b"whole"
    )
    assert recorded_sleeps == [1.0]


def test_an_unverifiable_certificate_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, recorded_sleeps: list[float]
) -> None:
    """A TLS certificate failure raises at once, since a retry fails the same way."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError(ssl.SSLCertVerificationError("self signed"))

    monkeypatch.setattr(_http._URLLIB_OPENER, "open", refuse)
    with pytest.raises(DownloadError, match="certificate could not be verified"):
        fetch_bytes("https://example.com/a.zip")
    assert recorded_sleeps == []


# ---------------------------------------------------------------------------
# Cloudflare
# ---------------------------------------------------------------------------


def test_cloudflare_error_1010_is_named_and_not_retried(
    http_server: LocalHTTPServer, client: HttpClient, recorded_sleeps: list[float]
) -> None:
    """A 403 whose body is ``error code: 1010`` names Cloudflare."""
    http_server.respond("a.zip", ScriptedResponse(status=403, body=b"error code: 1010"))
    with pytest.raises(DownloadError, match="Cloudflare") as error:
        fetch_bytes(http_server.url("a.zip"), http_client=client)
    assert "error code: 1010" in str(error.value)
    assert recorded_sleeps == []
    assert len(http_server.requests) == 1


def test_a_plain_403_does_not_name_cloudflare(http_server: LocalHTTPServer) -> None:
    """A 403 with any other body is an ordinary failure."""
    http_server.respond("a.zip", ScriptedResponse(status=403, body=b"Forbidden"))
    with pytest.raises(DownloadError, match="HTTP 403") as error:
        fetch_bytes(http_server.url("a.zip"))
    assert "Cloudflare" not in str(error.value)


def test_a_403_with_no_body_does_not_name_cloudflare(
    http_server: LocalHTTPServer,
) -> None:
    """A 403 with an empty body is an ordinary failure."""
    http_server.respond("a.zip", ScriptedResponse(status=403))
    with pytest.raises(DownloadError, match="HTTP 403"):
        fetch_bytes(http_server.url("a.zip"))


def test_a_web_page_where_a_zip_is_expected_names_cloudflare(
    http_server: LocalHTTPServer, client: HttpClient, recorded_sleeps: list[float]
) -> None:
    """An HTML page in answer to a ``.zip`` URL is a refusal, not a zip."""
    http_server.respond(
        "2026March.zip",
        ScriptedResponse(
            body=b"<html>Just a moment...</html>",
            headers={"Content-Type": "text/html; charset=UTF-8"},
        ),
    )
    with pytest.raises(DownloadError, match=r"Cloudflare.*web page"):
        fetch_bytes(http_server.url("2026March.zip"), http_client=client)
    assert recorded_sleeps == []


def test_a_web_page_is_fine_when_no_zip_is_expected(
    http_server: LocalHTTPServer,
) -> None:
    """An HTML page in answer to a page URL is returned as usual."""
    (http_server.root / "page.html").write_bytes(b"<html></html>")
    assert fetch_bytes(http_server.url("page.html")) == b"<html></html>"


def test_a_cf_mitigated_header_names_cloudflare(
    http_server: LocalHTTPServer, client: HttpClient, recorded_sleeps: list[float]
) -> None:
    """A ``cf-mitigated: challenge`` header is a Cloudflare challenge."""
    http_server.respond(
        "a.zip",
        ScriptedResponse(status=403, headers={"cf-mitigated": "challenge"}),
    )
    with pytest.raises(DownloadError, match=r"Cloudflare.*'challenge'"):
        fetch_bytes(http_server.url("a.zip"), http_client=client)
    assert recorded_sleeps == []


# ---------------------------------------------------------------------------
# URL checks and warnings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/a.zip"])
def test_a_url_that_is_not_http_raises_before_any_request(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """Only http and https URLs are requested."""

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("no request should be sent")

    monkeypatch.setattr(_http, "_urllib_open", fail)
    with pytest.raises(DownloadError, match="Only http and https"):
        fetch_bytes(url)


def test_plain_http_to_another_host_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain http URL to a host that is not this machine warns, then proceeds."""
    opener = _fake_opener({"http://example.com/a.zip": _ok()})
    monkeypatch.setattr(_http, "_urllib_open", opener)
    with pytest.warns(InsecureConnectionWarning, match="plain HTTP") as record:
        assert fetch_bytes("http://example.com/a.zip") == b"ok"
    # The warning points at the caller, not inside the helper.
    assert record[0].filename == __file__


def test_https_does_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """An https URL issues no warning (warnings are errors in this suite)."""
    opener = _fake_opener({"https://example.com/a.zip": _ok()})
    monkeypatch.setattr(_http, "_urllib_open", opener)
    assert fetch_bytes("https://example.com/a.zip") == b"ok"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_plain_http_to_this_machine_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """A loopback host is exempt, so a local server does not warn."""
    url = f"http://{host}:8000/a.zip"
    monkeypatch.setattr(_http, "_urllib_open", _fake_opener({url: _ok()}))
    assert fetch_bytes(url) == b"ok"


def test_a_redirect_to_plain_http_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirect from https to plain http warns too."""
    responses = {
        "https://example.com/a.zip": _Response(
            status=302,
            headers={"location": "http://example.com/b.zip"},
            chunks=iter([]),
        ),
        "http://example.com/b.zip": _ok(),
    }
    monkeypatch.setattr(_http, "_urllib_open", _fake_opener(responses))
    with pytest.warns(InsecureConnectionWarning, match="b.zip") as record:
        assert fetch_bytes("https://example.com/a.zip") == b"ok"
    # The warning points at the caller, not inside the helper.
    assert record[0].filename == __file__


# ---------------------------------------------------------------------------
# Failed downloads leave nothing behind
# ---------------------------------------------------------------------------


def test_a_failed_download_file_leaves_no_file(
    http_server: LocalHTTPServer, tmp_path: Path
) -> None:
    """A download that fails writes no file, partial or otherwise."""
    with pytest.raises(DownloadError):
        download_file(http_server.url("missing.zip"), destination=tmp_path / "a.zip")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["served"]


def test_a_download_cut_short_leaves_no_partial_file(
    http_server: LocalHTTPServer, tmp_path: Path, recorded_sleeps: list[float]
) -> None:
    """A body cut short on the last attempt leaves the old file as it was."""
    destination = tmp_path / "a.zip"
    destination.write_bytes(b"old")
    http_server.respond("a.zip", ScriptedResponse(body=b"partial", truncate=True))
    with pytest.raises(DownloadError, match="failed while reading"):
        download_file(http_server.url("a.zip"), destination=destination, max_retries=0)
    assert destination.read_bytes() == b"old"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a.zip", "served"]


# ---------------------------------------------------------------------------
# Choosing a client
# ---------------------------------------------------------------------------


def test_curl_cffi_impersonates_a_browser_and_does_not_follow_redirects(
    http_server: LocalHTTPServer, curl_calls: list[dict[str, Any]]
) -> None:
    """curl_cffi is asked to copy Chrome's TLS fingerprint and to leave redirects."""
    (http_server.root / "a.txt").write_bytes(b"a")
    fetch_bytes(http_server.url("a.txt"), timeout=5.0, http_client="curl_cffi")
    (call,) = curl_calls
    assert call["impersonate"] == "chrome"
    assert call["allow_redirects"] is False
    assert call["timeout"] == 5.0


def test_curl_cffi_leaves_the_user_agent_to_the_browser_it_copies(
    http_server: LocalHTTPServer, curl_calls: list[dict[str, Any]]
) -> None:
    """curl_cffi is not given the package's User-Agent.

    It sends the impersonated browser's own, so the header matches the TLS
    fingerprint. Extra headers are still passed on.
    """
    (http_server.root / "a.txt").write_bytes(b"a")
    fetch_bytes(
        http_server.url("a.txt"), headers={"X-Extra": "yes"}, http_client="curl_cffi"
    )
    (call,) = curl_calls
    assert call["headers"] == {"X-Extra": "yes"}


def test_curl_cffi_missing_gives_the_install_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the library, the error says which extra to install."""
    monkeypatch.setitem(sys.modules, "curl_cffi", None)
    with pytest.raises(ModuleNotFoundError, match=r"call-report\[curl_cffi\]"):
        fetch_bytes("https://example.com/a.zip", http_client="curl_cffi")


def test_curl_cffi_too_old_is_refused(
    monkeypatch: pytest.MonkeyPatch, curl_calls: list[dict[str, Any]]
) -> None:
    """A curl_cffi older than the supported floor raises before any request."""
    monkeypatch.setattr(sys.modules["curl_cffi"], "__version__", "0.6.1")
    with pytest.raises(ModuleUpgradeRequiredError, match=r"curl_cffi 0\.6\.2"):
        fetch_bytes("https://example.com/a.zip", http_client="curl_cffi")
    assert curl_calls == []


def test_an_unknown_client_raises() -> None:
    """An http_client that is not a known client raises ValueError."""
    with pytest.raises(ValueError, match="http_client"):
        fetch_bytes("https://example.com/a.zip", http_client="requests")  # type: ignore[arg-type]


def test_the_real_curl_cffi_downloads_from_the_local_server(
    http_server: LocalHTTPServer, recorded_sleeps: list[float]
) -> None:
    """The installed curl_cffi downloads, redirects, and retries as urllib does."""
    pytest.importorskip("curl_cffi")
    (http_server.root / "real.zip").write_bytes(ZIP_BYTES)
    # A retry starts again from the URL asked for, so it redirects twice.
    http_server.redirect("moved.zip", to="/real.zip")
    http_server.redirect("moved.zip", to="/real.zip")
    http_server.respond("real.zip", ScriptedResponse(status=503))
    assert fetch_bytes(http_server.url("moved.zip"), http_client="curl_cffi") == (
        ZIP_BYTES
    )
    assert recorded_sleeps == [1.0]
    assert http_server.paths_requested() == [
        "/moved.zip",
        "/real.zip",
        "/moved.zip",
        "/real.zip",
    ]
    user_agent = http_server.requests[0].headers["user-agent"]
    assert user_agent.startswith("Mozilla/5.0")
    assert "Chrome/" in user_agent


# ---------------------------------------------------------------------------
# Retry-After parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("2", 2.0),
        ("0.5", 0.5),
        ("-5", 0.0),
        ("3600", 60.0),
        ("soon", None),
    ],
)
def test_parse_retry_after_seconds(value: str | None, expected: float | None) -> None:
    """A number of seconds is read, kept non-negative, and capped at a minute."""
    assert _parse_retry_after(value) == expected


def test_parse_retry_after_an_http_date() -> None:
    """An HTTP date is turned into the seconds from now until then."""
    when = datetime.now(UTC) + timedelta(seconds=30)
    seconds = _parse_retry_after(email.utils.format_datetime(when, usegmt=True))
    assert seconds is not None
    assert 25 <= seconds <= 30


def test_parse_retry_after_a_date_without_a_time_zone() -> None:
    """A date with a ``-0000`` zone is read as UTC. A past date waits for nothing."""
    past = datetime(2000, 1, 1, 12, 0, 0)
    assert _parse_retry_after(email.utils.format_datetime(past)) == 0.0


def test_the_curl_cffi_floor_matches_pyproject() -> None:
    """The version checked at import time is the floor the extra declares."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    extras = tomllib.loads(pyproject.read_text())["project"]["optional-dependencies"]
    (requirement,) = extras["curl_cffi"]
    assert requirement == f"curl_cffi>={_http._CURL_CFFI_MIN_VERSION}"


# ---------------------------------------------------------------------------
# Review follow-ups: credentials on redirects, curl certificates, jitter
# ---------------------------------------------------------------------------


def test_a_redirect_to_another_host_drops_credentials(
    http_server: LocalHTTPServer, client: HttpClient
) -> None:
    """Authorization and Cookie are not sent on to a different host.

    ``localhost`` and ``127.0.0.1`` reach the same test server under two
    host names, so the server sees both requests. Other headers still go.
    """
    (http_server.root / "b.txt").write_bytes(b"b")
    other_host = http_server.url("b.txt").replace("127.0.0.1", "localhost")
    http_server.redirect("a.txt", to=other_host)
    fetch_bytes(
        http_server.url("a.txt"),
        headers={"Authorization": "token secret", "cookie": "a=1", "X-Keep": "1"},
        http_client=client,
    )
    first, second = http_server.requests
    assert first.headers["authorization"] == "token secret"
    assert first.headers["cookie"] == "a=1"
    assert "authorization" not in second.headers
    assert "cookie" not in second.headers
    assert second.headers["x-keep"] == "1"


def test_a_redirect_on_the_same_host_keeps_credentials(
    http_server: LocalHTTPServer,
) -> None:
    """A redirect that stays on the host keeps every header."""
    (http_server.root / "b.txt").write_bytes(b"b")
    http_server.redirect("a.txt", to="/b.txt")
    fetch_bytes(http_server.url("a.txt"), headers={"Authorization": "token secret"})
    _, second = http_server.requests
    assert second.headers["authorization"] == "token secret"


def test_curl_cffi_certificate_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    curl_calls: list[dict[str, Any]],
    recorded_sleeps: list[float],
) -> None:
    """A curl certificate error (60) raises at once, as urllib's does."""

    def refuse(url: str, **kwargs: Any) -> None:
        raise _StubCurlError("SSL certificate problem", code=60)

    monkeypatch.setattr(sys.modules["curl_cffi"].requests, "get", refuse)
    with pytest.raises(DownloadError, match="certificate could not be verified"):
        fetch_bytes("https://example.com/a.zip", http_client="curl_cffi")
    assert recorded_sleeps == []


def test_curl_cffi_other_errors_are_retried(
    monkeypatch: pytest.MonkeyPatch,
    curl_calls: list[dict[str, Any]],
    recorded_sleeps: list[float],
) -> None:
    """Any other curl error, such as a refused connection (7), is retried."""

    def refuse(url: str, **kwargs: Any) -> None:
        raise _StubCurlError("Failed to connect", code=7)

    monkeypatch.setattr(sys.modules["curl_cffi"].requests, "get", refuse)
    with pytest.raises(DownloadError, match=r"after 2 attempt\(s\)"):
        fetch_bytes("https://example.com/a.zip", max_retries=1, http_client="curl_cffi")
    assert recorded_sleeps == [1.0]


def test_backoff_adds_up_to_a_quarter_at_random(
    http_server: LocalHTTPServer,
    recorded_sleeps: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The jitter lengthens each backoff by up to 25 percent.

    Without it, several downloads that fail together would retry together.
    """
    monkeypatch.setattr(_http, "_jitter", lambda: 1.0)
    (http_server.root / "a.txt").write_bytes(b"a")
    http_server.respond(
        "a.txt", ScriptedResponse(status=503), ScriptedResponse(status=503)
    )
    fetch_bytes(http_server.url("a.txt"))
    assert recorded_sleeps == [1.25, 2.5]


def test_retry_after_is_not_jittered(
    http_server: LocalHTTPServer,
    recorded_sleeps: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server's requested wait is used exactly."""
    monkeypatch.setattr(_http, "_jitter", lambda: 1.0)
    (http_server.root / "a.txt").write_bytes(b"a")
    http_server.respond(
        "a.txt", ScriptedResponse(status=429, headers={"Retry-After": "3"})
    )
    fetch_bytes(http_server.url("a.txt"))
    assert recorded_sleeps == [3.0]
