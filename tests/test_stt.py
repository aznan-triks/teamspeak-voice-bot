"""Gemini transcription: the expected names go with the audio; when Gemini fails, the local model answers."""
import importlib.util
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tsbot.stt import Transcriber, fix_names, no_loops


class FakeGemini(BaseHTTPRequestHandler):
    requests, fail, empty = [], False, False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGemini.requests.append((self.headers.get("Authorization"), body))
        if FakeGemini.fail:
            self.send_response(429)
            self.end_headers()
            return
        message = {"role": "assistant"} if FakeGemini.empty else {"content": " Wesh BaJell, tu lances ? "}
        data = json.dumps({"choices": [{"message": message}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


class FakeLocal:
    def recognize(self, audio, sample_rate, **options):
        return "local"


class NamesTest(unittest.TestCase):
    NAMES = ["BaJell", "IPKOZ", "Ilker", "botop", "Bob"]

    def test_misheard_nicknames_get_their_real_spelling(self):
        cases = {
            "Wesh Bajel, tu lances la game ou quoi?": "Wesh BaJell, tu lances la game ou quoi?",
            "Wesh Badjell, tu lances ?": "Wesh BaJell, tu lances ?",
            "IP-Cos, il a encore rageé.": "IPKOZ, il a encore rageé.",
            "IP Cos il a encore ragé": "IPKOZ il a encore ragé",
            "c'est Ilker qui m'a tué, pas Ipekaos.": "c'est Ilker qui m'a tué, pas IPKOZ.",
            "Ouais, grave. TKT, je gère.": "Ouais, grave. TKT, je gère.",  # nothing close: unchanged
            "il a pris le bobsleigh": "il a pris le bobsleigh",  # short names are left alone
        }
        for heard, fixed in cases.items():
            self.assertEqual(fix_names(heard, self.NAMES), fixed, heard)
        # The group's own words; the short word before one is kept (it used to be swallowed: "sur Claude" → "Claude").
        self.assertEqual(fix_names("Tu vas sur Claude, ça arrive chez Gémini", ["Claude", "Gemini"]), "Tu vas sur Claude, ça arrive chez Gemini")
        # The bot's name misheard is found again, a common phrase that sounds close is left alone (heard live).
        self.assertEqual(fix_names("Botob, réponds", ["botop", "bottop"]), "bottop, réponds")
        self.assertEqual(fix_names("Elle est au top.", ["botop", "bottop"]), "Elle est au top.")

    def test_loops_are_cut(self):
        # Heard live: a model stuck repeating a word, a letter or a sign.
        self.assertEqual(no_loops("Ouais, " * 200 + "o, o, o, o, o."), "Ouais, Ouais, Ouais, o, o, o, o.")
        self.assertEqual(no_loops("O" + "o" * 300), "Ooo")
        self.assertEqual(no_loops("Le choix entre...!!!!!!!!!!!!"), "Le choix entre...!!!")
        self.assertEqual(no_loops("Il a 10000 points, non non non."), "Il a 10000 points, non non non.")


class GeminiTest(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), FakeGemini)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.stt = Transcriber({"engine": "gemini", "gemini_url": url, "lazy": True}, api_key="k")
        self.stt._local, self.stt.options = FakeLocal(), {}
        self.stt.names = lambda: ["BaJell", "IPKOZ", "BaJell"]

    def tearDown(self):
        self.server.shutdown()

    def test_short_sounds_stay_local(self):
        if importlib.util.find_spec("numpy") is None:
            self.skipTest("numpy (installed in the Docker image) is needed for the local model")
        sent = len(FakeGemini.requests)
        self.assertEqual((self.stt(b"\x00\x01" * 16000), self.stt.engine), ("local", "nemo-canary-1b-v2"))  # 1 s
        self.assertEqual(len(FakeGemini.requests), sent)

    def test_names_go_with_the_audio_and_failures_fall_back(self):
        pcm = b"\x00\x01" * 32000  # 2 s: long enough for Gemini
        self.assertEqual((self.stt(pcm), self.stt.engine), ("Wesh BaJell, tu lances ?", "gemini"))
        auth, body = FakeGemini.requests[-1]
        self.assertEqual(auth, "Bearer k")
        self.assertIn("BaJell, IPKOZ.", body["messages"][0]["content"])  # each name once
        self.assertEqual(body["messages"][1]["content"][0]["input_audio"]["format"], "wav")
        if importlib.util.find_spec("numpy") is None:
            self.skipTest("numpy (installed in the Docker image) is needed for the local fallback")
        FakeGemini.fail = True
        try:
            self.assertEqual(self.stt(pcm), "local")
        finally:
            FakeGemini.fail = False


class GeminiCompareTest(unittest.TestCase):
    def test_gemini_as_second_model_is_only_logged(self):
        server = HTTPServer(("127.0.0.1", 0), FakeGemini)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            stt = Transcriber({"compare_model": "gemini", "gemini_url": url, "lazy": True}, api_key="k")
            stt.names = lambda: ["BaJell"]
            self.assertIsNotNone(stt.cloud)
            with self.assertLogs("tsbot.stt", "INFO") as logs:
                stt.compare(b"\x00\x01" * 16000, "Ilker", "Wesh Bajel")
                for _ in range(50):
                    if logs.output:
                        break
                    time.sleep(0.05)
            self.assertIn("[spoken] compare [Ilker] nemo-canary-1b-v2: Wesh Bajel | gemini: Wesh BaJell, tu lances ?",
                          logs.output[0])
            FakeGemini.empty = True  # live: an answer with no "content" at all
            try:
                self.assertEqual(stt.with_gemini(b"\x00\x01" * 16000), "")
            finally:
                FakeGemini.empty = False
        finally:
            server.shutdown()


class FakeTagger:
    def __init__(self, laugh):
        self.laugh = laugh

    def run(self, _, inputs):
        import numpy as np

        if inputs["waveform"].shape[1] > 10 * 16000:
            raise RuntimeError("CED-mini fails beyond 10 s")
        return [np.array([[0.9] + [0.0] * 15 + [self.laugh] * 6 + [0.0] * 505])]


@unittest.skipIf(importlib.util.find_spec("numpy") is None, "numpy is installed in the Docker image")
class LaughTest(unittest.TestCase):
    def test_laughter_is_written_only_above_the_threshold(self):
        stt = Transcriber({"lazy": True, "vad": False, "laughs": True, "laugh_threshold_pct": 20})
        stt._local, stt.options = FakeLocal(), {}
        stt._tagger = FakeTagger(0.5)
        self.assertEqual(stt(b"\x00\x01" * 16000), "local (rires)")
        stt._tagger = FakeTagger(0.05)
        self.assertEqual(stt(b"\x00\x01" * 16000), "local")
        stt._tagger, stt.cfg["noise_phrases"] = FakeTagger(0.5), ["local"]  # a laugh alone, heard as "Merci."
        self.assertEqual(stt(b"\x00\x01" * 16000), "(rires)")
        self.assertEqual(stt(b"\x00\x01" * 16000 * 25), "(rires)")  # 25 s: read 10 s at a time (crashed the bot live)
        stt._tagger.run = lambda *_: 1 / 0
        self.assertEqual(stt(b"\x00\x01" * 16000), "local")  # a failing tagger never stops the transcription


if __name__ == "__main__":
    unittest.main()
