"""Speech-to-text.

Local, on the CPU with onnx-asr: NVIDIA Canary-1B-v2 with the language forced (default), or Parakeet TDT 0.6B v3
(language guessed: short sounds often come out as English). Optional: Gemini through its OpenAI-compatible API,
told the names and nicknames to expect; the audio then leaves this machine. If Gemini fails, the local model takes over.
"""
from __future__ import annotations

import base64
import difflib
import io
import logging
import os
import queue
import re
import threading
import time
import unicodedata
import wave

from . import SPOKEN

log = logging.getLogger(__name__)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


def sound_key(word: str) -> str:
    """Rough French spelling-to-sound key: "IP-Cos", "Ipkoz" and "ipcos" all give "ipkos"."""
    w = unicodedata.normalize("NFKD", word.lower()).encode("ascii", "ignore").decode()
    w = re.sub(r"[^a-z]", "", w)
    for a, b in (("ph", "f"), ("qu", "k"), ("ck", "k"), ("c", "k"), ("q", "k"), ("z", "s"), ("y", "i"), ("dj", "j"), ("h", "")):
        w = w.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", w)  # doubled letters sound like one


def fix_names(text: str, names) -> str:
    """Replace words that sound like a known name or nickname ("Bajel" -> "BaJell") with its real spelling."""
    keys = {sound_key(n): n for n in names if n and len(sound_key(n)) >= 4}
    if not keys:
        return text
    words = re.findall(r"[\w'-]+|\W+", text)
    out, i = [], 0
    while i < len(words):
        best = None
        for size in (1, 2):  # a single word first; then "IP Cos", one name heard as two words
            chunk = words[i:i + 2 * size - 1]
            if len(chunk) < 2 * size - 1 or not chunk[0].strip() or (size == 2 and (not chunk[-1].strip() or chunk[1] != " ")):
                continue
            key = sound_key("".join(chunk))
            if len(key) < 4:
                continue
            # "sur Claude" sounds close to "Claude" too: when the last word alone is the name, keep the first word.
            if size == 2 and difflib.get_close_matches(sound_key(chunk[-1]), keys, n=1, cutoff=0.8):
                continue
            match = difflib.get_close_matches(key, keys, n=1, cutoff=0.8)
            if match:
                best = (keys[match[0]], 2 * size - 1)
                break
        if best:
            out.append(best[0])
            i += best[1]
        else:
            out.append(words[i])
            i += 1
    return "".join(out)


def no_loops(text: str) -> str:
    """A model stuck in a loop (seen live: "ouais, " 200 times, "Ooooo…", "!!!!!!"): three in a row are enough."""
    text = re.sub(r"(\b[\w'-]+\W{1,3})\1{3,}", r"\1\1\1", text, flags=re.I)
    text = re.sub(r"([^\W\d])\1{3,}", lambda m: m[0][:3], text, flags=re.I)  # letters only: "10000" stays
    return re.sub(r"([^\w\s])\1{3,}", r"\1\1\1", text)


class Transcriber:
    def __init__(self, cfg: dict, api_key: str = ""):
        self.cfg = cfg
        self.names = lambda: []  # set by the bot: people, nicknames and words to expect
        self._local = None
        self.cloud = None
        if "gemini" in (cfg.get("engine"), cfg.get("compare_model")):
            from .llm import LLM

            key = cfg.get("api_key") or api_key or os.environ.get("GEMINI_API_KEY", "")
            self.cloud = LLM({"base_url": cfg.get("gemini_url", GEMINI_URL), "model": cfg.get("gemini_model", "gemini-flash-latest"),
                              "api_key": key, "timeout_s": cfg.get("gemini_timeout_s", 15), "json_mode": False,
                              "fallbacks": cfg.get("gemini_fallbacks", [])})
        if not cfg.get("lazy"):
            self.local()  # loaded now even with Gemini: it is the fallback, and loading takes a few seconds

    def load(self, model: str, threads: int = 0):
        import onnx_asr  # late import: heavy, and not needed by the tests
        import onnxruntime

        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = threads  # 0 = onnxruntime's default (all cores)
        # Canary and Whisper take the language; Parakeet has no such option and guesses it.
        takes_language = self.cfg.get("language") and ("canary" in model or "whisper" in model)
        loaded = onnx_asr.load_model(model, quantization="int8", sess_options=options)
        return loaded, {"language": self.cfg["language"]} if takes_language else {}

    def local(self):
        if self._local is None:
            model, self.options = self.load(self.cfg.get("model", "nemo-canary-1b-v2"))
            if self.cfg.get("vad", True):
                import onnx_asr

                # Silero keeps only the speech: a breath or a click no longer comes out as "Merci." (measured), and
                # never reaches the model. A long minimum silence keeps a joined sentence in one piece. The default
                # 30 ms margin cut soft first sounds live ("botop, je t'ai parlé" heard as "Top, je t'ai parlé").
                model = model.with_vad(onnx_asr.load_vad("silero"), min_silence_duration_ms=self.cfg.get("vad_split_ms", 2000),
                                       max_speech_duration_s=30, speech_pad_ms=self.cfg.get("vad_pad_ms", 250))
            self._local = model
        return self._local

    def __call__(self, pcm: bytes) -> str:
        text = fix_names(no_loops(self.transcribe(pcm)), self.names())
        if not self.laughing(pcm):
            return text
        laugh = self.cfg.get("laugh_text", "(rires)")
        noise = re.sub(r"\W+", "", text.lower()) in self.cfg.get("noise_phrases", ["merci"])  # a laugh alone heard as "Merci."
        return laugh if noise else f"{text} {laugh}".strip()

    def laughing(self, pcm: bytes) -> bool:
        """The speech model drops laughter: a small sound tagger (CED, AudioSet classes) hears it instead, in ~20 ms."""
        if not self.cfg.get("laughs"):
            return False
        import numpy as np

        if getattr(self, "_tagger", None) is None:
            import onnxruntime
            from huggingface_hub import hf_hub_download

            self._tagger = onnxruntime.InferenceSession(hf_hub_download(self.cfg.get("laugh_model", "mispeech/ced-mini"), "model.onnx"))
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        step = 10 * 16000  # CED-mini takes 10 s at most (12 s failed live and stopped the bot)
        try:
            # AudioSet 16-21: laughter, baby laughter, giggle, snicker, belly laugh, chuckle. Measured: real laughs
            # 0.23-0.58, speech, coughs, typing, clapping 0.00-0.05.
            best = max(float(self._tagger.run(None, {"waveform": audio[None, i:i + step]})[0][0][16:22].max())
                       for i in range(0, max(len(audio) - 3200, 1), step))  # a last bit under 0.2 s is skipped
        except Exception:
            log.exception("laughter detection failed")  # never stop listening because of it
            return False
        return best * 100 >= self.cfg.get("laugh_threshold_pct", 20)

    def compare(self, pcm: bytes, who: str, text: str) -> None:
        """Optional second model on the same sound, in the background, logged next to the first one's text so both
        can be judged on real voices (the sound itself is never kept). When it lags behind, sounds are skipped."""
        name = self.cfg.get("compare_model")
        if not name:
            return
        if not hasattr(self, "queue"):
            self.queue = queue.Queue(maxsize=self.cfg.get("compare_queue", 3))
            threading.Thread(target=self._compare_loop, args=(name,), daemon=True).start()
        try:
            self.queue.put_nowait((pcm, who, text))
        except queue.Full:
            log.info("compare: second model busy, sound skipped")

    def _compare_loop(self, name: str) -> None:
        if hasattr(os, "setpriority"):  # Linux: this thread (and the model's threads it starts) yield to the bot
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), self.cfg.get("compare_nice", 15))
        if name == "gemini":
            recognize = self.with_gemini
        else:
            import numpy as np

            model, options = self.load(name, self.cfg.get("compare_threads", 2))
            recognize = lambda pcm: model.recognize(np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0,
                                                    sample_rate=16000, **options)
        while True:
            pcm, who, text = self.queue.get()
            started = time.monotonic()
            try:
                other = recognize(pcm)
            except Exception:
                log.exception("compare: %s failed", name)
                continue
            log.info(SPOKEN + "compare [%s] %s: %s | %s: %s  (%d ms)", who, self.cfg.get("model", "nemo-canary-1b-v2"), text, name,
                     fix_names(other.strip(), self.names()), (time.monotonic() - started) * 1000)

    def transcribe(self, pcm: bytes) -> str:
        # Measured on 18 real sentences: Gemini better on 7 (calls, English words), the local model on 2, both very
        # short ("Oui." heard "Alice": Gemini makes up a name from its list on a tiny sound). Short ones stay local:
        # 0.9 s, measured live: "Oui." 0.68-0.86 s, while a 0.96 s "toptop" came out "tup tup" from the local model.
        self.engine = self.cfg.get("model", "nemo-canary-1b-v2")
        if self.cfg.get("engine") == "gemini" and len(pcm) >= 32 * self.cfg.get("gemini_min_ms", 900):
            try:
                text = self.with_gemini(pcm)
                self.engine = "gemini"
                return text
            except Exception as error:  # quota, network, refused key...
                log.warning("Gemini transcription failed, local model used instead: %s", str(error)[:200])
        import numpy as np

        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        # TeamSpeak sends speech from its very first sample: with no quiet before it, the first word is often lost
        # or misheard ("toptop" → "Oktobre", live). A short silence on both sides gives the model a clean start.
        quiet = np.zeros(16 * self.cfg.get("lead_in_ms", 300), np.float32)
        audio = np.concatenate([quiet, audio, quiet])
        model = self.local()
        if hasattr(model, "asr"):  # live: a 2.7 s "Ooooh" came out as 55 s of "o" and froze the bot; cap the text length
            model.asr.config["max_sequence_length"] = 30 + int(len(audio) / 16000 * self.cfg.get("max_tokens_per_s", 10))
        result = model.recognize(audio, sample_rate=16000, **self.options)
        text = result if isinstance(result, str) else " ".join(s.text.strip() for s in result)
        return text.replace("<unk>", "").strip()  # "<unk>": a sign Canary cannot write ("90°" came out "90<unk>")

    def with_gemini(self, pcm: bytes) -> str:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        names = ", ".join(dict.fromkeys(n for n in self.names() if n))
        system = ("Transcris mot pour mot ce qui est dit dans l'audio, en français parlé familier tel quel (argot, verlan, "
                  "phrases coupées), sans corriger, résumer ni répondre. Réponds uniquement par la transcription, "
                  "ou par une réponse vide si rien d'intelligible n'est dit."
                  # Live: on unclear sounds it answered a lone name from this list ("Oui." → "Alice", then "Ilker").
                  + (f" Orthographe des noms, pseudos et mots du groupe, à utiliser seulement s'ils sont vraiment dits "
                     f"(n'écris jamais un nom de cette liste que tu n'entends pas) : {names}." if names else ""))
        audio = {"type": "input_audio", "input_audio": {"data": base64.b64encode(buf.getvalue()).decode(), "format": "wav"}}
        reply = self.cloud.request_chat([{"role": "system", "content": system}, {"role": "user", "content": [audio]}])
        return reply.strip().strip('"').strip()
