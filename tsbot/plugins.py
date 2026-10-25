"""Plugin contract (v1). Full specification: docs/PLUGIN_CONTRACT.md."""
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


def load(cfg: dict) -> list:
    """Create `module.Plugin(config)` for every module listed in [plugins].enabled."""
    section = cfg.get("plugins", {})
    return [importlib.import_module(name).Plugin(section.get(name, {})) for name in section.get("enabled", [])]


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
