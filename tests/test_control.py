"""Control API used by the management site, and the site's settings on top of config.toml."""
import json
import logging
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tsbot.__main__ import load_config, merge, to_speaker
from tsbot.archive import Archive
from tsbot.control import Control, LogBuffer, State
from tsbot.voice import Bot


class FakeVoice:
    engine = "piper"
    phrases = {}

    def __init__(self):
        self.sent = []

    def send(self, command, pcm=b""):
        self.sent.append(command)

    def say(self, what):
        return what, 1.5


class ControlTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = {"web": {"token_file": f"{self.dir.name}/token", "control_port": 0, "clients_refresh_s": 3600},
                    "identities": {"uid:a=": "Alice"}}
        self.voice, self.logs = FakeVoice(), LogBuffer()
        self.bot = Bot(self.voice, Archive(":memory:"))
        self.state = State()
        self.control = Control(self.cfg, self.bot, self.voice, None, [], self.state, self.logs)

    def tearDown(self):
        self.dir.cleanup()

    def call(self, method, path, query=None, body=None):
        status, kind, data = self.control.handle(method, path, query or {}, body or {})
        return status, json.loads(data) if kind == "application/json" else data

    def test_state_says_who_is_there_and_who_speaks(self):
        self.state.on_event("clients", {"clients": [{"client_id": 1, "uid": "a=", "nickname": "al"}]}, lambda r: to_speaker(self.cfg, r))
        self.state.on_event("speaking", {"speaker": {"client_id": 1, "uid": "a=", "nickname": "al"}}, lambda r: to_speaker(self.cfg, r))
        status, state = self.call("GET", "/state")
        self.assertEqual(status, 200)
        self.assertEqual((state["clients"][0]["identity"], state["speaking"][0]["identity"], state["llm"]), ("Alice", "Alice", None))

    def test_say_message_logs_and_unknown(self):
        self.assertEqual(self.call("POST", "/say", body={"text": "Bonjour"}), (200, {"seconds": 1.5}))
        self.assertEqual(self.call("POST", "/say", body={"text": " "})[0], 400)
        self.call("POST", "/message", body={"client_id": 4, "text": "Code : 123456"})
        self.assertEqual(self.voice.sent, [{"cmd": "message", "client_id": 4, "text": "Code : 123456"}])
        self.logs.emit(logging.LogRecord("x", logging.INFO, "", 0, "un", None, None))
        self.logs.emit(logging.LogRecord("x", logging.INFO, "", 0, "deux", None, None))
        status, lines = self.call("GET", "/logs", {"after": ["1"]})
        self.assertEqual([line["id"] for line in lines], [2])
        self.assertEqual(self.call("GET", "/nothing")[0], 404)

    def test_http_needs_the_token(self):
        self.control.start()
        url = f"http://127.0.0.1:{self.control.port}/state"
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(refused.exception.code, 401)
        token = Path(self.cfg["web"]["token_file"]).read_text()
        reply = urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=5)
        self.assertIn("clients", json.loads(reply.read()))


class SettingsTest(unittest.TestCase):
    def test_site_settings_go_on_top_of_config(self):
        self.assertEqual(merge({"llm": {"a": 1, "b": 2}, "x": [1]}, {"llm": {"b": 3}, "x": [2]}), {"llm": {"a": 1, "b": 3}, "x": [2]})
        with tempfile.TemporaryDirectory() as d:
            Path(d, "config.toml").write_text(f'[web]\nsettings_file = "{d}/settings.json"\n[llm]\nwake_words = ["hector"]\ntimeout_s = 20\n'.replace("\\", "/"))
            Path(d, "settings.json").write_text('{"llm": {"wake_words": ["gaston"]}}')
            cfg = load_config(f"{d}/config.toml")
        self.assertEqual(cfg["llm"], {"wake_words": ["gaston"], "timeout_s": 20})


if __name__ == "__main__":
    unittest.main()
