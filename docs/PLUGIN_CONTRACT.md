# Plugin contract

A plugin is an importable Python module that defines `class Plugin`. It is loaded when its name is listed in `[plugins].enabled`, and it receives its own `[plugins."<module>"]` config section as a `dict`. The core contains no business logic: anything tied to an outside application (scores, accounts, rules) lives in a plugin, in its own repository.

## v1: available now

```python
class Plugin:
    def __init__(self, config: dict): ...
    def on_ready(self, me: Speaker) -> None: ...                # the bot's own profile, once connected
    def on_transcription(self, t: Transcription) -> None: ...   # every archived utterance
```

Types (`tsbot/plugins.py`):

```python
Speaker(client_id: int, uid: str | None, nickname: str,
        server_groups: tuple[str, ...], identity: str | None)
Transcription(archive_id: int, speaker: Speaker, started_ms: int,
              duration_ms: int, text: str)
```

How plugins are called:
- Both methods are optional.
- Calls are synchronous, in the order of `enabled`, on the main thread: a slow hook delays the next transcription. Do long input/output (HTTP calls…) in your own thread.
- An exception raised by a plugin is logged and ignored: transcription and archiving go on.
- `speaker.identity` is `None` for an unknown person. A plugin that changes data must refuse unknown people.
- `on_ready` gives the bot's UID: a plugin that hands out something (points…) uses it to exclude the bot.

## v2: designed, not built yet

The plugin declares what it can do; the core handles the LLM, the spoken confirmation and the voice.

```python
@dataclass(frozen=True)
class Action:
    name: str          # unique, prefixed by the plugin: "scores.add_points"
    description: str   # read by the LLM
    params: dict       # JSON Schema of the arguments

@dataclass(frozen=True)
class ProposedAction:
    name: str
    args: dict
    confirm_text: str                # sentence read back to the person who asked
    requested_by: Speaker
    transcript_ids: tuple[int, ...]  # archive rows that led to it

class Plugin:
    def attach(self, bot: "BotApi") -> None: ...            # before on_ready
    def actions(self) -> list[Action]: ...
    def context(self) -> dict[str, list[str]]: ...          # valid names, injected into the prompt
    def execute(self, action: ProposedAction) -> str: ...   # returns the sentence to say

class BotApi:
    def say(self, sound: str) -> None: ...  # audio file (preferred) or text (text-to-speech)
```

How an action flows:
1. A transcription starts with the wake word → the core calls the LLM with the `actions()` and `context()` of every plugin.
2. The LLM answers `propose`. The core checks: declared action, arguments match the schema, known person.
3. The bot reads `confirm_text` aloud and waits 20 s for "yes" or "no" from the **same UID**.
4. "Yes" → `execute()`; the returned sentence is read aloud. Silence or "no" → cancelled and logged.

What `execute()` is responsible for: checking that the person is allowed to do it, making the call idempotent (a repeated confirmation must not count twice), calling the application's API, and never changing data from `on_transcription`. Test plugins against a fake server, never against real data.

## Minimal example

`tsbot/plugins_examples/echo.py` appends every transcription to a file. For your own plugins, use the `plugins/` folder: `docker-compose.yml` mounts it and adds it to `PYTHONPATH`.

```toml
[plugins]
enabled = ["my_plugin"]          # ./plugins/my_plugin.py

[plugins."my_plugin"]
api_url = "http://localhost:3000"
```
