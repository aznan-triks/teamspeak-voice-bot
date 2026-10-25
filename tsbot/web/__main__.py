"""The bot's management site: `python -m tsbot.web [--config config.toml]`. Serves on [web] host:port."""
from __future__ import annotations

import argparse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tsbot.__main__ import load_config

from .app import App, HttpError, tr

log = logging.getLogger("tsbot.web")
STATIC = Path(__file__).parent / "static"
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}
COOKIE = "tsbot_session"


def route(
    app: App,
    method: str,
    path: str,
    query: dict,
    body: dict,
    user: dict | None,
    site_url: str = "",
    lang: str = "fr",
):
    """Returns (status, content type, bytes, cookie or None)."""
    one = lambda name, default="": query.get(name, [default])[0]
    raw_lang = one("lang") or lang or "fr"
    lang = raw_lang if raw_lang in app.available_languages() else raw_lang[:2].lower()
    if lang not in app.available_languages():
        lang = "fr"

    def ok(data, cookie=None):
        return 200, "application/json", json.dumps(data, ensure_ascii=False).encode(), cookie

    def err(status: int, key: str, **kwargs):
        return HttpError(status, key, lang=lang, **kwargs)

    # Open to everyone: logging in.
    if method == "GET" and path == "/api/info":
        return ok({"name": app.bot_name(), "languages": app.available_languages()})
    if method == "GET" and path == "/api/login/clients":
        return ok(app.login_clients())
    if method == "POST" and path == "/api/login/start":
        return ok(app.login_start(int(body.get("client_id", 0)), site_url, lang=lang))
    if method == "POST" and path == "/api/login/finish":
        session, cookie = app.login_finish(int(body.get("client_id", 0)), str(body.get("code", "")), lang=lang)
        return ok(session, cookie)
    if method == "POST" and path == "/api/logout":
        return ok({"bye": True}, "")
    if user is None:
        raise err(401, "error.login_required")

    # Every member.
    if method == "GET" and path == "/api/me":
        cfg = app.current_config()
        if user.get("uid"):
            cfg_id = cfg.get("identities", {}).get(f"uid:{user['uid']}")
            if cfg_id is not None:
                user["identity"] = cfg_id
            user["opt_out"] = user["uid"] in cfg.get("privacy", {}).get("opt_out_uids", [])
        return ok(user)
    if method == "GET" and path == "/api/my/profile":
        return ok(app.my_profile(user["uid"]))
    if method == "POST" and path == "/api/my/profile":
        return ok(app.save_my_profile(user["uid"], body.get("opt_out"), lang=lang))
    if method == "GET" and path == "/api/my/phrases":
        return ok(app.my_phrases(user["uid"], one("q"), int(one("before_id", "0")), int(one("limit", "50"))))
    if method == "GET" and path == "/api/conversation":
        return ok(app.conversation(user["uid"], int(one("after_id", "0"))))
    if method == "GET" and path == "/api/presence":
        return ok(app.presence(user["uid"], lang=lang))
    if method == "GET" and path == "/api/actions":
        return ok(app.actions(user["uid"], user["admin"], int(one("before_id", "0")), int(one("limit", "50"))))
    if method == "GET" and path == "/api/stats":
        return ok(app.stats(int(one("days", "30"))))
    if not user["admin"]:
        raise err(403, "error.admin_only")

    # Admins only: making the bot say anything in the channel.
    if method == "POST" and path == "/api/say":
        by = user.get("identity") or user.get("nickname") or user["uid"]
        return ok(app.bot("POST", "/say", {"text": str(body.get("text", "")), "by": by}, lang=lang))
    if method == "GET" and path == "/api/voice":
        from urllib.parse import quote

        return 200, "audio/wav", app.bot("GET", f"/voice?text={quote(one('text'))}", raw=True, lang=lang), None

    # Admins only.
    if method == "GET" and path == "/api/state":
        return ok(app.bot("GET", "/state", lang=lang))
    if method == "GET" and path == "/api/logs":
        return ok(app.bot("GET", f"/logs?after={int(one('after', '0'))}", lang=lang))
    if method == "POST" and path == "/api/restart":
        return ok(app.bot("POST", "/restart", lang=lang))
    if method == "GET" and path == "/api/settings":
        return ok(app.settings(lang=lang))
    if method == "POST" and path == "/api/settings":
        return ok(app.save_settings(body, lang=lang))
    if method == "POST" and path == "/api/plugins/test":
        return ok(app.test_plugin(str(body.get("name", "")), lang=lang))
    if method == "GET" and path == "/api/services":
        return ok(app.service_list(lang=lang))
    if method == "POST" and path == "/api/services":
        return ok(app.service_action(str(body.get("name", "")), str(body.get("action", "")), lang=lang))
    if method == "GET" and path == "/api/services/logs":
        return ok(app.service_logs(one("name"), int(one("lines", "200")), lang=lang))
    raise err(404, "error.page_not_found")


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def _serve(self, method: str) -> None:
            url = urlparse(self.path)
            if method == "GET" and not url.path.startswith("/api/"):
                return self._static(url.path)

            query = parse_qs(url.query)
            accept_lang = self.headers.get("Accept-Language", "").split(",")[0].split(";")[0].split("-")[0].lower()
            raw_lang = query.get("lang", [accept_lang])[0] or "fr"
            lang = raw_lang if raw_lang in app.available_languages() else raw_lang[:2].lower()
            if lang not in app.available_languages():
                lang = "fr"

            cookie = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
            user = app.session(cookie.value if cookie else None)
            try:
                body = {}
                if method == "POST":
                    # A JSON body cannot be sent by another site's form: this blocks cross-site requests.
                    if not self.headers.get("Content-Type", "").startswith("application/json"):
                        raise HttpError(415, "error.json_expected", lang=lang)
                    size = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(size) or b"{}") if size else {}
                host = self.headers.get("Host", "")
                status, kind, data, new_cookie = route(
                    app, method, url.path, query, body, user, f"http://{host}" if host else "", lang=lang
                )
            except HttpError as error:
                status, kind, data, new_cookie = (
                    error.status,
                    "application/json",
                    json.dumps({"error": error.localized(lang)}, ensure_ascii=False).encode(),
                    None,
                )
            except Exception as error:
                log.exception("%s %s failed", method, url.path)
                msg = tr("error.internal", lang, error=str(error))
                status, kind, data, new_cookie = (
                    500,
                    "application/json",
                    json.dumps({"error": msg}, ensure_ascii=False).encode(),
                    None,
                )

            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if new_cookie is not None:
                max_age = app.web.get("session_days", 30) * 86400 if new_cookie else 0
                self.send_header(
                    "Set-Cookie", f"{COOKIE}={new_cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
                )
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
    server = ThreadingHTTPServer(
        (web.get("host", "0.0.0.0"), web.get("port", 8095)), make_handler(App(cfg, args.config))
    )
    log.info("management site on http://%s:%d", *server.server_address[:2])
    server.serve_forever()


if __name__ == "__main__":
    main()
