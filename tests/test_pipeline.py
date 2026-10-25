"""Bridge → filters → archive → plugins, without TeamSpeak or Whisper. Run: `python -m unittest discover -s tests`."""
import io
import json
import unittest

from tsbot.__main__ import resolve_identity, serve
from tsbot.archive import Archive
from tsbot.stt import is_hallucination


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

    def test_truncated_frame_raises(self):
        with self.assertRaises(EOFError):
            serve(io.BytesIO(frame("utterance", ALICE, ms=1000)[:-10]), lambda p: "x", Archive(":memory:"), [], CFG)

    def test_identity_uid_wins_over_group(self):
        self.assertEqual(resolve_identity(CFG, "alice=", ["Modo"]), "Alice")
        self.assertIsNone(resolve_identity(CFG, "zed=", ["Guest"]))

    def test_hallucinations(self):
        self.assertTrue(is_hallucination("Sous-titres réalisés par la communauté d'Amara.org"))
        self.assertFalse(is_hallucination("on lance la partie"))


if __name__ == "__main__":
    unittest.main()
