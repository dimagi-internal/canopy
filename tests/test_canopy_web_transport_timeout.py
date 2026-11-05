"""canopy_web.urllib_transport never hangs, and a dropped connection is a URLError naming the call.

Run: uv run pytest tests/test_canopy_web_transport_timeout.py
"""
from __future__ import annotations

import socket
import urllib.error
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from orchestrator import canopy_web


class _Server:
    def __init__(self, behaviour):
        hits = self.hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                hits.append(self.command)
                behaviour(self, len(hits))

            do_GET = do_POST = _go

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/x/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server():
    made = []

    def make(behaviour):
        s = _Server(behaviour)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def _ok(h, _n):
    h.send_response(200)
    h.end_headers()
    h.wfile.write(b'{"ok": true}')


def _hang(h, _n):
    time.sleep(3)


def test_answers_pass_through(server):
    s = server(_ok)
    assert canopy_web.urllib_transport("GET", s.url, {}, None) == (200, '{"ok": true}')


def test_http_errors_are_answers_not_exceptions(server):
    def _404(h, _n):
        h.send_response(404)
        h.end_headers()
        h.wfile.write(b"nope")
    s = server(_404)
    assert canopy_web.urllib_transport("GET", s.url, {}, None) == (404, "nope")


def test_a_silent_server_times_out(server, monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_TIMEOUT", "0.3")
    monkeypatch.setattr(canopy_web.time, "sleep", lambda s: None)
    s = server(_hang)
    started = time.monotonic()
    with pytest.raises(canopy_web.CanopyNetworkError):
        canopy_web.urllib_transport("POST", s.url, {}, b"{}")
    assert time.monotonic() - started < 2


def test_a_read_is_retried_once_and_a_write_never(server, monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_TIMEOUT", "0.3")
    monkeypatch.setattr(canopy_web.time, "sleep", lambda s: None)

    def first_hangs(h, n):
        if n == 1:
            time.sleep(3)
        else:
            _ok(h, n)
    s = server(first_hangs)
    assert canopy_web.urllib_transport("GET", s.url, {}, None)[0] == 200
    assert s.hits == ["GET", "GET"]

    w = server(first_hangs)
    with pytest.raises(canopy_web.CanopyNetworkError):
        canopy_web.urllib_transport("POST", w.url, {}, b"{}")
    assert w.hits == ["POST"]


def test_connection_refused_is_a_url_error(monkeypatch):
    monkeypatch.setattr(canopy_web.time, "sleep", lambda s: None)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(urllib.error.URLError, match="no answer"):
        canopy_web.urllib_transport("GET", f"http://127.0.0.1:{port}/api/x/", {}, None)


def test_timeout_scales_with_the_body(monkeypatch):
    monkeypatch.delenv("CANOPY_WEB_TIMEOUT", raising=False)
    assert canopy_web.request_timeout(None) == canopy_web.DEFAULT_TIMEOUT
    assert canopy_web.request_timeout(b"x" * 10_000_000) == canopy_web.DEFAULT_TIMEOUT + 100
