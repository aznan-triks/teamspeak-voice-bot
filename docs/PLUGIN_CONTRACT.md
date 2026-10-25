# Plugin contract

A plugin is an importable Python module that defines `class Plugin`. It is loaded when its name is listed in `[plugins].enabled`, and it receives its own `[plugins."<module>"]` config section as a `dict`. The core contains no business logic: anything tied to an outside application (scores, accounts, rules) lives in a plugin, in its own repository.

Every method below is optional. Types live in `tsbot/plugins.py`.

## Listening

```python
class Plugin:
    def __init__(self, config: dict): ...
    def attach(self, bot) -> None: ...                       # once, before connecting: keep `bot` to speak
    def on_ready(self, me: Speaker) -> None: ...             # the bot's own profile, once connected
    def on_transcription(self, t: Transcription) -> None: ... # every saved sentence
    def on_command(self, t: Transcription) -> None: ...      # a sentence that contains the wake word
```

```python
Speaker(client_id: int, uid: str | None, nickname: str,
        server_groups: tuple[str, ...], identity: str | None)   # identity None = unknown person
Transcription(archive_id: int, speaker: Speaker, started_ms: int,
              duration_ms: int, text: str)
```

`bot.say(text_or_phrase_key)` speaks (and saves the sentence); `bot.me` is the bot's `Speaker`.

## Voice actions

The plugin says what it can do; the core handles the LLM, the spoken confirmation and the voice.

```python
class Plugin:
    def actions(self) -> list[Action]: ...                 # read once, when the bot connects
    def context(self) -> dict[str, list]: ...              # valid names etc., sent to the LLM on every request
    def execute(self, action: ProposedAction) -> str: ...  # after "yes"; returns the sentence to say
    def on_action(self, event: ActionEvent) -> None: ...   # every status change of any action
```

```python
Action(name: str,          # unique, prefixed by the plugin: "scoreboard.add_points"
       description: str,   # read by the LLM
       params: dict)       # JSON Schema of the arguments
ProposedAction(name, args, confirm_text, requested_by: Speaker, transcript_ids)
ActionEvent(action: ProposedAction, status: str, result: str | None)
# status: proposed | replaced | cancelled | expired | failed | done
```

`params` supports this JSON Schema subset, checked by the core before anything is proposed: an object with `properties` (`type`: string, integer, number, boolean; `enum`) and `required`. Unknown keys are refused. Use `enum` for names (players, games...): the LLM then cannot invent one.

How an action flows:
1. A sentence contains the wake word → the core sends the LLM the actions, the contexts and the last 20 lines.
2. The LLM answers `propose`. The core checks: declared action, valid arguments, known person (`identity` not `None`).
3. The bot reads `confirm_text` and waits 20 s (after it finished speaking) for "oui" / "non" from the **same UID**.
4. "Oui" → `execute()`, and the returned sentence is read out. "Non", silence or an error → nothing is done.
5. Every step is saved in the `actions` table of the archive.

What `execute()` is responsible for: checking that the person may do this, making the call idempotent (a repeated confirmation must not count twice), calling the application's API, and never changing data from `on_transcription`. Test plugins against a fake server, never against real data.

## Rules for every hook

- Hooks run one at a time on the main thread: a slow hook delays the next transcription. Do long input/output (HTTP calls…) in your own thread, except in `execute()`, whose answer the bot waits for.
- An exception raised by a plugin is logged and ignored: transcription and archiving go on. A failing `execute()` makes the bot say the action failed.
- Use `on_ready`'s UID to exclude the bot from anything it should not receive (points...).

## Examples

- `tsbot/plugins_examples/scoreboard.py`: a voice action that adds points in a JSON file. Start from this one to connect a real application.
- The README shows a listening-only plugin in five lines.

Your own plugins go in the `plugins/` folder: `docker-compose.yml` mounts it and adds it to `PYTHONPATH`.

```toml
[plugins]
enabled = ["my_plugin"]          # ./plugins/my_plugin.py

[plugins."my_plugin"]
api_url = "http://localhost:3000"
```
