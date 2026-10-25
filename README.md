# teamspeak-voice-bot

A bot that joins your TeamSpeak server, listens to a channel, knows **who** is talking, writes down **what** they say, and saves everything in a small database. You can also **talk to it**: "Hector, one point for Bob on darts" → the bot asks "I add one point for Bob, OK?" → you say "yes" → it's done, and it answers out loud.

It runs on a normal PC or a small home server, on the CPU, without a graphics card.

## What works today

Everything below was tested on a real TeamSpeak server (October 2026).

| Feature | How |
|---|---|
| Joins a TeamSpeak 3 server (TeamSpeak 6 servers work too) | small Rust client built on [tsclientlib](https://github.com/ReSpeak/tsclientlib) |
| Hears each person separately, cuts speech into sentences | TeamSpeak already sends one voice stream per person |
| Knows who is talking | TeamSpeak UID or server group → a name you choose |
| Speech-to-text (25 European languages) | [NVIDIA Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) |
| Saves everything (SQLite), exports to JSON lines | built in |
| You talk to it with a wake word ("Hector, ...") | any OpenAI-compatible LLM; tested with [llama.cpp](https://github.com/ggml-org/llama.cpp) + Qwen3.5-4B |
| Every action is confirmed out loud first | "yes" / "no" from the same person |
| It talks back | your audio files first, then [Phonon](https://github.com/gradium-ai/xn-ptts) (best French voice), or [Piper](https://github.com/OHF-Voice/piper1-gpl) automatically |
| Degraded mode | LLM down → says so once, keeps listening and saving, stops acting |
| Plugins: your code reacts to sentences and adds voice actions | see [Write a plugin](#write-a-plugin) |

## ⚠️ Privacy first

This bot writes down what people say. That is personal data, and in many countries recording people without telling them is illegal.

- **Tell the people on your server** before you run it.
- Keep a nickname that says the bot is recording (the example config does).
- People who say no: put their UID in `privacy.opt_out_uids`, their voice is then ignored completely.
- Whispers are never recorded. Audio is never saved, only the text.

## How it works (short version)

TeamSpeak sends each person's voice as a separate stream, and only while that person talks. So the bot gets "who" and "where the sentence ends" for free.

1. A small TeamSpeak client written in Rust (the **bridge**) receives the voices and can also speak.
2. A Python program turns each sentence into text, saves it and passes it to your plugins.
3. When a sentence starts with the wake word, it asks an LLM what to do. The LLM can only answer, ask for details, or propose an action that a plugin declared. The bot reads the proposal out loud and waits for "yes".

More details and the reasons behind each choice: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Install

You only need **Docker**:
- Windows or macOS: install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it.
- Linux (Ubuntu): install [Docker Engine](https://docs.docker.com/engine/install/ubuntu/).

### 1. Download the bot

```bash
git clone https://github.com/aznan-triks/teamspeak-voice-bot.git
```

```bash
cd teamspeak-voice-bot
```

### 2. Create your config

```bash
cp config.example.toml config.toml
```

Open `config.toml` in a text editor. The only line you must change is `address` in the `[teamspeak]` section: put your server's address there. Every other setting is explained in the file.

### 3. Add your secrets

Create a file named `.env` in the same folder:

```
TS_SERVER_PASSWORD=your-server-password
TS_CHANNEL_PASSWORD=
HF_TOKEN=
LLM_API_KEY=
```

Leave a line empty if you do not need it. This file stays on your computer: git ignores it, so you cannot publish a password by mistake.

### 4. Start the bot

```bash
docker compose up -d --build
```

The first start takes a few minutes: Docker builds the bot, then the bot downloads its speech model (about 700 MB).

### 5. Check that it works

```bash
docker compose logs -f tsbot
```

You should see `connected as Hector (recording)`. Join the bot's channel and say something: a line like `[YourName] Hello everyone` appears a second or two later. Press `Ctrl+C` to stop watching the logs (the bot keeps running).

To stop the bot:

```bash
docker compose down
```

**On Linux**, the bot runs as user 1000. If it cannot write to `data/`, run `sudo chown -R 1000 data`.

## Talk to the bot

The bot needs an LLM server to understand requests. Any OpenAI-compatible server works. The tested setup is **llama.cpp** with **Qwen3.5-4B** on the CPU of a regular PC: 2 to 5 seconds per answer, 7 out of 7 test requests understood correctly.

### 1. Start an LLM server

On the PC that will run the LLM (it can be the same machine as the bot, but a small server is better off sending this work to a bigger PC):

```bash
docker run -d --name llm --restart unless-stopped -p 8080:8080 -v llm-models:/root/.cache/huggingface ghcr.io/ggml-org/llama.cpp:server -hf unsloth/Qwen3.5-4B-GGUF:Q4_K_M --no-mmproj --host 0.0.0.0 --port 8080 -c 8192 --jinja --reasoning-budget 0 -ub 128 -cms 64 --api-key choose-a-long-password
```

The first start downloads the model (about 2.7 GB). It uses about 3 GB of RAM.

`-ub 128 -cms 64` matter on a small CPU: Qwen3.5 can only reuse what it already read from saved checkpoints, and these two options save them often. On an Intel i5-1135G7 (4 cores), a follow-up question takes 5 to 8 seconds instead of 11 to 16.

A small PC with an Intel graphics chip can do better: use the `ghcr.io/ggml-org/llama.cpp:server-vulkan` image, add `--device /dev/dri` and `-ngl 99`. On the same i5-1135G7 it reads requests twice as fast; in a busy channel the average answer went from 14 to 7 seconds. With rootless Podman, the user needs the `render` group and the `crun` runtime with `--group-add keep-groups` (the default `runc` drops that group).

### 2. Point the bot to it

In `config.toml`, section `[llm]`:
- `enabled = true`
- `base_url = "http://host.docker.internal:8080/v1"` if the LLM runs on the same machine, otherwise `"http://IP-OF-THAT-PC:8080/v1"`.

In `.env`: `LLM_API_KEY=choose-a-long-password` (the same as `--api-key` above). Then restart: `docker compose restart`.

### 3. Talk

- Start with the wake word: **"Hector, ..."**. Pick another word in `wake_words` if you like, but test it: speech-to-text must write it correctly. "Jarvis" often came out as "J'arrive" in our tests; Hector, Gaston and Ordinateur came out right every time.
- Ask a question about the conversation ("Hector, who suggested a game earlier?"), or ask for an action a plugin provides.
- For an action, the bot reads it back and asks you to confirm. Only **you** can confirm your request, within 20 seconds: "oui" / "non".
- Only people listed in `[identities]` can trigger actions.
- If the LLM stops answering (3 failures in a row), the bot says once that it switches to degraded mode. It keeps listening and saving, and it checks the LLM again every 5 minutes.

## A better voice (optional)

Out of the box the bot speaks with **Piper**. The best French voice available in October 2026 is **Phonon 40m** (Gradium). The bot uses it automatically as soon as it can download it:

1. Create a free account on [Hugging Face](https://huggingface.co/join).
2. Open [gradium/phonon-40m-fr](https://huggingface.co/gradium/phonon-40m-fr) and accept its terms (approval is instant).
3. Create a token with "read" access in [your Hugging Face settings](https://huggingface.co/settings/tokens) and put it in `.env`: `HF_TOKEN=hf_...`.
4. `docker compose restart`.

Phonon 40m is free for **non-commercial use only** (CC BY-NC 4.0). If you need commercial use, keep Piper (`engine = "piper"` in `[voice]`).

Every sentence is generated once and cached in `data/tts_cache/`. To use your own recordings for the bot's fixed sentences (degraded mode, "cancelled"...), give file paths in `[voice.phrases]`.

Test the voice without waiting for a conversation (with the bot stopped):

```bash
docker compose run --rm tsbot python -m tsbot say "Bonjour tout le monde"
```

## Manage the bot from a web page

The bot comes with a management site, in French, styled like the TeamSpeak 6 client. Start it next to the bot (`docker compose up -d` starts both), then open `http://THIS-MACHINE:8095` from your home network.

- **Log in** by picking your TeamSpeak nickname: the bot sends you a 6-digit code in a private TeamSpeak message. No password to remember.
- **Everyone** sees the sentences the bot transcribed from their own voice, and the list of voice actions.
- **Admins** (members of the server groups in `[web] admin_groups`, or the UIDs in `admin_uids`) also get: a live view of the channel, settings with explanations, identities and people not to transcribe, plugins, statistics, the bot's log, and a box to make the bot say something.
- Settings changed on the site are saved in `data/settings.json`, on top of `config.toml`. The **Appliquer** button restarts the bot (about a minute) so it uses them.
- The site is meant for your home network only: do not open port 8095 on your router.

## Everyday use

### Where things are saved

Everything lives in the `data/` folder:
- `identity.json`: the bot's TeamSpeak identity. Keep it: deleting it gives the bot a new UID, and it loses the server groups you gave it.
- `archive.sqlite3`: every sentence (including the bot's), and every voice action with its outcome. Open it with any SQLite tool, e.g. [DB Browser for SQLite](https://sqlitebrowser.org/).
- `models/`, `tts_cache/`: downloaded models and generated sentences.

### Export the transcriptions

```bash
docker compose exec tsbot python -m tsbot export > archive.jsonl
```

### Put real names on people

```toml
[identities]
"uid:ExampleUid0aBcDeFgHiJkLmNoPqRs=" = "Alice"   # one person
"group:Admin" = "Bob"                             # everyone in this server group
```

- A **UID** is unique to one person: use it whenever you can. You can read someone's UID in the exported archive, or in TeamSpeak (right-click the person → Client Info).
- A **server group** works too, but only if that person is alone in the group.

### Speech-to-text

NVIDIA Parakeet TDT 0.6B v3 understands 25 European languages and detects the language by itself. About 1 s per sentence on 3 CPU cores; the whole bot uses about 1 GB of RAM. It cannot be taught nicknames: if a name keeps coming out wrong, the LLM usually still matches it to the right person (it corrected "Bobe" to "Bob" in our tests).

## Write a plugin

A plugin is a Python file with a class named `Plugin`, in the `plugins/` folder. The simplest one reacts to every sentence:

```python
# plugins/my_plugin.py
class Plugin:
    def __init__(self, config):
        self.config = config          # your section from config.toml

    def on_transcription(self, t):
        if "good night" in t.text.lower():
            print(f"{t.speaker.nickname} is leaving")
```

A plugin can also add **voice actions**: it describes them, the LLM picks one, the person confirms, then the bot calls the plugin. A complete working example is [tsbot/plugins_examples/scoreboard.py](tsbot/plugins_examples/scoreboard.py) (a tiny scoreboard, about 40 lines). Enable plugins in `config.toml`:

```toml
[plugins]
enabled = ["my_plugin", "tsbot.plugins_examples.scoreboard"]

[plugins."tsbot.plugins_examples.scoreboard"]
players = ["Alice", "Bob"]
games = ["Fléchettes", "Rocket League"]
```

Restart the bot (`docker compose restart`). If a plugin crashes, the error is logged and the bot keeps working. Everything a plugin can do: [docs/PLUGIN_CONTRACT.md](docs/PLUGIN_CONTRACT.md).

## Troubleshooting

- **`Unknown argument … client_is_streaming` in the logs**: harmless, these are TeamSpeak 6 features the bot does not use.
- **`Phonon unavailable, using Piper instead`**: normal until you set up [a better voice](#a-better-voice-optional).
- **The bot does not react to "Hector"**: check the logs. If the transcription shows another word, choose a different wake word, or add the misheard spelling to `wake_words`.
- **The bot cannot speak in the channel**: the channel may require talk power. Give the bot's server group enough talk power.
- **Server groups show up as numbers**: give the bot's server group the permission to list server groups (`b_virtualserver_servergroup_list`).
- **The bot cannot connect**: check the address and the password in `.env`, and that UDP port 9987 is reachable from the machine running the bot.

## For developers

Run the tests (Python 3.11 or newer; no TeamSpeak server, model or LLM needed):

```bash
python -m unittest discover -s tests -v
```

The Rust bridge is built inside the Docker image. To build it on its own you need Rust, `libopus-dev` and `pkg-config`, then run `cargo build --release` in `bridge/`. Its stdin/stdout protocol is described in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## License

MIT. The bridge uses tsclientlib (MIT or Apache-2.0). Models are downloaded at runtime and keep their own licenses: Parakeet v3 (CC BY 4.0), Phonon 40m (CC BY-NC 4.0), Piper (GPL-3.0, run as a separate program), Qwen3.5 (Apache 2.0).
