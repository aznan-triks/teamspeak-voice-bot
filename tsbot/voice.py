"""The bot's voice: audio files first, then text-to-speech (Phonon, with Piper as the fallback)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import SPOKEN

log = logging.getLogger(__name__)
AUDIO_FILES = (".wav", ".ogg", ".opus", ".mp3", ".flac")
SEND_RATE = 48_000  # what the bridge expects: 16-bit PCM, 48 kHz, mono
PHONON_MODEL = "hf://gradium/phonon-40m-fr"  # best French voice in October 2026; gated, non-commercial
PIPER_VOICE = "fr_FR-siwis-medium"

# What the bot says on its own. French by default: override any line in [voice.phrases],
# with other words or with the path of an audio file you recorded.
DEFAULT_PHRASES = {
    "degraded": "Je passe en mode dégradé : je continue d'écouter et d'archiver, mais je n'agis plus.",
    "restored": "Je suis de nouveau pleinement opérationnel.",
    "cancelled": "D'accord, j'annule.",
    "failed": "L'action a échoué, désolé.",
    "invalid": "Je n'ai pas compris la demande.",
    "unknown_speaker": "Je ne sais pas qui tu es : ajoute ton identifiant dans ma configuration.",
}


def decode(path: Path) -> bytes:
    """Any audio file → 16-bit PCM, 48 kHz, mono (PyAV)."""
    import av

    out = bytearray()
    with av.open(str(path)) as f:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SEND_RATE)
        for frame in f.decode(audio=0):
            for r in resampler.resample(frame):
                out += r.to_ndarray().tobytes()
        for r in resampler.resample(None):
            out += r.to_ndarray().tobytes()
    return bytes(out)


class Voice:
    def __init__(self, cfg: dict, sink):
        self.cfg = cfg
        self.sink = sink  # the bridge's stdin
        self.lock = threading.Lock()  # the main loop and the control API both write to the bridge
        self.phrases = {**DEFAULT_PHRASES, **cfg.get("phrases", {})}
        self.cache = Path(cfg.get("cache_dir", "data/tts_cache"))
        self.engine = None  # chosen on first use

    def say(self, what: str) -> tuple[str, float]:
        """Say a phrase key, an audio file or plain text. Returns what was said and how long it lasts."""
        text = self.phrases.get(what, what)
        path = Path(text) if text.lower().endswith(AUDIO_FILES) else self.speech_file(text)
        pcm = decode(path)
        self.send({"cmd": "play", "samples": len(pcm) // 2}, pcm)
        return text, len(pcm) / 2 / SEND_RATE

    def send(self, command: dict, pcm: bytes = b"") -> None:
        """One command line to the bridge, followed by its audio if any."""
        with self.lock:
            self.sink.write(json.dumps(command, ensure_ascii=False).encode() + b"\n" + pcm)
            self.sink.flush()

    def speech_file(self, text: str) -> Path:
        """Text-to-speech, cached: a sentence is generated once, then replayed from disk."""
        engine = self._engine()
        voice = self.cfg.get("phonon_voice") if engine == "phonon" else self.cfg.get("piper_voice", PIPER_VOICE)
        key = hashlib.sha1(f"{engine}|{self.cfg.get('phonon_model', PHONON_MODEL)}|{voice}|{text}".encode()).hexdigest()
        path = self.cache / f"{key}.wav"
        if not path.exists():
            self.cache.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp.wav")
            if engine == "phonon":
                self._phonon.save(str(tmp), text, **({"voice": voice} if voice else {}))
            else:
                self._piper(text, voice, tmp)
            os.replace(tmp, path)  # never leave a half-written file in the cache
        return path

    def _engine(self) -> str:
        if self.engine:
            return self.engine
        self.engine = "piper"
        if self.cfg.get("engine", "phonon") == "phonon":
            try:
                import ptts

                self._phonon = ptts.TTS(self.cfg.get("phonon_model", PHONON_MODEL), lang=self.cfg.get("language", "fr"))
                # Extra voices: <name>.safetensors files from the gradium/phonon-voices catalogue, used by their name.
                for path in sorted(Path(self.cfg.get("phonon_voices_dir", "data/voices")).glob("*.safetensors")):
                    self._phonon.add_voice(path.stem, str(path))
                self.engine = "phonon"
            except Exception as error:  # gated model not accepted yet, no HF_TOKEN, no network...
                log.warning("Phonon unavailable, using Piper instead (%s). See README, 'A better voice'.", str(error)[:200])
        return self.engine

    def _piper(self, text: str, voice: str, out: Path) -> None:
        # Piper is GPL-3: it runs as a separate program, never imported, so this project stays MIT.
        voices = self.cfg.get("piper_dir", "data/models/piper")
        if not (Path(voices) / f"{voice}.onnx").exists():
            subprocess.run([sys.executable, "-m", "piper.download_voices", voice, "--data-dir", voices], check=True, timeout=300)
        subprocess.run([sys.executable, "-m", "piper", "-m", voice, "--data-dir", voices, "-f", str(out), "--", text], check=True, timeout=120)


class Bot:
    """What the core and plugins can ask of the bot: speak, and remember the recent conversation."""

    def __init__(self, voice: Voice | None, archive, window: int = 20):
        self.voice, self.archive, self.me = voice, archive, None
        self.window, self.size = [], window
        self.channel_id, self.listeners = None, []  # who can hear the bot right now
        self.spans: list = []  # (start ms, end ms, name) of the last lines, to see who talked over whom

    def heard(self, t) -> None:
        # A line is written when its speaker stops: people talking over each other would read like turns.
        name = t.speaker.identity or t.speaker.nickname
        end = t.started_ms + t.duration_ms
        over = list(dict.fromkeys(n for s, e, n in self.spans if n != name and s < end and t.started_ms < e))
        self.spans = (self.spans + [(t.started_ms, end, name)])[-6:]
        self._remember(f"[{name}" + (f", en même temps que {', '.join(over)}" if over else "") + f"] {t.text}")

    def _remember(self, line: str) -> None:
        self.window.append(line)
        # Trim by chunks, not one line at a time: the start of the conversation sent to the LLM then stays the
        # same between trims, and the LLM reuses what it already read instead of reading everything again.
        if len(self.window) > self.size * 3 // 2:
            del self.window[:-self.size]

    def say(self, what: str) -> float:
        """Speak and archive the sentence. Returns its duration in seconds (0 if the voice failed)."""
        if self.voice is None:
            log.info("[bot, voice off] %s", what)
            return 0.0
        try:
            text, seconds = self.voice.say(what)
        except Exception:
            log.exception("the bot could not speak: %r", what)  # never stop listening because of the voice
            return 0.0
        name = self.me.nickname if self.me else "bot"
        now = int(time.time() * 1000)
        self.spans = (self.spans + [(now, now + int(seconds * 1000), name)])[-6:]
        self._remember(f"[{name}] {text}")
        if self.me:
            self.archive.add(
                started_ms=int(time.time() * 1000), duration_ms=int(seconds * 1000), client_id=self.me.client_id,
                uid=self.me.uid, nickname=self.me.nickname, identity=None, server_groups=self.me.server_groups,
                channel_id=self.channel_id, text=text, stt_ms=None, listeners=self.listeners,
            )
        log.info(SPOKEN + "[%s] %s", name, text)
        return seconds
