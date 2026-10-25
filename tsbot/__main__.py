"""Entry point: `python -m tsbot [run|export] [--config config.toml]`."""
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


def serve(stream, transcribe, archive: Archive, loaded: list, cfg: dict) -> None:
    stt = cfg.get("stt", {})
    opt_out = set(cfg.get("privacy", {}).get("opt_out_uids", []))
    for header, pcm in read_frames(stream):
        raw = header.get("speaker", {})
        if header["event"] == "ready":
            me = to_speaker(cfg, raw)
            log.info("connected as %s (uid %s)", me.nickname, me.uid)
            plugins.dispatch(loaded, "on_ready", me)
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
        )
        log.info("[%s] %s  (%d ms audio, %d ms STT)", speaker.identity or speaker.nickname, text, duration_ms, stt_ms)
        plugins.dispatch(loaded, "on_transcription", Transcription(archive_id, speaker, header.get("started_ms", 0), duration_ms, text))


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
    # Passwords only come from environment variables (TS_SERVER_PASSWORD, TS_CHANNEL_PASSWORD).
    return subprocess.Popen([ts.get("bridge_binary", "ts-bridge")], stdout=subprocess.PIPE, env=env)


def run(cfg: dict) -> None:
    from .stt import Transcriber

    transcribe = Transcriber(cfg.get("stt", {}))
    archive = Archive(cfg.get("archive", {}).get("path", "data/archive.sqlite3"))
    loaded = plugins.load(cfg)
    bridge = spawn_bridge(cfg["teamspeak"])
    try:
        serve(bridge.stdout, transcribe, archive, loaded, cfg)
    finally:
        if bridge.poll() is None:
            bridge.send_signal(signal.SIGINT if os.name == "posix" else signal.SIGTERM)  # SIGINT = clean disconnect
            try:
                bridge.wait(5)
            except subprocess.TimeoutExpired:
                bridge.kill()
    sys.exit(bridge.returncode or 0)


def main() -> None:
    parser = argparse.ArgumentParser(prog="tsbot")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "export"])
    parser.add_argument("--config", default=os.environ.get("TSBOT_CONFIG", "config.toml"))
    args = parser.parse_args()
    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "export":
        n = Archive(cfg.get("archive", {}).get("path", "data/archive.sqlite3")).export_jsonl(sys.stdout)
        log.info("%d rows exported", n)
        return
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # `docker stop`: exit cleanly through `finally`
    run(cfg)


if __name__ == "__main__":
    main()
