"""Example plugin: appends every transcription to a text file. Use it as a minimal template."""
from __future__ import annotations

import datetime


class Plugin:
    def __init__(self, config: dict):
        self.path = config.get("path", "data/echo.log")

    def on_ready(self, me):
        self.me = me  # the bot's own profile: a business plugin uses it to exclude the bot (e.g. from points)

    def on_transcription(self, t):
        when = datetime.datetime.fromtimestamp(t.started_ms / 1000).strftime("%H:%M:%S")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"{when} {t.speaker.identity or t.speaker.nickname}: {t.text}\n")
