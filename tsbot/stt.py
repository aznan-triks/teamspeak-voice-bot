"""Speech-to-text on the CPU with onnx-asr: NVIDIA Canary-1B-v2 with the language forced (default),
or Parakeet TDT 0.6B v3 (language detected automatically: short sounds often come out as English)."""
from __future__ import annotations


class Transcriber:
    def __init__(self, cfg: dict):
        import onnx_asr  # late import: heavy, and not needed by the tests

        self.model = onnx_asr.load_model(cfg.get("model", "nemo-canary-1b-v2"), quantization="int8")
        # Canary takes the language; Parakeet has no such option and guesses it.
        self.options = {"language": cfg["language"]} if cfg.get("language") and "canary" in cfg.get("model", "nemo-canary-1b-v2") else {}

    def __call__(self, pcm: bytes) -> str:
        import numpy as np

        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        return self.model.recognize(audio, sample_rate=16000, **self.options).strip()
