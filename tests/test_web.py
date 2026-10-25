"""Management site: login by TeamSpeak code, roles, private sentences, settings, statistics. No network, no bot."""
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from tsbot.archive import Archive
from tsbot.plugins import ProposedAction, Speaker
from tsbot.web.__main__ import route
from tsbot.web.app import MASK, App, HttpError

ADMIN = {"client_id": 3, "uid": "admin=", "nickname": "Chef", "server_groups": ["Server Admin"], "identity": "Chef"}
MEMBER = {"client_id": 4, "uid": "member=", "nickname": "Bob", "server_groups": ["Guest"], "identity": "Bob"}


class FakeBot:
    """Stands in for the bot's control API."""

    def __init__(self):
        self.messages, self.restarts = [], 0

    def __call__(self, req, timeout=0):
        path = req.full_url.split("8096", 1)[1]
        body = json.loads(req.data) if req.data else {}
        if path == "/state":
            data = {"started_ms": 0, "me": {"client_id": 1}, "clients": [{"client_id": 1, "uid": "bot="}, ADMIN, MEMBER]}
        elif path == "/message":
            self.messages.append(body)
            data = {"sent": True}
        elif path == "/restart":
            self.restarts += 1
            data = {"restarting": True}
        else:
            data = {}
        return io.BytesIO(json.dumps(data).encode())


class WebTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        Path(d, "config.toml").write_text(
            f'[web]\nsettings_file = "{d}/settings.json"\ntoken_file = "{d}/token"\nsecret_file = "{d}/secret"\n'
            f'[archive]\npath = "{d}/archive.sqlite3"\n[llm]\nwake_words = ["hector"]\n'
            '[plugins.my_plugin]\npassword = "s3cret"\n'.replace("\\", "/"))
        Path(d, "token").write_text("t")
        self.archive = Archive(f"{d}/archive.sqlite3")
        from tsbot.__main__ import load_config

        self.fake = FakeBot()
        self.app = App(load_config(f"{d}/config.toml"), f"{d}/config.toml", opener=self.fake)

    def tearDown(self):
        self.archive.db.close()
        self.dir.cleanup()

    def login(self, who):
        self.app.login_start(who["client_id"])
        code = self.fake.messages[-1]["text"].split(" : ")[1][:6]
        user, cookie = self.app.login_finish(who["client_id"], code)
        return self.app.session(cookie)

    def call(self, user, method, path, body=None, query=None):
        try:
            status, kind, data, cookie = route(self.app, method, path, query or {}, body or {}, user)
        except HttpError as error:  # what the server sends back for a refusal
            return {"error": str(error)}
        return json.loads(data) if kind == "application/json" else data

    def test_login_by_code_sent_on_teamspeak(self):
        names = [c["nickname"] for c in self.app.login_clients()]
        self.assertEqual(names, ["Bob", "Chef"])  # the bot itself is not offered
        self.app.login_start(4)
        self.assertEqual(self.fake.messages[0]["client_id"], 4)
        with self.assertRaises(HttpError):
            self.app.login_start(4)  # no new code right away
        with self.assertRaises(HttpError):
            self.app.login_finish(4, "000000" if "000000" not in self.fake.messages[0]["text"] else "111111")
        self.app.codes.clear()
        member, admin = self.login(MEMBER), self.login(ADMIN)
        self.assertEqual((member["admin"], admin["admin"], member["identity"]), (False, True, "Bob"))
        self.assertIsNone(self.app.session("forged.cookie"))
        cookie = self.app.sign({**admin, "exp": time.time() + 60})
        Path(self.dir.name, "settings.json").write_text('{"web": {"admin_groups": ["Nobody"]}}')
        self.assertFalse(self.app.session(cookie)["admin"])  # demoted at once, not when the cookie expires

    def test_too_many_wrong_codes(self):
        self.app.login_start(4)
        for _ in range(5):
            with self.assertRaises(HttpError):
                self.app.login_finish(4, "x")
        with self.assertRaises(HttpError) as blocked:
            self.app.login_finish(4, "x")
        self.assertEqual(blocked.exception.status, 429)

    def test_roles_and_private_sentences(self):
        member, admin = self.login(MEMBER), self.login(ADMIN)
        ids = [self.archive.add(started_ms=int(time.time() * 1000), duration_ms=900, client_id=4, uid=uid, nickname=n, identity=n,
                                server_groups=[], channel_id=1, text=text, stt_ms=200)
               for uid, n, text in [("member=", "Bob", "Hector, un point pour Chef"), ("admin=", "Chef", "Hector, deux points pour Bob")]]
        for uid, name, utterance in [("member=", "Bob", ids[0]), ("admin=", "Chef", ids[1])]:
            who = Speaker(1, uid, name, (), name)
            self.archive.add_action(ProposedAction("scores.add", {"player": "x"}, "?", who, (utterance,)), int(time.time() * 1000))
        self.assertEqual([p["text"] for p in self.call(member, "GET", "/api/my/phrases")], ["Hector, un point pour Chef"])
        seen = {a["identity"]: a["sentence"] for a in self.call(member, "GET", "/api/actions")}
        self.assertEqual(seen, {"Bob": "Hector, un point pour Chef", "Chef": None})  # someone else's sentence stays hidden
        self.assertEqual(self.call(member, "GET", "/api/stats"), {"error": "Réservé aux administrateurs."})
        self.assertEqual(self.call(None, "GET", "/api/me"), {"error": "Connecte-toi d'abord."})
        self.assertEqual(self.call(admin, "GET", "/api/stats")["stt_ms"]["count"], 2)
        self.call(admin, "POST", "/api/restart")
        self.assertEqual(self.fake.restarts, 1)

    def test_settings_are_checked_and_secrets_masked(self):
        admin = self.login(ADMIN)
        settings = self.call(admin, "GET", "/api/settings")
        self.assertEqual(settings["plugins"]["config"]["my_plugin"]["password"], MASK)
        saved = self.call(admin, "POST", "/api/settings", {"fields": {"llm.wake_words": ["gaston", " "]},
                                                           "identities": {"uid:member=": "Bob"},
                                                           "plugins": {"config": {"my_plugin": {"password": MASK, "url": "http://x"}}}})
        wake = next(f for f in saved["fields"] if f["key"] == "wake_words")
        self.assertEqual((wake["value"], saved["identities"]), (["gaston"], {"uid:member=": "Bob"}))
        self.assertEqual(self.app.current_config()["plugins"]["my_plugin"], {"password": "s3cret", "url": "http://x"})
        self.assertIn("error", self.call(admin, "POST", "/api/settings", {"fields": {"llm.confirm_timeout_s": "vite"}}))
        self.assertIn("error", self.call(admin, "POST", "/api/settings", {"fields": {"teamspeak.address": "evil"}}))
        self.assertIn("error", self.call(admin, "POST", "/api/settings", {"identities": {"bob": "Bob"}}))


if __name__ == "__main__":
    unittest.main()
