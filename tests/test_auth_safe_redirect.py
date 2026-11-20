"""A bearer token never follows a redirect off its origin (orchestrator/provenance.py)."""
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from orchestrator import canopy_web  # noqa: F401  (importing it installs the handler)
from orchestrator.provenance import AuthSafeRedirectHandler


def _server(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def servers():
    seen = []

    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                         "cookie": self.headers.get("Cookie")})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    sink = _server(Sink)
    other = f"http://127.0.0.1:{sink.server_port}"

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/same":
                target = "/landed"
            else:
                target = other + "/landed"
            if self.path == "/landed":
                Sink.do_GET(self)
                return
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

        def log_message(self, *a):
            pass

    redirector = _server(Redirector)
    yield f"http://127.0.0.1:{redirector.server_port}", seen
    sink.shutdown()
    redirector.shutdown()


def _get(url):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer t0k", "Cookie": "s=1"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read()


def test_the_default_opener_is_the_auth_safe_one():
    handlers = [type(h) for h in urllib.request._opener.handlers]
    assert AuthSafeRedirectHandler in handlers


def test_a_cross_origin_redirect_drops_the_bearer_and_cookies(servers):
    base, seen = servers
    assert _get(base + "/elsewhere") == b"ok"
    assert seen == [{"path": "/landed", "auth": None, "cookie": None}]


def test_a_same_origin_redirect_keeps_the_bearer(servers):
    base, seen = servers
    assert _get(base + "/same") == b"ok"
    assert seen == [{"path": "/landed", "auth": "Bearer t0k", "cookie": "s=1"}]
