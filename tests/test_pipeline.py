"""Bridge → filters → archive → plugins, without TeamSpeak or a speech model. Run: `python -m unittest discover -s tests`."""
import io
import json
import unittest

from tsbot import plugins
from tsbot.__main__ import resolve_identity, serve
from tsbot.archive import Archive


def frame(event, speaker, ms=0):
    samples = ms * 16
    header = {"event": event, "speaker": speaker, "started_ms": 1_700_000_000_000, "samples": samples}
    return json.dumps(header).encode() + b"\n" + b"\x00\x01" * samples


class Recorder:
    def __init__(self):
        self.events = []

    def on_ready(self, me):
        self.events.append(("ready", me))

    def on_transcription(self, t):
        self.events.append(("transcription", t))


class Broken:
    def on_transcription(self, t):
        raise RuntimeError("broken plugin")


CFG = {
    "stt": {"min_utterance_ms": 400},
    "privacy": {"opt_out_uids": ["optout="]},
    "identities": {"uid:alice=": "Alice", "group:Modo": "Bob"},
}
ALICE = {"client_id": 3, "uid": "alice=", "nickname": "al", "server_groups": ["Modo"], "channel_id": 7}
BOB = {"client_id": 4, "uid": "bob=", "nickname": "bobby", "server_groups": ["Guest", "Modo"], "channel_id": 7}
OPTED_OUT = {"client_id": 5, "uid": "optout=", "nickname": "x", "server_groups": [], "channel_id": 7}


class PipelineTest(unittest.TestCase):
    def test_serve(self):
        stream = io.BytesIO(b"".join([
            frame("ready", {"client_id": 9, "uid": "bot=", "nickname": "Bot", "server_groups": []}),
            frame("utterance", ALICE, ms=1000),
            frame("utterance", BOB, ms=200),    # too short: ignored
            frame("utterance", OPTED_OUT, ms=1000),  # opted out: never transcribed
            frame("utterance", BOB, ms=800),
        ]))
        heard = []
        archive, rec = Archive(":memory:"), Recorder()
        serve(stream, lambda pcm: heard.append(len(pcm)) or "bonjour", archive, [Broken(), rec], CFG)

        self.assertEqual(heard, [32000, 25600])  # only the two valid utterances reach speech-to-text
        kinds = [k for k, _ in rec.events]
        self.assertEqual(kinds, ["ready", "transcription", "transcription"])
        self.assertEqual(rec.events[0][1].uid, "bot=")
        self.assertEqual([e.speaker.identity for _, e in rec.events[1:]], ["Alice", "Bob"])

        out = io.StringIO()
        self.assertEqual(archive.export_jsonl(out), 2)
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(rows[1]["server_groups"], ["Guest", "Modo"])
        self.assertEqual((rows[0]["duration_ms"], rows[0]["identity"], rows[0]["text"]), (1000, "Alice", "bonjour"))

    def test_opting_out_on_the_site_applies_at_once(self):
        import os
        import tempfile

        settings = os.path.join(tempfile.mkdtemp(), "settings.json")

        class Stream(io.BytesIO):
            lines = 0

            def readline(self, *a):
                Stream.lines += 1
                if Stream.lines == 2:  # Alice opts out on the site between her two sentences
                    with open(settings, "w", encoding="utf-8") as f:
                        json.dump({"privacy": {"opt_out_uids": ["alice="]}}, f)
                return super().readline(*a)

        archive = Archive(":memory:")
        serve(Stream(frame("utterance", ALICE, ms=1000) + frame("utterance", ALICE, ms=1000)), lambda pcm: "salut",
              archive, [], {**CFG, "web": {"settings_file": settings}})
        self.assertEqual(archive.export_jsonl(io.StringIO()), 1)

    def test_sounds_too_quiet_to_be_speech_are_skipped(self):
        # frame() makes a steady signal of about -42 dB: skipped below a -30 dB threshold, transcribed without one.
        heard = []
        serve(io.BytesIO(frame("utterance", ALICE, ms=1000)), lambda pcm: heard.append(1) or "Oui.", Archive(":memory:"), [],
              {**CFG, "stt": {"min_level_db": -30}})
        self.assertEqual(heard, [])
        serve(io.BytesIO(frame("utterance", ALICE, ms=1000)), lambda pcm: heard.append(1) or "Oui.", Archive(":memory:"), [], CFG)
        self.assertEqual(heard, [1])

    def test_short_merci_is_noise(self):
        stream = io.BytesIO(frame("utterance", ALICE, ms=700) + frame("utterance", ALICE, ms=2000))
        archive = Archive(":memory:")
        serve(stream, lambda pcm: "Merci.", archive, [], CFG)
        self.assertEqual(archive.export_jsonl(io.StringIO()), 1)  # only the long one is kept

    def test_plugin_that_cannot_start_is_skipped(self):
        cfg = {"plugins": {"enabled": ["no_such_plugin", "tsbot.plugins_examples.scoreboard"]}}
        with self.assertLogs("tsbot.plugins", "ERROR"):
            loaded = plugins.load(cfg)
        self.assertEqual([type(p).__module__ for p in loaded], ["tsbot.plugins_examples.scoreboard"])

    def test_truncated_frame_raises(self):
        with self.assertRaises(EOFError):
            serve(io.BytesIO(frame("utterance", ALICE, ms=1000)[:-10]), lambda p: "x", Archive(":memory:"), [], CFG)

    def test_identity_uid_wins_over_group(self):
        self.assertEqual(resolve_identity(CFG, "alice=", ["Modo"]), "Alice")
        self.assertIsNone(resolve_identity(CFG, "zed=", ["Guest"]))


if __name__ == "__main__":
    unittest.main()
