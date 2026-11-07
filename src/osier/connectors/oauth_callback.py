"""A tiny local HTTP listener for OAuth redirects (local development).

The provider redirects the person's browser to http://127.0.0.1:<port>/oauth/callback
with ``state`` and ``code``. A hosted osier serves the same path on its public URL.
"""

from __future__ import annotations

import html
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CALLBACK_PATH = "/oauth/callback"

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>osier</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem}}
</style></head><body><h1>{title}</h1><p>{message}</p></body></html>"""


def redirect_uri(port: int, host: str = "127.0.0.1") -> str:
    # Default: a loopback IP literal, as recommended for native apps (RFC 8252). Some
    # providers only accept "localhost"; the listener then serves IPv4 and IPv6.
    return f"http://{host}:{port}{CALLBACK_PATH}"


class CallbackServer:
    """Calls ``on_callback(state, code, error)``; it returns (ok, message)."""

    def __init__(
        self, port: int, on_callback: Callable[[str, str | None, str | None], tuple[bool, str]]
    ) -> None:
        self.port = port
        self._on_callback = on_callback
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> None:
        callback = self._on_callback

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                parsed = urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_error(404)
                    return
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                state = query.get("state")
                if not state:
                    ok, message = False, "Missing state."
                else:
                    try:
                        ok, message = callback(state, query.get("code"), query.get("error"))
                    except Exception:  # noqa: BLE001 - never show internals to the browser
                        ok, message = False, "Something went wrong completing the sign-in."
                body = _PAGE.format(
                    title="Connected" if ok else "Not connected",
                    message=html.escape(message),
                ).encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass  # never log query strings (they contain codes)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        # "localhost" may resolve to ::1 in the browser; listen there too when possible.
        try:

            class V6Server(ThreadingHTTPServer):
                address_family = socket.AF_INET6

            self._server_v6 = V6Server(("::1", self.port), Handler)
            threading.Thread(target=self._server_v6.serve_forever, daemon=True).start()
        except OSError:
            self._server_v6 = None

    def stop(self) -> None:
        for server in (self._server, getattr(self, "_server_v6", None)):
            if server is not None:
                server.shutdown()
                server.server_close()
        self._server = None
        self._server_v6 = None
