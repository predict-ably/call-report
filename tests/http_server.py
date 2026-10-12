"""A local HTTP server that stands in for a download source in tests.

`LocalHTTPServer` runs a `http.server.ThreadingHTTPServer` on
``127.0.0.1``. It serves the files in a folder, and a test can tell it to
answer a path with a redirect or with a given sequence of responses. Every
request is recorded, so a test can check what was asked for and which
headers were sent. Nothing leaves the machine, so the default suite never
reaches the internet.

The ``http_server`` fixture in ``tests/conftest.py`` starts one for a test
and stops it afterwards.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_CONTENT_TYPES = {
    ".zip": "application/zip",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
}


@dataclass(frozen=True, kw_only=True)
class ScriptedResponse:
    """One response the server gives in place of serving a file.

    Attributes
    ----------
    status : int
        The HTTP status code.
    body : bytes
        The response body.
    headers : Mapping[str, str]
        Extra response headers.
    delay : float
        Seconds to wait before answering, to make a client time out.
    truncate : bool
        If true, the ``Content-Length`` header promises more bytes than the
        body holds, and the connection closes early.
    stall : float
        If set with `truncate`, seconds to wait after the short body before
        closing, to make a client time out while reading.
    """

    status: int = 200
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    delay: float = 0.0
    truncate: bool = False
    stall: float = 0.0


@dataclass(frozen=True, kw_only=True)
class RecordedRequest:
    """One request the server received.

    Attributes
    ----------
    path : str
        The request path, such as ``"/files/2026March.zip"``.
    headers : dict[str, str]
        The request headers, with lower case names.
    """

    path: str
    headers: dict[str, str]


class LocalHTTPServer:
    """A threaded HTTP server on ``127.0.0.1`` with scripted responses.

    Parameters
    ----------
    root : pathlib.Path
        The folder whose files are served. A request for ``/a/b.zip`` is
        answered with ``root / "a" / "b.zip"``, or a 404 if it is missing.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.requests: list[RecordedRequest] = []
        self._scripts: dict[str, list[ScriptedResponse]] = {}
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        """Return the server's root URL, ending in ``/``."""
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}/"

    def url(self, path: str) -> str:
        """Return the full URL of a path on this server."""
        return self.base_url + path.lstrip("/")

    def respond(self, path: str, *responses: ScriptedResponse) -> None:
        """Answer the next requests for `path` with `responses`, in order.

        Once they are used up, `path` is served from `root` as usual.
        """
        with self._lock:
            self._scripts.setdefault("/" + path.lstrip("/"), []).extend(responses)

    def redirect(self, path: str, *, to: str, status: int = 302) -> None:
        """Answer the next request for `path` with a redirect to `to`."""
        self.respond(path, ScriptedResponse(status=status, headers={"Location": to}))

    def paths_requested(self) -> list[str]:
        """Return the path of every request received, in order."""
        with self._lock:
            return [request.path for request in self.requests]

    def start(self) -> None:
        """Start answering requests in a background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop the server and close its socket."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def _next_script(self, path: str) -> ScriptedResponse | None:
        """Pop the next scripted response for `path`, if there is one."""
        with self._lock:
            queue = self._scripts.get(path)
            return queue.pop(0) if queue else None

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        """Return a request handler class bound to this server."""
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with server._lock:
                    server.requests.append(
                        RecordedRequest(
                            path=self.path,
                            headers={k.lower(): v for k, v in self.headers.items()},
                        )
                    )
                scripted = server._next_script(self.path)
                if scripted is not None:
                    self._send_scripted(scripted)
                    return
                target = server.root / self.path.lstrip("/")
                if not target.is_file():
                    self._send(404, b"not found", {"Content-Type": "text/plain"})
                    return
                content_type = _CONTENT_TYPES.get(
                    target.suffix.lower(), "application/octet-stream"
                )
                self._send(200, target.read_bytes(), {"Content-Type": content_type})

            def _send_scripted(self, scripted: ScriptedResponse) -> None:
                if scripted.delay:
                    time.sleep(scripted.delay)
                if scripted.truncate:
                    self.send_response(scripted.status)
                    self.send_header("Content-Length", str(len(scripted.body) + 100))
                    self.end_headers()
                    self.wfile.write(scripted.body)
                    self.wfile.flush()
                    time.sleep(scripted.stall)
                    self.close_connection = True
                    return
                self._send(scripted.status, scripted.body, dict(scripted.headers))

            def _send(
                self, status: int, body: bytes, headers: Mapping[str, str]
            ) -> None:
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                """Keep the test output free of access log lines."""

            def handle(self) -> None:
                # A client that times out closes its end while the handler is
                # still writing. That is the point of the test, not a fault.
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    super().handle()

        return Handler
