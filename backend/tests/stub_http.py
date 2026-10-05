"""A loopback HTTP stub for the RPC transport's edge cases: replies a real node won't produce on
demand (oversized bodies, malformed JSON, a 500). Behaviour against a real node is tested on
regtest (ENGINEERING §3.1: the node itself isn't mocked)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Reply = tuple[int, bytes]


@dataclass
class Recorded:
    headers: dict[str, str]
    body: Any


@dataclass
class Stub:
    port: int
    requests: list[Recorded] = field(default_factory=list)


def jsonrpc_result(result: Any, request_id: int = 1) -> Reply:
    return 200, json.dumps({"jsonrpc": "2.0", "result": result, "id": request_id}).encode()


@contextmanager
def serve(respond: Callable[[Any], Reply]) -> Iterator[Stub]:
    """Serve on 127.0.0.1 with a random port; `respond` maps the decoded request body to a reply."""
    stub = Stub(port=0)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw)
            with lock:
                stub.requests.append(Recorded(dict(self.headers.items()), body))
            status, payload = respond(body)
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # keep test output clean

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    stub.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
