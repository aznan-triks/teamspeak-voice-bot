"""Speech-to-text with faster-whisper (CPU, int8)."""
from __future__ import annotations

# Phrases Whisper makes up on silence or noise (they come from its French training subtitles).
HALLUCINATIONS = (
    "amara.org",
    "sous-titrage st'",
    "sous-titres réalisés par",
    "sous-titrage société radio-canada",
    "merci d'avoir regardé",
    "abonnez-vous",
)
# Above this, a segment is mostly repetition ("... ... ...") rather than speech (Whisper's own default).
MAX_COMPRESSION_RATIO = 2.4


def is_hallucination(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in HALLUCINATIONS)


class Transcriber:
    def __init__(self, cfg: dict):
        from faster_whisper import WhisperModel  # late import: heavy, and not needed by the tests

        self.cfg = cfg
        self.model = WhisperModel(
            cfg.get("model", "small"),
            device="cpu",
            compute_type=cfg.get("compute_type", "int8"),
            cpu_threads=cfg.get("cpu_threads", 0),
            download_root=cfg.get("model_dir"),
        )

    def __call__(self, pcm: bytes) -> str:
        import numpy as np

        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            audio,
            language=self.cfg.get("language", "fr"),
            beam_size=self.cfg.get("beam_size", 1),
            # A single temperature disables Whisper's retry loop, which cost up to 7 s on a 1 s clip.
            # Repetitive output is dropped below instead of being decoded again.
            temperature=0.0,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=self.cfg.get("vocabulary") or None,
        )
        max_no_speech = self.cfg.get("max_no_speech_prob", 0.6)
        kept = (
            s.text.strip()
            for s in segments
            if s.no_speech_prob < max_no_speech and s.compression_ratio < MAX_COMPRESSION_RATIO
        )
        return " ".join(t for t in kept if t and not is_hallucination(t))
