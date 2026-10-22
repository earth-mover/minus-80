import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import obstore
import pytest

from minus80.core.registry import store_and_key

DATA = bytes(range(256)) * 4
TOKEN = "token=abc&dl=1"


class _TokenHandler(BaseHTTPRequestHandler):
    """Serves DATA at /f.bin only when the request carries TOKEN, like a signed link."""

    def _authorized(self):
        if self.path != f"/f.bin?{TOKEN}":
            self.send_error(404)
            return False
        return True

    def do_HEAD(self):
        if self._authorized():
            self.send_response(200)
            self.send_header("Content-Length", str(len(DATA)))
            self.end_headers()

    def do_GET(self):
        if not self._authorized():
            return
        start, end = 0, len(DATA) - 1
        if rng := self.headers.get("Range"):
            lo, hi = rng.removeprefix("bytes=").split("-")
            start, end = int(lo), int(hi)
        body = DATA[start:end + 1]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(body)))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _TokenHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_query_string_reaches_the_server(server):
    store, key = store_and_key(f"{server}/f.bin?{TOKEN}")
    assert obstore.head(store, key)["size"] == len(DATA)
    assert bytes(obstore.get_range(store, key, start=10, end=20)) == DATA[10:20]
