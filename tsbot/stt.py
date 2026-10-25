"""Speech-to-text on the CPU with NVIDIA Parakeet TDT 0.6B v3 (25 European languages, detected automatically)."""
from __future__ import annotations


class Transcriber:
    def __init__(self, cfg: dict):
        import onnx_asr  # late import: heavy, and not needed by the tests

        self.model = onnx_asr.load_model(cfg.get("model", "nemo-parakeet-tdt-0.6b-v3"), quantization="int8")

    def __call__(self, pcm: bytes) -> str:
        import numpy as np

        return self.model.recognize(np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0, sample_rate=16000).strip()
