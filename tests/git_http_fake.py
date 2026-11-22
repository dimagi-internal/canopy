"""A canopy-web git endpoint for tests: real `git http-backend` behind the auth rule.

    fake = GitHttpFake(tmp_path / "srv", token="tok").start()
    origin = fake.repo("connect", "helper")      # a bare repo served at fake.url("connect", "helper")
    ...
    fake.stop()

Serves ``<base>/git/<ws>/<slug>.git`` read-only (smart HTTP, upload-pack only), and answers
401 + ``WWW-Authenticate: Basic realm="canopy"`` unless the request carries
``Authorization: Bearer <token>`` or Basic with the token as the password — the contract in
STORED-AGENTS-GIT.md § Reading over git. Every request's path and Authorization header is
recorded in ``.seen`` so a test can prove what went over the wire.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def git(cwd, *args, env=None) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, **(env or {})}, timeout=60)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def http_backend() -> str:
    exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True).stdout.strip()
    p = Path(exec_path) / "git-http-backend"
    return str(p) if p.exists() else ""


class GitHttpFake:
    def __init__(self, root: Path, token: str = "tok"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.seen: list = []
        self._srv = None

    # ── the repos ────────────────────────────────────────────────────────────────────
    def repo(self, ws: str, slug: str) -> Path:
        bare = self.root / ws / f"{slug}.git"
        if not bare.exists():
            bare.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        return bare

    def url(self, ws: str, slug: str) -> str:
        return f"{self.base}/git/{ws}/{slug}.git"

    def publish(self, ws: str, slug: str, files: dict, revision: int, *,
                ref: str = "main", message: str = "") -> str:
        """Commit ``files`` ({path: (bytes, executable)}) as the next commit on ``ref`` (the
        whole tree, as canopy-web writes it) and tag it ``r<revision>`` when ref is main.
        Returns the commit sha."""
        bare = self.repo(ws, slug)
        work = self.root / f".work-{ws}-{slug}"
        if work.exists():
            shutil.rmtree(work)
        subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True,
                       capture_output=True)
        git(work, "checkout", "-q", "-B", ref)
        if git(work, "ls-remote", "origin", f"refs/heads/{ref}"):
            git(work, "reset", "-q", "--hard", f"origin/{ref}")
        for p in git(work, "ls-files").splitlines():
            (work / p).unlink()
        for path, (content, executable) in files.items():
            f = work / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(content)
            f.chmod(0o755 if executable else 0o644)
        git(work, "add", "-A")
        who = {"GIT_AUTHOR_NAME": "canopy", "GIT_AUTHOR_EMAIL": "agents@canopy.dimagi.com",
               "GIT_COMMITTER_NAME": "canopy", "GIT_COMMITTER_EMAIL": "agents@canopy.dimagi.com"}
        git(work, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m",
            message or f"revision {revision}", env=who)
        sha = git(work, "rev-parse", "HEAD")
        git(work, "push", "-q", "origin", f"HEAD:refs/heads/{ref}")
        if ref == "main":
            git(bare, "tag", f"r{revision}", sha)
        shutil.rmtree(work)
        return sha

    # ── the server ───────────────────────────────────────────────────────────────────
    def start(self) -> "GitHttpFake":
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _authorized(self) -> bool:
                auth = self.headers.get("Authorization") or ""
                if auth == f"Bearer {fake.token}":
                    return True
                if auth.startswith("Basic "):
                    try:
                        _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                    except Exception:  # noqa: BLE001
                        return False
                    return pw == fake.token
                return False

            def _serve(self, method: str) -> None:
                fake.seen.append({"method": method, "path": self.path,
                                  "auth": self.headers.get("Authorization") or ""})
                path, _, query = self.path.partition("?")
                if not path.startswith("/git/"):
                    self.send_error(404)
                    return
                if "git-receive-pack" in self.path:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"read-only: use canopy agent definition edit\n")
                    return
                if not self._authorized():
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="canopy"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                env = {**os.environ, "GIT_PROJECT_ROOT": str(fake.root),
                       "GIT_HTTP_EXPORT_ALL": "1", "REQUEST_METHOD": method,
                       "PATH_INFO": path[len("/git"):], "QUERY_STRING": query,
                       "CONTENT_TYPE": self.headers.get("Content-Type") or "",
                       "CONTENT_LENGTH": str(len(body)),
                       "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding") or "",
                       "GIT_PROTOCOL": self.headers.get("Git-Protocol") or ""}
                r = subprocess.run([http_backend()], input=body, capture_output=True, env=env,
                                   timeout=60)
                head, _, payload = r.stdout.partition(b"\r\n\r\n")
                status = 200
                hdrs = []
                for line in head.decode("latin-1").split("\r\n"):
                    k, _, v = line.partition(":")
                    if k.lower() == "status":
                        status = int(v.strip().split()[0])
                    elif k:
                        hdrs.append((k, v.strip()))
                self.send_response(status)
                for k, v in hdrs:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
