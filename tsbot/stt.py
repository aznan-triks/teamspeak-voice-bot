"""Speech-to-text.

Local, on the CPU with onnx-asr: NVIDIA Canary-1B-v2 with the language forced (default), or Parakeet TDT 0.6B v3
(language guessed: short sounds often come out as English). Optional: Gemini through its OpenAI-compatible API,
told the names and nicknames to expect; the audio then leaves this machine. If Gemini fails, the local model takes over.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import wave

log = logging.getLogger(__name__)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


class Transcriber:
    def __init__(self, cfg: dict, api_key: str = ""):
        self.cfg = cfg
        self.names = lambda: []  # set by the bot: people, nicknames and words to expect
        self._local = None
        self.cloud = None
        if cfg.get("engine") == "gemini":
            from .llm import LLM

            key = cfg.get("api_key") or api_key or os.environ.get("GEMINI_API_KEY", "")
            self.cloud = LLM({"base_url": cfg.get("gemini_url", GEMINI_URL), "model": cfg.get("gemini_model", "gemini-flash-latest"),
                              "api_key": key, "timeout_s": cfg.get("gemini_timeout_s", 15), "json_mode": False})
        if not cfg.get("lazy"):
            self.local()  # loaded now even with Gemini: it is the fallback, and loading takes a few seconds

    def local(self):
        if self._local is None:
            import onnx_asr  # late import: heavy, and not needed by the tests

            model = self.cfg.get("model", "nemo-canary-1b-v2")
            self._local = onnx_asr.load_model(model, quantization="int8")
            # Canary takes the language; Parakeet has no such option and guesses it.
            self.options = {"language": self.cfg["language"]} if self.cfg.get("language") and "canary" in model else {}
        return self._local

    def __call__(self, pcm: bytes) -> str:
        if self.cloud is not None:
            try:
                return self.with_gemini(pcm)
            except Exception as error:  # quota, network, refused key...
                log.warning("Gemini transcription failed, local model used instead: %s", str(error)[:200])
        import numpy as np

        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        return self.local().recognize(audio, sample_rate=16000, **self.options).strip()

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
                  + (f" Noms, pseudos et mots qui peuvent être prononcés : {names}." if names else ""))
        audio = {"type": "input_audio", "input_audio": {"data": base64.b64encode(buf.getvalue()).decode(), "format": "wav"}}
        reply = self.cloud.request_chat([{"role": "system", "content": system}, {"role": "user", "content": [audio]}])
        return reply.strip().strip('"').strip()
