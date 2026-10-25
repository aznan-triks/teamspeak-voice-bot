"""Plugin contract. Full specification: docs/PLUGIN_CONTRACT.md."""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Speaker:
    client_id: int
    uid: str | None
    nickname: str
    server_groups: tuple[str, ...]
    identity: str | None  # resolved from [identities] in the config; None = unknown person


@dataclass(frozen=True)
class Transcription:
    archive_id: int
    speaker: Speaker
    started_ms: int  # Unix time, milliseconds
    duration_ms: int
    text: str


@dataclass(frozen=True)
class Action:
    """Something a plugin can do when asked out loud. The LLM picks it; a person confirms it."""
    name: str          # unique, prefixed by the plugin: "scores.add_points"
    description: str   # read by the LLM
    params: dict       # JSON Schema of the arguments (subset: see router.valid)


@dataclass(frozen=True)
class ProposedAction:
    name: str
    args: dict
    confirm_text: str
    requested_by: Speaker
    transcript_ids: tuple[int, ...]


@dataclass(frozen=True)
class ActionEvent:
    action: ProposedAction
    status: str  # proposed | replaced | cancelled | expired | failed | done
    result: str | None = None


def load(cfg: dict) -> list:
    """Create `module.Plugin(config)` for every module listed in [plugins].enabled."""
    section = cfg.get("plugins", {})
    loaded = []
    for name in section.get("enabled", []):
        try:  # a plugin that cannot start (its application is down...) must not stop the bot
            loaded.append(importlib.import_module(name).Plugin(section.get(name, {})))
        except Exception:
            log.exception("plugin %s could not start: the bot runs without it", name)
    return loaded


def dispatch(plugins: list, hook: str, event) -> None:
    # A crashing plugin must never stop transcription or archiving.
    for plugin in plugins:
        handler = getattr(plugin, hook, None)
        if handler is None:
            continue
        try:
            handler(event)
        except Exception:
            log.exception("plugin %s failed in %s", type(plugin).__module__, hook)
