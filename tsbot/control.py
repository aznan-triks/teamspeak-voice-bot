"""Local control API: what the management site asks the running bot (state, logs, speak, restart).

Listens on 127.0.0.1 only. Every request needs the token the bot writes to `token_file` at start,
so only programs that can read the bot's data folder (the site) can use it.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import threading
import time
from collections import deque
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import SPOKEN
from .plugins import WebRequest

log = logging.getLogger(__name__)


class LogBuffer(logging.Handler):
    """The last log lines, numbered, for the site's log page."""

    def __init__(self, size: int = 500):
        super().__init__()
        self.lines: deque = deque(maxlen=size)
        self.next_id = 1
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record) -> None:
        if str(record.msg).startswith(SPOKEN):
            return  # what people said: only for those who were in the channel, never in the site's log page
        self.lines.append({"id": self.next_id, "level": record.levelname, "text": self.format(record)})
        self.next_id += 1

    def after(self, last_id: int) -> list:
        return [line for line in list(self.lines) if line["id"] > last_id]


class State:
    """What the bridge tells about the server, kept for the live dashboard."""

    def __init__(self):
        self.started_ms = int(time.time() * 1000)
        self.clients: list = []
        self.speaking: deque = deque(maxlen=50)

    on_clients = None  # set when the bot follows people: called with each new client list
    channels = None  # the channels the bots are in (two when players split up), for "can this person hear it?"

    def on_event(self, event: str, header: dict, to_speaker) -> None:
        if event == "clients":
            self.clients = [{**raw, "identity": to_speaker(raw).identity} for raw in header.get("clients", [])]
            if self.on_clients:
                try:
                    self.on_clients(self.clients)
                except Exception:
                    log.exception("following people failed")  # never stop listening because of it
        elif event == "speaking":
            raw = header.get("speaker", {})
            self.speaking.append({**raw, "identity": to_speaker(raw).identity, "ms": int(time.time() * 1000)})


class Control:
    def __init__(self, cfg: dict, bot, voice, router, loaded: list, state: State, logs: LogBuffer):
        self.cfg, self.bot, self.voice, self.router, self.loaded = cfg, bot, voice, router, loaded
        self.state, self.logs = state, logs
        self.token = secrets.token_urlsafe(32)

    def start(self) -> None:
        web = self.cfg.get("web", {})
        path = Path(web.get("token_file", "data/control_token"))
        path.write_text(self.token, encoding="utf-8")
        path.chmod(0o600)
        server = ThreadingHTTPServer(("127.0.0.1", web.get("control_port", 8096)), self._handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Thread(target=self._poll_clients, daemon=True).start()
        self.port = server.server_address[1]
        log.info("control API on 127.0.0.1:%d", self.port)

    def _poll_clients(self) -> None:
        while True:
            try:
                self.voice.send({"cmd": "clients"})
            except Exception:
                log.exception("could not ask the bridge for the client list")
            time.sleep(self.cfg.get("web", {}).get("clients_refresh_s", 5))

    def snapshot(self) -> dict:
        me = self.bot.me
        return {
            "started_ms": self.state.started_ms,
            "me": asdict(me) if me else None,
            "bot_channels": self.state.channels() if self.state.channels else [self.bot.channel_id],
            "clients": self.state.clients,
            "speaking": list(self.state.speaking),
            "llm": None if self.router is None else {"degraded": self.router.degraded, "pending": bool(self.router.pending)},
            "voice": self.voice.engine,
            "plugins": [type(p).__module__ for p in self.loaded],
        }

    def handle(self, method: str, path: str, query: dict, body: dict):
        """Returns (status, content type, bytes)."""
        def ok(data) -> tuple:
            return 200, "application/json", json.dumps(data, ensure_ascii=False).encode()

        if method == "GET" and path == "/state":
            return ok(self.snapshot())
        if method == "GET" and path == "/logs":
            return ok(self.logs.after(int(query.get("after", ["0"])[0])))
        if method == "GET" and path == "/voice":
            text = query.get("text", [""])[0].strip()
            if not text:
                return 400, "text/plain", b"text is required"
            return 200, "audio/wav", self.voice.speech_file(text).read_bytes()
        if method == "POST" and path == "/say":
            text = str(body.get("text", "")).strip()
            if not text:
                return 400, "text/plain", b"text is required"
            log.info("asked from the site by %s", body.get("by", "?"))  # who made the bot say the next line
            return ok({"seconds": self.bot.say(text)})
        if method == "POST" and path == "/message":
            self.voice.send({"cmd": "message", "client_id": int(body["client_id"]), "text": str(body["text"]), "poke": bool(body.get("poke"))})
            return ok({"sent": True})
        if method == "GET" and path == "/plugins":
            actions = self.router.actions if self.router else {}
            return ok([{"name": type(p).__module__, "web": hasattr(p, "web"),
                        "actions": [n for n, (owner, _) in actions.items() if owner is p]} for p in self.loaded])
        if method == "POST" and path == "/plugin":
            # The site's door to any plugin; the answer is always 200, its own status inside.
            plugin = next((p for p in self.loaded if type(p).__module__ == body.get("name") and hasattr(p, "web")), None)
            if plugin is None:
                return ok({"status": 404, "error": "no such plugin"})
            request = WebRequest(str(body.get("method", "GET")), str(body.get("path", "")), body.get("query") or {},
                                 body.get("body") or {}, body.get("user") or {})
            try:
                return ok({"status": 200, "data": plugin.web(request)})
            except (PermissionError, LookupError, ValueError) as error:
                status = 403 if isinstance(error, PermissionError) else 404 if isinstance(error, LookupError) else 400
                return ok({"status": status, "error": str(error)})
            except Exception as error:
                log.exception("plugin %s failed in web", body.get("name"))
                return ok({"status": 500, "error": str(error)})
        if method == "POST" and path == "/restart":
            # The bot quits cleanly; Restart=always (systemd) or restart: unless-stopped (compose) starts it again.
            threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM)).start()
            return ok({"restarting": True})
        return 404, "text/plain", b"not found"

    def _handler(self):
        control = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self, method: str) -> None:
                if not secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {control.token}"):
                    return self._reply(401, "text/plain", b"bad token")
                url = urlparse(self.path)
                try:
                    size = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(size) or b"{}") if size else {}
                    self._reply(*control.handle(method, url.path, parse_qs(url.query), body))
                except Exception as error:
                    log.exception("control API: %s %s failed", method, url.path)
                    self._reply(500, "text/plain", str(error).encode())

            def _reply(self, status: int, kind: str, data: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

            def log_message(self, *_):
                pass  # one line per request every few seconds would drown the bot's log

        return Handler
