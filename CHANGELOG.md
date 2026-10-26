# Changelog

Newest first. The bot reads the "Plain words" lines of the latest versions, so it can tell what is new when asked.

## 0.3.0 — 2026-10-26

**Plain words:** The bot hears better (Google Gemini writes most sentences), always answers when you call it, knows what it is and how it works, sees when people talk over each other, and the site has a real space for players.

**Technical:**
- Speech-to-text: Gemini (`engine = "gemini"`) for sentences of 1.2 s or more, the local Canary model for short sounds and when Gemini fails; chosen on 18 real sentences compared side by side (`compare_model`). 300 ms of silence around each sentence for the local model; transcription loops cut; Silero keeps 250 ms around speech.
- Conversation: called by name it never stays silent; a reply to its own question needs no name; "non, mets-en plus" changes the action instead of cancelling it; the real reason of a failed action is kept; overlapping speech is marked "[Alice, en même temps que Bob]"; the prompt says what the bot is, which models it runs, and what it keeps.
- Site: player space (stats, opt-out applied at once); names and "make the bot speak" stay with admins; correct "you are in the bot's channel" check; plugin API `/api/plugin/<module>/<path>` (`docs/PLUGIN_CONTRACT.md`, "Site"); lines with what people said never reach the log pages.
- Bridge: reports each channel's name and voice quality.
- Checked with 63 unit tests (also run inside the Docker image) and live on the NUC; not checked: overlap marking on a busy channel.

## 0.2.0 — 2026-10-25

**Plain words:** The bot has a management site, follows the players from channel to channel, speaks only when called by name, answers in about a second, and hears laughter.

**Technical:**
- Management site (`tsbot/web/`): login by TeamSpeak poke, conversation limited to what you could hear, settings, identities, plugins, stats, journal; French and English, WCAG 2.2 AA.
- Speech-to-text: Canary-1B-v2 with French forced, misheard names corrected by sound, laughter tagged by CED-mini, transcript length capped (a 2.7 s sound froze the bot for 55 s).
- Following players across channels, with a second bot when the group splits; answers from DeepSeek in 0.5 to 1.3 s; replies only when called by name.
- Checked with unit tests and live on the NUC.

## 0.1.0 — 2026-10-25

**Plain words:** First working bot: it listens to a TeamSpeak channel, knows who speaks, writes it down, and can do things it is asked out loud after a spoken "yes".

**Technical:**
- Rust bridge (one audio stream per speaker, voice output) and Python orchestrator: speech-to-text, SQLite archive (text only), LLM router, spoken confirmation, degraded mode, plugins.
- Checked with unit tests and a live two-bot test on the test server.
