# teamspeak-voice-bot

A bot that joins your TeamSpeak server, listens to a channel, knows **who** is talking, writes down **what** they say (speech-to-text), and saves everything in a small database. You can plug your own code into it to react to what people say.

It runs on a normal PC or a small home server, without a graphics card.

## What works today

| Feature | Status |
|---|---|
| Joins a TeamSpeak 3 server (also tested on a TeamSpeak 6 server) | ✅ tested on a real server |
| Hears each person separately and cuts speech into sentences | ✅ tested live |
| Knows who is talking (TeamSpeak UID or server group → a name you choose) | ✅ |
| Speech-to-text, French by default, any Whisper language (runs on the CPU) | ✅ tested live |
| Saves everything in a SQLite database, exports to JSON lines | ✅ |
| Plugins: your code is called for every sentence | ✅ |
| AI assistant you can talk to ("hey bot, ..."), with spoken confirmation | 📐 designed, not built yet ([architecture](docs/ARCHITECTURE.md)) |
| The bot talks back (pre-recorded sounds, then text-to-speech) | 📐 designed, not built yet |

## ⚠️ Privacy first

This bot writes down what people say. That is personal data, and in many countries recording people without telling them is illegal.

- **Tell the people on your server** before you run it.
- Keep a nickname that says the bot is recording (the example config does).
- People who say no: put their UID in `privacy.opt_out_uids`, their voice is then ignored completely.
- Whispers are never recorded. Audio is never saved, only the text.

## How it works (short version)

TeamSpeak sends each person's voice as a separate stream, and only while that person talks. So the bot gets "who" and "where the sentence ends" for free.

1. A small TeamSpeak client written in Rust (the **bridge**, built on [tsclientlib](https://github.com/ReSpeak/tsclientlib)) receives the voices.
2. It passes each sentence to a Python program, which turns it into text with [faster-whisper](https://github.com/SYSTRAN/faster-whisper).
3. The text is saved, then sent to your plugins.

More details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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

### 3. Add the server password (if your server has one)

Create a file named `.env` in the same folder, with this content:

```
TS_SERVER_PASSWORD=your-server-password
TS_CHANNEL_PASSWORD=
```

This file stays on your computer: git ignores it, so you cannot publish your password by mistake.

### 4. Start the bot

```bash
docker compose up -d --build
```

The first start takes a few minutes: Docker builds the bot, then the bot downloads the speech model (about 480 MB for the default `small` model).

### 5. Check that it works

```bash
docker compose logs -f tsbot
```

You should see `connected as Voice bot (recording)`. Join the bot's channel and say something: a line like `[YourName] Hello everyone` appears a second or two later. Press `Ctrl+C` to stop watching the logs (the bot keeps running).

To stop the bot:

```bash
docker compose down
```

**On Linux**, the bot runs as user 1000. If it cannot write to `data/`, run `sudo chown -R 1000 data`.

## Everyday use

### Where things are saved

Everything lives in the `data/` folder:
- `identity.json`: the bot's TeamSpeak identity. Keep it: deleting it gives the bot a new UID, and it loses the server groups you gave it.
- `archive.sqlite3`: every transcription (open it with any SQLite tool, e.g. [DB Browser for SQLite](https://sqlitebrowser.org/)).
- `models/`: the downloaded speech models.

### Export the transcriptions

```bash
docker compose exec tsbot python -m tsbot export > archive.jsonl
```

You get one line of JSON per sentence: time, duration, who spoke, their server groups, the text.

### Put real names on people

By default the bot shows people's TeamSpeak nicknames. To use stable names, add them in the `[identities]` section of `config.toml`:

```toml
[identities]
"uid:ExampleUid0aBcDeFgHiJkLmNoPqRs=" = "Alice"   # one person
"group:Admin" = "Bob"                                          # everyone in this server group
```

- A **UID** is unique to one person: use it whenever you can. You can read someone's UID in the exported archive, or in TeamSpeak (right-click the person → Client Info).
- A **server group** works too, but only if that person is alone in the group.
- If both match, the UID wins.

### Make it faster or more accurate

- The `small` model is a good default. Measured: 7.8 s of speech transcribed in 1.6 s on 3 desktop CPU cores; live on a real server, about 1 to 1.5 s per sentence.
- On a slow or busy machine, use `model = "base"` (faster, makes more mistakes).
- Add the nicknames and game names people use to `vocabulary`: Whisper spells them better (not always perfectly).

## Write a plugin

A plugin is a Python file with a class named `Plugin`. Put it in the `plugins/` folder:

```python
# plugins/my_plugin.py
class Plugin:
    def __init__(self, config):
        self.config = config          # your section from config.toml

    def on_ready(self, me):
        self.bot_uid = me.uid         # the bot's own profile, once connected

    def on_transcription(self, t):
        # Called after every sentence: t.speaker.nickname, t.speaker.identity, t.text, ...
        if "good night" in t.text.lower():
            print(f"{t.speaker.nickname} is leaving")
```

Then enable it in `config.toml`:

```toml
[plugins]
enabled = ["my_plugin"]

[plugins."my_plugin"]
some_setting = 3
```

Restart the bot (`docker compose restart`). If your plugin crashes, the error is logged and the bot keeps working.

Everything a plugin receives, and what is planned next (actions confirmed by voice): [docs/PLUGIN_CONTRACT.md](docs/PLUGIN_CONTRACT.md).

## Troubleshooting

- **`Unknown argument … client_is_streaming` in the logs**: harmless, these are TeamSpeak 6 features the bot does not use.
- **Server groups show up as numbers**: give the bot's server group the permission to list server groups (`b_virtualserver_servergroup_list`).
- **Nothing gets transcribed**: check the bot is in the same channel as you, and that you talk for more than half a second.
- **The bot cannot connect**: check the address and the password in `.env`, and that UDP port 9987 is reachable from the machine running the bot.

## For developers

Run the tests (Python 3.11 or newer; no TeamSpeak server or speech model needed):

```bash
python -m unittest discover -s tests -v
```

The Rust bridge is built inside the Docker image. To build it on its own you need Rust, `libopus-dev` and `pkg-config`, then run `cargo build --release` in `bridge/`. Running `bridge/target/release/ts-bridge` with `TS_ADDRESS` (and `TS_SERVER_PASSWORD` if needed) prints a `ready` line, then one line per sentence it hears.

## License

MIT. The bridge uses tsclientlib (MIT or Apache-2.0).
