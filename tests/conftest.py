import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class RangeServer:
    """A local HTTP server for range reads, recording every request it answers.

    ``files`` maps a request path, query string included, to the bytes served
    there; any other path is a 404. ``requests`` holds ``(method, path, range)``
    per request. ``short`` drops that many bytes from the end of every ranged
    response, to stand in for a server that answers short.
    """

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, str | None]] = []
        self.short = 0
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
            start, end = 0, len(data) - 1
            if rng := self.headers.get("Range"):
                lo, hi = rng.removeprefix("bytes=").split("-")
                start, end = int(lo), min(int(hi), len(data) - 1)
            body = data[start:end + 1]
            if rng and server.short:
                body = body[:-server.short]
            self.send_response(206 if rng else 200)
            self.send_header("Content-Length", str(len(body)))
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
    httpd.shutdown()
