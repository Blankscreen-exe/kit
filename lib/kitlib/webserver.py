"""Shared behaviour for kit's local HTTP servers, so large responses arrive whole.

Python's http.server defaults to HTTP/1.0, where a response ends when the server closes the
connection. On Windows that close can throw away data still in flight: 11 of 20 100 MB downloads
through `kit serve` arrived a few hundred bytes short, after a ~19 second stall and with no error.

Two things fix it, and this mixin does both:

* **HTTP/1.1**, so a response ends at its Content-Length instead of at a socket close.
* **Never closing first.** Even on HTTP/1.1, a client that asks for `Connection: close` (wget,
  urllib, `curl --http1.0`) makes the server close, and the same truncation comes back. When the
  response carries a Content-Length the client can tell where the body ends by itself, so kit keeps
  the connection open and lets the client hang up. `timeout` ends connections that go quiet.

A response without a Content-Length still has to be closed to mark its end, so those are left alone
(they are streams, e.g. the hub's live job output, which say `Connection: close` themselves).

Usage:

    class Handler(KitHandler, BaseHTTPRequestHandler):  # KitHandler first
        ...
"""

from __future__ import annotations


class KitHandler:
    """Mix in before BaseHTTPRequestHandler (or SimpleHTTPRequestHandler)."""

    protocol_version = "HTTP/1.1"
    timeout = 30  # seconds an idle connection is held open before the thread is freed

    def send_header(self, keyword: str, value: str) -> None:
        if keyword.lower() == "content-length":
            self._kit_sent_length = True
        super().send_header(keyword, value)  # type: ignore[misc]

    def handle_one_request(self) -> None:
        self._kit_sent_length = False
        super().handle_one_request()  # type: ignore[misc]
        if self.close_connection and getattr(self, "_kit_sent_length", False):
            self.close_connection = False  # the client knows where the body ends; let it close
