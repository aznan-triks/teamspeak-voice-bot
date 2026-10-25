# Architecture

## 1. Overview

```
          TeamSpeak server (UDP 9987)
                     │  Opus voice, one stream per person + server state (clients, groups, channels)
                     ▼
┌──────────── ts-bridge (Rust, tsclientlib) ─────────────┐
│ connection, persistent identity (stable UID)            │
│ AudioHandler: jitter, packet loss, Opus decoding        │
│   PER PERSON                                            │
│ utterance = one person's continuous stream (max 30 s)   │
│ 48 kHz stereo → 16 kHz mono, 16-bit PCM                 │
└───────────────┬─────────────────────────────────────────┘
                │ stdout: one JSON line + PCM   (stdin: commands, v2)
                ▼
┌──────────── tsbot (Python) ────────────────────────────┐
│ opt-out, minimum length → faster-whisper (int8, CPU)    │
│ → identity (UID, else server group) → SQLite → plugins  │
│ v2: LLM router on a wake word, spoken confirmation,     │
│     voice output                                        │
└───────────────┬───────────────────────┬────────────────┘
                ▼                       ▼
        data/archive.sqlite3     your plugins (outside this repo)
                                 → their own APIs
```

Why it is split this way:

- **Who is talking, without speaker recognition.** TeamSpeak carries one Opus stream per person, and every packet says who sent it. The speaker comes from the packet, not from the voice: no AI is needed to tell voices apart, and two people talking at once are transcribed separately.
- **Where a sentence ends, without server-side voice detection.** A TeamSpeak client only sends audio while its user talks (voice activation or push-to-talk). End of stream = end of the utterance. faster-whisper's VAD filter only removes noise inside an utterance.
- **Rust for the protocol, Python for the rest.** tsclientlib is the only free, maintained implementation of the TeamSpeak client protocol (last commit: July 2026). Python has the speech-to-text, LLM and text-to-speech ecosystem, and makes plugins easy to write.
- **A pipe, not a network.** Python starts the bridge and reads its output: no port, no message broker. If Python dies, the bridge can no longer write and stops.

## 2. Repository layout

```
teamspeak-voice-bot/
├── bridge/                  Rust bridge
│   ├── Cargo.toml, Cargo.lock
│   └── src/main.rs
├── tsbot/                   Python orchestrator
│   ├── __main__.py          main loop, frame reader, identities, CLI (run, export)
│   ├── stt.py               faster-whisper + hallucination filter
│   ├── archive.py           SQLite + JSON lines export
│   ├── plugins.py           v1 contract: Speaker, Transcription, loading, isolation
│   └── plugins_examples/echo.py
├── plugins/                 your own plugins (ignored by git)
├── prompts/system_fr.md     system prompt of the LLM router (v2)
├── docs/                    ARCHITECTURE.md, PLUGIN_CONTRACT.md
├── tests/test_pipeline.py
├── config.example.toml
└── Dockerfile, docker-compose.yml, requirements.txt
```

v2 will add `tsbot/llm.py`, `tsbot/voice.py` and `sounds/` (pre-recorded announcements). Nothing is created ahead of time.

## 3. Bridge ↔ orchestrator protocol

**Bridge stdout** (implemented): one JSON line, followed by `samples × 2` bytes of 16-bit little-endian PCM, 16 kHz, mono.

```
{"event":"ready","speaker":{...}}
{"event":"utterance","speaker":{...},"started_ms":1761355442000,"samples":16000}<32000 bytes>
```

`speaker` = `{client_id, uid, nickname, server_groups: [names], channel_id}`. Bridge logs go to stderr.

**Bridge environment variables**: `TS_ADDRESS`, `TS_NICKNAME`, `TS_CHANNEL`, `TS_CHANNEL_PASSWORD`, `TS_SERVER_PASSWORD`, `TS_IDENTITY_FILE`, `TS_MAX_UTTERANCE_S`. SIGINT disconnects cleanly; a closed stdout stops the bridge.

**Bridge stdin** (v2): `{"cmd":"play","samples":N}` followed by N samples of 16-bit PCM, 48 kHz, mono. The bridge encodes them to Opus and sends one packet every 20 ms. Python decodes audio files (with PyAV, already installed by faster-whisper), so announcements can be ogg, mp3 or wav while the bridge stays minimal.

## 4. Code plan

### Rust: ts-bridge

Done:
1. Connect with `Connection::build(addr).name().identity().channel().password()`. The identity (saved as JSON) is created on first start and reused afterwards: same UID, same server permissions.
2. Wait for the initial state, then for the bot itself and the server group names (5 s at most, otherwise numeric ids).
3. `select!` loop: 20 ms tick, network event, Ctrl-C. `S2C` audio packets go to `AudioHandler<u16>`; whispers (`S2CWhisper`) are ignored.
4. On every tick, `fill_buffer_with_proc` yields each person's audio; 48 kHz stereo → 16 kHz mono; end of stream or 30 s → `utterance` frame.
5. stdout is written from a dedicated thread: a slow orchestrator never blocks the network loop (which must answer the server's pings).

v2:
6. Thread reading stdin, queue of PCM to send, `audiopus::coder::Encoder` at 48 kHz mono, 960-sample frames sent on the tick, empty packet at the end (end of speech).
7. Client join/leave events: only once a plugin needs them.

### Python: tsbot

Done: main loop, filters (opt-out, length), speech-to-text, identities, archive, v1 plugins, JSON lines export.

v2, in this order:
1. `voice.py`: `say(path)` decodes the file to 48 kHz mono and sends it to the bridge. Fixed announcements (`sounds/degraded.ogg`, `sounds/restored.ogg`, `sounds/confirmed.ogg`…) are generated once.
2. `llm.py`: OpenAI-compatible client using `urllib` (no SDK): `POST {base_url}/chat/completions`, 20 s timeout. Switching providers = changing `base_url`, `model` and `api_key` in the config (Ollama, LM Studio, llama.cpp server, a hosted service).
   - Triggered only by a wake word (a regular expression on the transcription, e.g. `^(ok |hey )?bot\b`) or while a confirmation is pending. Never on every sentence.
   - Context sent: the last 20 lines, the actions declared by plugins (name, description, JSON Schema of the arguments) and their reference data (valid names).
   - The answer is validated strictly (known type, declared action, valid arguments); anything else is ignored and logged.
3. Degraded mode: a circuit breaker. 3 failures in a row (timeout, HTTP error, invalid JSON) → no more LLM calls, `sounds/degraded.ogg` played once; `GET /models` probe every 5 min; back online → `sounds/restored.ogg`. Transcription and archiving keep running.
4. Confirmation: proposed action → the bot reads `confirm_text` → waits for "yes" or "no" from the **same UID** for 20 s (regular expression first, LLM only when unclear) → `plugin.execute()`; otherwise cancelled.
5. Text-to-speech for dynamic sentences: Piper as a subprocess (voice `fr_FR-siwis-medium`, about 60 MB, faster than real time on a CPU). Kokoro-82M (`kokoro-onnx`) as an option: more natural, heavier on a CPU, one French voice, not yet compatible with Python 3.14. Piper is GPL-3 since `piper1-gpl`: call it as a subprocess, do not import it, so this repository can stay MIT.

### Load on a small server (Intel NUC, i5-1135G7, 16 GB)

- Bridge: a few MB of RAM, negligible CPU.
- Whisper `small` int8: about 1 GB of RAM. If latency is too high, switch to `base`.
- Local LLM on the same machine: not recommended. A quantized 3B model takes 2 to 3 GB of RAM and runs at 5 to 10 tokens/s, competing with Whisper. Use another PC (Ollama, LM Studio) or a hosted service.

## 5. Capturing TeamSpeak audio: options

| Option | Pros | Cons |
|---|---|---|
| **tsclientlib (chosen)** | free (MIT/Apache), maintained, separate voice per person, no GUI | reverse-engineered protocol: a server update can break the connection |
| Official client + C plugin under Xvfb | official API, PCM per person (`onEditPlaybackVoiceDataEvent`) | proprietary binary that cannot be redistributed in an image, Qt + Xvfb ≈ 300 MB of RAM, plugin in C |
| TS3AudioBot (C#) | mature | built to play music, not to transcribe |
| ServerQuery (`ts3` Python package) | official | no access to voice |

Fallback if tsclientlib breaks: official client + plugin. The test server (2026-10-25) sends TeamSpeak 6 fields (`client_is_streaming`): tsclientlib ignores them with a warning and the connection works.

## 6. Design decisions that differ from a naive spec

1. **Identity by server group.** A server group (Guest, Admin…) is usually shared by several people: it describes a role, not a person. The TeamSpeak UID identifies the person. Server groups remain available as a fallback, which is enough when each member has their own group.
2. **Speaker recognition.** Not needed, see §1.
3. **An LLM that "thinks about the conversation" all the time.** Too heavy for a small server, and it invents actions nobody asked for. The LLM is only called on a wake word, and every action needs a spoken confirmation.
4. **A "system prompt for degraded mode".** Impossible: an unavailable LLM does not read its prompt. Degraded mode lives in the code (§4, circuit breaker).
5. **Consent.** Transcribing voices all the time is processing personal data (GDPR in the EU); in France, recording private speech without consent is a criminal offence (Code pénal, article 226-1). Technical answer: a nickname that says the bot records, opt-out by UID, whispers never captured, audio never saved (text only). Telling the people on the server is still required.
6. **"SQLite or JSON lines".** Doing both doubles the code: SQLite, plus a JSON lines export.
7. **"The bot cannot receive points" in the core.** That is a business rule: the core only exposes the bot's UID (`on_ready`), the business plugin excludes it.
8. **Whisper hallucinations.** On silence or noise, Whisper makes up sentences from its training data ("Sous-titres réalisés par la communauté d'Amara.org"), and can loop on repeated text. Filters: known phrases, `no_speech_prob` threshold, compression ratio, and a single decoding temperature (no slow retries).
9. **Everything at once.** The shipped code covers what was tested (capture, transcription, archive). The LLM and voice output are specified precisely, without dead code.
