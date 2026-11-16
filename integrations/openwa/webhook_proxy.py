"""Expose only the signed OpenWA webhook route through a local tunnel."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8765
FASTAPI_WEBHOOK = "http://127.0.0.1:8000/webhooks/openwa"
MAX_BODY_BYTES = 1024 * 1024
FORWARDED_HEADERS = (
    "Content-Type",
    "X-OpenWA-Signature",
    "X-OpenWA-Idempotency-Key",
    "X-OpenWA-Event",
    "X-OpenWA-Delivery-Id",
    "X-OpenWA-Retry-Count",
)


class WebhookOnlyProxy(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhooks/openwa":
            self.send_error(404)
            return
        if self.headers.get_content_type() != "application/json":
            self.send_error(415)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            self.send_error(400)
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self.send_error(413)
            return

        body = self.rfile.read(length)
        headers = {name: self.headers[name] for name in FORWARDED_HEADERS if self.headers.get(name)}
        req = Request(FASTAPI_WEBHOOK, data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=15) as response:
                self._reply(response.status, response.read(), response.headers.get("Content-Type", "application/json"))
        except HTTPError as error:
            self._reply(error.code, error.read(), error.headers.get("Content-Type", "application/json"))
        except (URLError, TimeoutError):
            self._reply(502, b'{"detail":"Webhook receiver unavailable."}', "application/json")

    def do_GET(self):
        self.send_error(404)

    def do_PUT(self):
        self.send_error(404)

    def do_DELETE(self):
        self.send_error(404)

    def do_PATCH(self):
        self.send_error(404)

    def _reply(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), WebhookOnlyProxy)
    print(f"Webhook-only proxy listening on http://{LISTEN_HOST}:{LISTEN_PORT}")
    server.serve_forever()
