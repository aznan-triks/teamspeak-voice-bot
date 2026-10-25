"""The bot's management site: `python -m tsbot.web [--config config.toml]`. Serves on [web] host:port."""
from __future__ import annotations

import argparse
import json
import logging
import os
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tsbot.__main__ import load_config

from .app import App, HttpError

log = logging.getLogger("tsbot.web")
STATIC = Path(__file__).parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}
COOKIE = "tsbot_session"


def route(app: App, method: str, path: str, query: dict, body: dict, user: dict | None, site_url: str = ""):
    """Returns (status, content type, bytes, cookie or None)."""
    one = lambda name, default="": query.get(name, [default])[0]

    def ok(data, cookie=None):
        return 200, "application/json", json.dumps(data, ensure_ascii=False).encode(), cookie

    # Open to everyone: logging in.
    if method == "GET" and path == "/api/info":
        return ok({"name": app.bot_name()})
    if method == "GET" and path == "/api/login/clients":
        return ok(app.login_clients())
    if method == "POST" and path == "/api/login/start":
        return ok(app.login_start(int(body.get("client_id", 0)), site_url))
    if method == "POST" and path == "/api/login/finish":
        session, cookie = app.login_finish(int(body.get("client_id", 0)), str(body.get("code", "")))
        return ok(session, cookie)
    if method == "POST" and path == "/api/logout":
        return ok({"bye": True}, "")
    if user is None:
        raise HttpError(401, "Connecte-toi d'abord.")

    # Every member.
    if method == "GET" and path == "/api/me":
        return ok(user)
    if method == "GET" and path == "/api/my/phrases":
        return ok(app.my_phrases(user["uid"], one("q"), int(one("before_id", "0")), int(one("limit", "50"))))
    if method == "GET" and path == "/api/conversation":
        return ok(app.conversation(user["uid"], int(one("after_id", "0"))))
    if method == "GET" and path == "/api/presence":
        return ok(app.presence(user["uid"]))
    if method == "GET" and path == "/api/actions":
        return ok(app.actions(user["uid"], user["admin"], int(one("before_id", "0")), int(one("limit", "50"))))
    if not user["admin"]:
        raise HttpError(403, "Réservé aux administrateurs.")

    # Admins only.
    if method == "GET" and path == "/api/state":
        return ok(app.bot("GET", "/state"))
    if method == "GET" and path == "/api/logs":
        return ok(app.bot("GET", f"/logs?after={int(one('after', '0'))}"))
    if method == "POST" and path == "/api/restart":
        return ok(app.bot("POST", "/restart"))
    if method == "POST" and path == "/api/say":
        by = user.get("identity") or user.get("nickname") or user["uid"]
        return ok(app.bot("POST", "/say", {"text": str(body.get("text", "")), "by": by}))
    if method == "GET" and path == "/api/voice":
        from urllib.parse import quote

        return 200, "audio/wav", app.bot("GET", f"/voice?text={quote(one('text'))}", raw=True), None
    if method == "GET" and path == "/api/settings":
        return ok(app.settings())
    if method == "POST" and path == "/api/settings":
        return ok(app.save_settings(body))
    if method == "POST" and path == "/api/plugins/test":
        return ok(app.test_plugin(str(body.get("name", ""))))
    if method == "GET" and path == "/api/services":
        return ok(app.service_list())
    if method == "POST" and path == "/api/services":
        return ok(app.service_action(str(body.get("name", "")), str(body.get("action", ""))))
    if method == "GET" and path == "/api/services/logs":
        return ok(app.service_logs(one("name"), int(one("lines", "200"))))
    if method == "GET" and path == "/api/stats":
        return ok(app.stats(int(one("days", "30"))))
    raise HttpError(404, "Page inconnue.")


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def _serve(self, method: str) -> None:
            url = urlparse(self.path)
            if method == "GET" and not url.path.startswith("/api/"):
                return self._static(url.path)
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
            user = app.session(cookie.value if cookie else None)
            try:
                body = {}
                if method == "POST":
                    # A JSON body cannot be sent by another site's form: this blocks cross-site requests.
                    if not self.headers.get("Content-Type", "").startswith("application/json"):
                        raise HttpError(415, "JSON attendu.")
                    size = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(size) or b"{}") if size else {}
                host = self.headers.get("Host", "")
                status, kind, data, new_cookie = route(app, method, url.path, parse_qs(url.query), body, user, f"http://{host}" if host else "")
            except HttpError as error:
                status, kind, data, new_cookie = error.status, "application/json", json.dumps({"error": str(error)}, ensure_ascii=False).encode(), None
            except Exception as error:
                log.exception("%s %s failed", method, url.path)
                status, kind, data, new_cookie = 500, "application/json", json.dumps({"error": f"Erreur interne : {error}"}, ensure_ascii=False).encode(), None
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if new_cookie is not None:
                max_age = app.web.get("session_days", 30) * 86400 if new_cookie else 0
                self.send_header("Set-Cookie", f"{COOKIE}={new_cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}")
            self.end_headers()
            self.wfile.write(data)

        def _static(self, path: str) -> None:
            file = (STATIC / (path.lstrip("/") or "index.html")).resolve()
            if not file.is_file() or STATIC.resolve() not in file.parents:
                file = STATIC / "index.html"  # the page handles its own sections
            data = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", TYPES.get(file.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def log_message(self, *_):
            pass

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(prog="tsbot.web")
    parser.add_argument("--config", default=os.environ.get("TSBOT_CONFIG", "config.toml"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    web = cfg.get("web", {})
    server = ThreadingHTTPServer((web.get("host", "0.0.0.0"), web.get("port", 8095)), make_handler(App(cfg, args.config)))
    log.info("management site on http://%s:%d", *server.server_address[:2])
    server.serve_forever()


if __name__ == "__main__":
    main()
