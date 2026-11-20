import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class RangeServer:
    """A local HTTP server for range reads, recording every request it answers.

    ``files`` maps a request path, query string included, to the bytes served
    there; any other path is a 404. ``requests`` holds ``(method, path, range)``
    per request. ``short`` drops that many bytes from the end of every ranged
    response, to stand in for a server that answers short. A GET whose range
    starts at or past ``stall_from`` gets no answer until the test ends, to stand
    in for a server that has stopped responding. The first GET of a range that
    starts at an offset in ``stall_once`` gets no answer either, while later
    GETs of it are answered: a server that leaves one request hanging.
    ``etags`` maps a path to the ETag the server sends for it; a GET whose
    ``If-Match`` names another ETag is refused with 412, as S3 refuses it.
    """

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self.requests: list[tuple[str, str, str | None]] = []
        self.short = 0
        self.stall_from: int | None = None
        self.stall_once: set[int] = set()
        self.released = threading.Event()
        self.url = ""

    def gets(self) -> list[str | None]:
        """The ``Range`` header of each GET, in arrival order."""
        return [r for method, _, r in self.requests if method == "GET"]


def _handler(server: RangeServer):
    class Handler(BaseHTTPRequestHandler):
        def _body(self):
            server.requests.append((self.command, self.path, self.headers.get("Range")))
            if self.path not in server.files:
                self.send_error(404)
                return None
            return server.files[self.path]

        def do_HEAD(self):
            data = self._body()
            if data is not None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()

        def do_GET(self):
            data = self._body()
            if data is None:
                return
            etag = server.etags.get(self.path)
            wanted = self.headers.get("If-Match")
            # Icechunk sends the ETag without its quotes, which S3 accepts.
            if wanted is not None and wanted.strip('"') != (etag or "").strip('"'):
                self.send_error(412)
                return
            start, end = 0, len(data) - 1
            if rng := self.headers.get("Range"):
                lo, hi = rng.removeprefix("bytes=").split("-")
                start, end = int(lo), min(int(hi), len(data) - 1)
                if server.stall_from is not None and start >= server.stall_from:
                    server.released.wait()
                    return
                if start in server.stall_once:
                    server.stall_once.discard(start)
                    server.released.wait()
                    return
            body = data[start:end + 1]
            if rng and server.short:
                body = body[:-server.short]
            self.send_response(206 if rng else 200)
            self.send_header("Content-Length", str(len(body)))
            if etag is not None:
                self.send_header("ETag", etag)
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


@pytest.fixture
def range_server():
    server = RangeServer()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(server))
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    server.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield server
    server.released.set()
    httpd.shutdown()
