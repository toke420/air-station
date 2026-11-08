"""Local preview: serves web/ and /art, proxies /api to a running API.

    python3 tests/devserver.py --api http://127.0.0.1:18080 --art data/art --port 8090
"""
import argparse
import http.server
import os
import urllib.error
import urllib.request
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".svg": "image/svg+xml"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def make_handler(api, art):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(WEB), **kw)

        def log_message(self, *args):
            pass

        def _proxy(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() in ("content-type", "cookie", "x-requested-with", "if-none-match")}
            headers["X-Forwarded-For"] = self.client_address[0]
            req = urllib.request.Request(api + self.path, data=body, method=method, headers=headers)
            try:
                resp = OPENER.open(req, timeout=10)
                status, data, rh = resp.status, resp.read(), resp.headers
            except urllib.error.HTTPError as err:
                status, data, rh = err.code, err.read(), err.headers
            self.send_response(status)
            for k in ("Content-Type", "Set-Cookie", "Cache-Control", "Location", "ETag"):
                for v in rh.get_all(k) or []:
                    self.send_header(k, v.replace("; Secure", ""))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/api/"):
                return self._proxy("GET")
            if self.path.startswith("/art/") or self.path.startswith("/branding/"):
                folder = art if self.path.startswith("/art/") else str(Path(art).parent / "branding")
                path = Path(folder) / os.path.basename(self.path.split("?")[0])
                if path.is_file():
                    data = path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", TYPES.get(path.suffix, "application/octet-stream"))
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_error(404)
                return
            if self.path.startswith("/radio"):
                self.send_error(404)
                return
            return super().do_GET()

        def do_POST(self):
            self._proxy("POST")

        def do_PUT(self):
            self._proxy("PUT")

        def do_PATCH(self):
            self._proxy("PATCH")

        def do_DELETE(self):
            self._proxy("DELETE")
    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:18080")
    parser.add_argument("--art", default="data/art")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    http.server.ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.api, args.art)).serve_forever()
