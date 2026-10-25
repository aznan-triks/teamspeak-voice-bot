# Architecture

## 1. Overview

```
          TeamSpeak server (UDP 9987)
                ▲    │  Opus voice, one stream per person + server state
    bot's voice │    ▼
┌──────────── ts-bridge (Rust, tsclientlib) ─────────────┐
│ connection, persistent identity (stable UID)            │
│ hearing: jitter, loss, Opus decoding PER PERSON,        │
│   utterance = one person's stream (max 30 s),           │
│   48 kHz stereo → 16 kHz mono                           │
│ speaking: 48 kHz mono PCM → Opus, one frame every 20 ms │
└───────────────┬──────────────────────────▲──────────────┘
   stdout: JSON line + PCM          stdin: play command + PCM
                ▼                          │
┌──────────── tsbot (Python) ──────────────┴──────────────┐
│ opt-out, minimum length → speech-to-text (Parakeet v3)  │
│ → identity (UID, else server group) → SQLite → plugins  │
│ wake word → LLM (OpenAI-compatible) → validated reply:  │
│   answer · clarify · propose action → "yes"? → plugin   │
│ voice: audio files → Phonon → Piper (cached)            │
│ degraded mode: LLM down → say it once, keep archiving   │
└───────────────┬───────────────────────┬────────────────┘
                ▼                       ▼
        data/archive.sqlite3     plugins (outside this repo)
                                 → their own APIs
```

Why it is split this way:

- **Who is talking, without speaker recognition.** TeamSpeak carries one Opus stream per person, and every packet says who sent it. No AI is needed to tell voices apart, and two people talking at once are transcribed separately.
- **Where a sentence ends, without voice detection.** A TeamSpeak client only sends audio while its user talks (voice activation or push-to-talk). End of stream = end of the utterance.
- **Rust for the protocol, Python for the rest.** tsclientlib is the only free, maintained implementation of the TeamSpeak client protocol that we could verify (last commit July 2026). Python has the speech, LLM and voice ecosystem, and makes plugins easy to write.
- **A pipe, not a network.** Python starts the bridge and talks to it through stdin/stdout: no port, no broker. If Python dies, the bridge stops.

## 2. Repository layout

```
teamspeak-voice-bot/
├── bridge/src/main.rs        Rust bridge: hearing and speaking
├── tsbot/
│   ├── __main__.py           main loop, bridge protocol, identities, CLI (run, export, say)
│   ├── stt.py                Parakeet v3 (onnx-asr)
│   ├── voice.py              Voice (files, Phonon, Piper, cache) and Bot (say + remember)
│   ├── llm.py                OpenAI-compatible client (urllib, no SDK)
│   ├── router.py             wake word, LLM, confirmation, degraded mode
│   ├── archive.py            SQLite: utterances, actions, LLM timings, JSON lines export
│   ├── plugins.py            plugin contract types, loading, isolation
│   ├── control.py            local control API for the management site
│   ├── web/                  management site (python -m tsbot.web): server, pages in static/
│   └── plugins_examples/     scoreboard.py (voice action example)
├── prompts/system_fr.txt     LLM system prompt (French, see prompts/README.md)
├── docs/                     this file, PLUGIN_CONTRACT.md
├── tests/                    unit tests (no server, model or LLM needed)
├── config.example.toml
└── Dockerfile, docker-compose.yml, requirements.txt
```

## 3. Bridge ↔ orchestrator protocol

**Bridge stdout**: one JSON line, then `samples × 2` bytes of 16-bit little-endian PCM, 16 kHz, mono.

```
{"event":"ready","speaker":{...}}
{"event":"utterance","speaker":{...},"started_ms":1761355442000,"samples":16000}<32000 bytes>
{"event":"played"}
{"event":"speaking","speaker":{...}}
{"event":"clients","clients":[{...speaker}, ...]}
{"event":"text","speaker":{...},"text":"..."}
```

`speaker` = `{client_id, uid, nickname, server_groups: [names], channel_id}`, read when the person **starts** talking (they may have left by the time they stop). `played` = the bot finished saying everything it was given. `speaking` = someone starts talking (live view). `clients` = everyone connected, answer to the `clients` command. `text` = a private text message to the bot.

**Bridge stdin**: `{"cmd":"play","samples":N}` then N samples of 16-bit little-endian PCM, 48 kHz, mono. The bridge queues it, encodes 20 ms Opus frames and sends one per tick, then an empty packet (end of speech). `{"cmd":"message","client_id":N,"text":"..."}` sends a private text message; `{"cmd":"clients"}` asks for the client list.

**Control API** (`tsbot/control.py`, 127.0.0.1 only, token in `[web] token_file`): `GET /state`, `GET /logs?after=N`, `GET /voice?text=`, `POST /say`, `POST /message`, `POST /restart` (the bot quits; the service manager starts it again). The management site (`tsbot/web/`) uses only this API and a read-only view of the archive.

**Environment variables**: `TS_ADDRESS`, `TS_NICKNAME`, `TS_CHANNEL`, `TS_CHANNEL_PASSWORD`, `TS_SERVER_PASSWORD`, `TS_IDENTITY_FILE`, `TS_MAX_UTTERANCE_S`. SIGINT disconnects cleanly; a closed stdout stops the bridge.

## 4. Choices, with the evidence behind them (October 2026)

### Speech-to-text: Parakeet TDT 0.6B v3

Measured on 3 CPU cores, 6 French sentences (Windows voices), int8:

| Engine | Speed | Notable mistakes |
|---|---|---|
| Parakeet TDT 0.6B v3 (onnx-asr) | 3.5× real time | "Ebo" for "Hé bot", "annul" |
| Whisper small (faster-whisper) | 3.2× real time | "Et beau", "Ali" for "Alice", "à nul" |
| Whisper base | 8.3× real time | "alli", more errors on names |

Parakeet is as fast as Whisper small and slightly more accurate here. It also avoids two Whisper problems seen live: made-up sentences on silence, and repetition loops (a 1 s clip once took 7.6 s). Whisper was removed: its only extras were non-European languages and a vocabulary prompt, which this bot does not need.

Rejected: **Qwen3-ASR 0.6B**. It needs PyTorch or vLLM with a GPU (no CPU-friendly runtime), and its model card shows it behind Whisper large-v3 on 3 of 4 multilingual benchmarks.

### Wake word: "Hector"

Through Piper voices → Parakeet: Hector, Gaston and Ordinateur 3/3; Jarvis, Alfred, Robot 2/3; Majordome 1/3; Bender 0/3. Live on TeamSpeak, "Jarvis" was transcribed "J'arrive" and the bot was never called. A wake word must be tested with the speech-to-text engine in use.

### LLM: any OpenAI-compatible server; tested with llama.cpp + Qwen3.5-4B

- Runtime: **llama.cpp's `llama-server`** (CPU, Docker image `ghcr.io/ggml-org/llama.cpp:server`). It enforces valid JSON with `response_format`.
- Model: **Qwen3.5-4B** (Apache 2.0), Q4_K_M, thinking disabled (`--reasoning-budget 0`). 7/7 French test cases right (propose, misheard name, unknown name → clarify, question about the conversation, hesitant "yes", insult → ignore, prompt injection → ignore), 2.4 to 4.8 s per answer on 8 CPU threads of a desktop PC.
- The LLM never acts by itself: it proposes, the code checks the action and its arguments against the plugin's schema, the requester must be a known person, and the same person must confirm out loud.
- Degraded mode is code, not prompt: 3 failures in a row → announced once, no more LLM calls, probe every 5 minutes. Also triggered at startup if the LLM does not answer.

### Voice: audio files → Phonon → Piper

- Fixed sentences can be your own recordings (`[voice.phrases]`); everything generated is cached, so a sentence is synthesized once.
- **Phonon 40m French** (Gradium, released 2026-10-20): best French voice we found, Rust runtime (`ptts`, no PyTorch). Its weights are gated on Hugging Face and **non-commercial** (CC BY-NC 4.0), so the bot falls back automatically when it cannot download them.
- **Piper** (`fr_FR-siwis-medium`): small, fast on a CPU, works out of the box. GPL-3: run as a separate program, never imported, so this repository stays MIT.
- Not chosen: Kyutai Pocket TTS (needs PyTorch, its French model is gated too), Kokoro-82M (a single French voice).

### Capture: tsclientlib

| Option | Pros | Cons |
|---|---|---|
| **tsclientlib (chosen)** | free (MIT/Apache), maintained, separate voice per person, speaks too, no GUI | reverse-engineered protocol: a server update can break it |
| Official client + C plugin under Xvfb | official API | proprietary binary that cannot be shipped in an image, Qt + Xvfb ≈ 300 MB of RAM |
| ServerQuery | official | no access to voice |

Claims we could not verify and did not use: a Rust crate named `tsclient-rs`, a bot named "TeamSpeakClaw". The test server sends TeamSpeak 6 fields; tsclientlib ignores them with a warning and works.

### Load (measured)

- Bot container with Parakeet: about 950 MB of RAM; about 1 s of CPU per sentence on 3 cores.
- LLM: run it on a bigger machine if the bot lives on a small server (Qwen3.5-4B Q4 ≈ 3 GB of RAM).

## 5. Design decisions that differ from a naive spec

1. **Identity by server group.** A group is usually shared: it describes a role. The UID identifies a person; groups are the fallback.
2. **An LLM listening to everything.** Too heavy and invents actions. It is only called on the wake word or to settle a pending confirmation, and only known people can trigger actions.
3. **Consent.** Transcribing voices is processing personal data (GDPR); in France recording private speech without consent is an offence (Code pénal, art. 226-1). The bot announces itself in its nickname, supports opt-out by UID, never captures whispers and never stores audio.
4. **The core has no business rules.** "The bot cannot receive points" belongs to the plugin: the core gives it the bot's UID (`on_ready`).
