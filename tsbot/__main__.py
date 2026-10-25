"""Entry point: `python -m tsbot [run|export|say "text"] [--config config.toml]`."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
import tomllib

from . import plugins
from .archive import Archive
from .plugins import Speaker, Transcription

log = logging.getLogger("tsbot")
BYTES_PER_MS = 32  # 16-bit PCM, 16 kHz, mono


def read_frames(stream):
    """Frames sent by the Rust bridge: one JSON line, then `samples` × 2 bytes of PCM audio."""
    for line in iter(stream.readline, b""):
        header = json.loads(line)
        size = header.get("samples", 0) * 2
        pcm = stream.read(size) if size else b""
        if len(pcm) != size:
            raise EOFError("truncated audio frame: the bridge stopped while writing")
        yield header, pcm


def resolve_identity(cfg: dict, uid: str | None, groups) -> str | None:
    """The UID wins (one per person); otherwise the first TeamSpeak server group mapped to an identity."""
    table = cfg.get("identities", {})
    if uid and f"uid:{uid}" in table:
        return table[f"uid:{uid}"]
    return next((table[f"group:{g}"] for g in groups if f"group:{g}" in table), None)


def to_speaker(cfg: dict, raw: dict) -> Speaker:
    groups = tuple(raw.get("server_groups") or ())
    return Speaker(
        client_id=raw.get("client_id", 0),
        uid=raw.get("uid"),
        nickname=raw.get("nickname", "?"),
        server_groups=groups,
        identity=resolve_identity(cfg, raw.get("uid"), groups),
    )


def serve(stream, transcribe, archive: Archive, loaded: list, cfg: dict, bot=None, router=None, state=None) -> None:
    stt = cfg.get("stt", {})
    opt_out = set(cfg.get("privacy", {}).get("opt_out_uids", []))
    for header, pcm in read_frames(stream):
        raw = header.get("speaker", {})
        if header["event"] == "clients" and bot is not None and bot.me is not None:
            bot.listeners = [c["uid"] for c in header.get("clients", []) if c.get("uid") and c.get("hearing")
                             and c.get("channel_id") == bot.channel_id and c.get("client_id") != bot.me.client_id]
        if state is not None and header["event"] in ("clients", "speaking"):
            state.on_event(header["event"], header, lambda r: to_speaker(cfg, r))
            continue
        if header["event"] == "text":
            plugins.dispatch(loaded, "on_text", (to_speaker(cfg, raw), header.get("text", "")))
            continue
        if header["event"] == "ready":
            me = to_speaker(cfg, raw)
            log.info("connected as %s (uid %s)", me.nickname, me.uid)
            if bot:
                bot.me, bot.channel_id = me, raw.get("channel_id")
            plugins.dispatch(loaded, "on_ready", me)
            if router:
                router.start()
            continue
        if header["event"] != "utterance" or raw.get("uid") in opt_out:
            continue
        duration_ms = len(pcm) // BYTES_PER_MS
        if duration_ms < stt.get("min_utterance_ms", 400):
            continue
        t0 = time.monotonic()
        text = transcribe(pcm)
        stt_ms = int((time.monotonic() - t0) * 1000)
        if not text:
            continue
        speaker = to_speaker(cfg, raw)
        archive_id = archive.add(
            started_ms=header.get("started_ms", 0), duration_ms=duration_ms, client_id=speaker.client_id,
            uid=speaker.uid, nickname=speaker.nickname, identity=speaker.identity,
            server_groups=speaker.server_groups, channel_id=raw.get("channel_id"), text=text, stt_ms=stt_ms,
            listeners=header.get("listeners", []),
        )
        if bot:
            bot.listeners = header.get("listeners", bot.listeners)
        log.info("[%s] %s  (%d ms audio, %d ms STT)", speaker.identity or speaker.nickname, text, duration_ms, stt_ms)
        t = Transcription(archive_id, speaker, header.get("started_ms", 0), duration_ms, text)
        if bot:
            bot.heard(t)
        plugins.dispatch(loaded, "on_transcription", t)
        if router:
            router.handle(t)


def spawn_bridge(ts: dict) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "TS_ADDRESS": ts["address"],
        "TS_NICKNAME": ts.get("nickname", "VoiceBot"),
        "TS_IDENTITY_FILE": ts.get("identity_file", "data/identity.json"),
        "TS_MAX_UTTERANCE_S": str(ts.get("max_utterance_s", 30)),
    })
    if ts.get("channel"):
        env["TS_CHANNEL"] = ts["channel"]
    # Passwords: from the site's settings if set there, otherwise from the environment (.env).
    for key, var in (("server_password", "TS_SERVER_PASSWORD"), ("channel_password", "TS_CHANNEL_PASSWORD")):
        if ts.get(key):
            env[var] = ts[key]
    return subprocess.Popen([ts.get("bridge_binary", "ts-bridge")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)


def stop_bridge(bridge: subprocess.Popen) -> None:
    if bridge.poll() is None:
        bridge.send_signal(signal.SIGINT if os.name == "posix" else signal.SIGTERM)  # SIGINT = clean disconnect
        try:
            bridge.wait(5)
        except subprocess.TimeoutExpired:
            bridge.kill()


def run(cfg: dict, logs=None) -> None:
    from .control import Control, State
    from .stt import Transcriber
    from .voice import Bot, Voice

    transcribe = Transcriber(cfg.get("stt", {}), api_key=cfg.get("llm", {}).get("api_key", ""))
    archive = Archive(cfg.get("archive", {}).get("path", "data/archive.sqlite3"))
    loaded = plugins.load(cfg)
    bridge = spawn_bridge(cfg["teamspeak"])
    voice = Voice(cfg.get("voice", {}), bridge.stdin)
    bot = Bot(voice, archive)
    plugins.dispatch(loaded, "attach", bot)
    router = None
    if cfg.get("llm", {}).get("enabled"):
        from .llm import LLM
        from .router import Router

        router = Router(cfg["llm"], LLM(cfg["llm"]), bot, loaded)
    state = State()

    def names() -> list:
        """What speech-to-text should expect: known people, connected nicknames, players, the wake words."""
        found = list(cfg.get("identities", {}).values()) + [c.get("nickname", "") for c in state.clients]
        for p in loaded:
            try:
                ctx = p.context() if hasattr(p, "context") else {}
            except Exception:
                ctx = {}
            found += [n for v in ctx.values() if isinstance(v, list) for n in v if isinstance(n, str)]
        return found + cfg.get("llm", {}).get("wake_words", [])

    transcribe.names = names
    if cfg.get("web", {}).get("control", True):
        Control(cfg, bot, voice, router, loaded, state, logs).start()
    try:
        serve(bridge.stdout, transcribe, archive, loaded, cfg, bot, router, state)
    finally:
        stop_bridge(bridge)
    sys.exit(bridge.returncode or 0)


def say(cfg: dict, text: str) -> None:
    """Join the server, say one sentence, leave. Handy to test the voice."""
    from .voice import AUDIO_FILES, Voice

    bridge = spawn_bridge(cfg["teamspeak"])
    voice = Voice(cfg.get("voice", {}), bridge.stdin)
    if not text.lower().endswith(AUDIO_FILES):
        voice.speech_file(text)  # generate while the bridge connects
    try:
        for header, _ in read_frames(bridge.stdout):
            if header["event"] == "ready":
                voice.say(text)
            elif header["event"] == "played":
                break
    finally:
        stop_bridge(bridge)


def merge(base: dict, extra: dict) -> dict:
    """Settings saved by the site, on top of config.toml: tables are merged, other values replaced."""
    out = dict(base)
    for key, value in extra.items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    settings = cfg.get("web", {}).get("settings_file", "data/settings.json")
    if os.path.exists(settings):
        with open(settings, encoding="utf-8") as f:
            cfg = merge(cfg, json.load(f))
    return cfg


def main() -> None:
    parser = argparse.ArgumentParser(prog="tsbot")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "export", "say"])
    parser.add_argument("text", nargs="?", help="what to say (say command only)")
    parser.add_argument("--config", default=os.environ.get("TSBOT_CONFIG", "config.toml"))
    args = parser.parse_args()
    cfg = load_config(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "export":
        n = Archive(cfg.get("archive", {}).get("path", "data/archive.sqlite3")).export_jsonl(sys.stdout)
        log.info("%d rows exported", n)
        return
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # `docker stop`: exit cleanly through `finally`
    if args.command == "say":
        if not args.text:
            parser.error("say needs a text, for example: python -m tsbot say \"Bonjour\"")
        say(cfg, args.text)
        return
    from .control import LogBuffer

    logs = LogBuffer()
    logging.getLogger().addHandler(logs)
    run(cfg, logs)


if __name__ == "__main__":
    main()
