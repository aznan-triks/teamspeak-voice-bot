"""Talking to the bot: wake word → LLM → spoken confirmation → plugin action. Plus degraded mode."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from . import plugins
from .plugins import ActionEvent, ProposedAction

log = logging.getLogger(__name__)
# "Yes" and "no" are recognised without the LLM: faster, and it still works when the LLM is slow.
YES = re.compile(r"^\W*(oui|ouais|yes|vas-y|valide|je valide|confirme|d'accord|ok|go)\b", re.I)
NO = re.compile(r"^\W*(non|nan|no|annule|stop|laisse tomber)\b", re.I)
TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}


def valid(args, schema: dict) -> bool:
    """JSON Schema subset: object with `properties` (type, enum) and `required`; unknown keys are refused."""
    if not isinstance(args, dict):
        return False
    props = schema.get("properties", {})
    if any(k not in args for k in schema.get("required", [])):
        return False
    for key, value in args.items():
        prop = props.get(key)
        if prop is None:
            return False
        kind = prop.get("type")
        if kind and (not isinstance(value, TYPES[kind]) or (isinstance(value, bool) and kind != "boolean")):
            return False
        if "enum" in prop and value not in prop["enum"]:
            return False
    return True


class Router:
    def __init__(self, cfg: dict, llm, bot, loaded: list):
        self.cfg, self.llm, self.bot, self.loaded = cfg, llm, bot, loaded
        self.prompt = Path(cfg.get("prompt_file", "prompts/system_fr.txt")).read_text(encoding="utf-8")
        self.wake_words = cfg.get("wake_words", ["hector"])
        self.wake = re.compile(r"\b(" + "|".join(map(re.escape, self.wake_words)) + r")\b", re.I)
        self.actions: dict = {}
        self.pending: dict | None = None
        self.failures, self.degraded, self.next_probe = 0, False, 0.0

    def start(self, now: float | None = None) -> None:
        """Once connected: collect the plugins' actions and check that the LLM answers."""
        self.actions = {a.name: (p, a) for p in self.loaded if hasattr(p, "actions") for a in p.actions()}
        if not self.llm.ping():
            self._degrade(time.monotonic() if now is None else now)

    def handle(self, t, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._expire(now)
        if self.degraded:
            if now >= self.next_probe:
                self._probe(now)
            return  # degraded: keep listening and archiving, take no action
        same = self.pending is not None and self.pending["action"].requested_by.uid == t.speaker.uid
        addressed = bool(self.wake.search(t.text))
        # A plain "oui"/"non" settles it at once; "Non Hector, plutôt deux points" goes to the LLM.
        if same and not addressed and YES.match(t.text):
            return self._finish(True)
        if same and not addressed and NO.match(t.text):
            return self._finish(False)
        if addressed:
            plugins.dispatch(self.loaded, "on_command", t)
        if not (addressed or same):
            return
        reply = self._ask(now)
        if reply is None:
            return
        kind = reply.get("type")
        if kind in ("answer", "clarify", "explain") and isinstance(reply.get("text"), str):
            self.bot.say(reply["text"])
        elif kind == "propose":
            self._propose(reply, t, now)
        elif kind == "confirm" and same:
            self._finish(reply.get("decision") == "oui")

    def user_message(self) -> str:
        """What the LLM gets besides the system prompt: actions, context, pending action, recent lines."""
        contexts = {}
        for p in self.loaded:
            if hasattr(p, "context"):
                try:
                    contexts.update(p.context())
                except Exception:
                    log.exception("plugin %s failed in context", type(p).__module__)
        me = self.bot.me.nickname if self.bot.me else "bot"
        pending = {}
        if self.pending:
            a = self.pending["action"]
            pending = {"action": a.name, "args": a.args, "requested_by": a.requested_by.identity}
        return "\n".join([
            "ACTIONS :", json.dumps([{"name": a.name, "description": a.description, "params": a.params} for _, a in self.actions.values()], ensure_ascii=False),
            "CONTEXTE :", json.dumps({"bot": me, "mots_d_eveil": self.wake_words, **contexts}, ensure_ascii=False),
            "EN ATTENTE :", json.dumps(pending, ensure_ascii=False),
            "TRANSCRIPTION :", *self.bot.window,
        ])

    def _ask(self, now: float) -> dict | None:
        try:
            reply = self.llm.chat(self.prompt, self.user_message())
        except Exception as error:
            self.failures += 1
            log.warning("LLM failure %d/%d: %s", self.failures, self.cfg.get("max_failures", 3), error)
            if self.failures >= self.cfg.get("max_failures", 3):
                self._degrade(now)
            return None
        self.failures = 0
        log.info("LLM: %s", json.dumps(reply, ensure_ascii=False))
        return reply

    def _propose(self, reply: dict, t, now: float) -> None:
        entry = self.actions.get(reply.get("action"))
        confirm = reply.get("confirm_text")
        if entry is None or not valid(reply.get("args"), entry[1].params) or not isinstance(confirm, str) or not confirm.strip():
            log.warning("invalid proposal ignored: %s", reply)
            self.bot.say("invalid")
            return
        if t.speaker.identity is None:  # an unknown person can never trigger an action
            self.bot.say("unknown_speaker")
            return
        if self.pending:
            self._close("replaced")
        action = ProposedAction(entry[1].name, reply["args"], confirm, t.speaker, (t.archive_id,))
        row = self.bot.archive.add_action(action, int(time.time() * 1000))
        seconds = self.bot.say(confirm)
        # The waiting time starts once the question has been read out.
        self.pending = {"action": action, "row": row, "plugin": entry[0], "deadline": now + seconds + self.cfg.get("confirm_timeout_s", 20)}
        plugins.dispatch(self.loaded, "on_action", ActionEvent(action, "proposed"))

    def _close(self, status: str, result: str | None = None) -> dict:
        p, self.pending = self.pending, None
        self.bot.archive.set_action_status(p["row"], status, result)
        plugins.dispatch(self.loaded, "on_action", ActionEvent(p["action"], status, result))
        return p

    def _finish(self, yes: bool) -> None:
        if not yes:
            self._close("cancelled")
            self.bot.say("cancelled")
            return
        p = self.pending
        try:
            result = p["plugin"].execute(p["action"])
        except Exception:
            log.exception("action %s failed", p["action"].name)
            self._close("failed")
            self.bot.say("failed")
            return
        self._close("done", result)
        if result:
            self.bot.say(result)

    def _expire(self, now: float) -> None:
        if self.pending and now > self.pending["deadline"]:
            log.info("no confirmation for %s: cancelled", self.pending["action"].name)
            self._close("expired")

    def _degrade(self, now: float) -> None:
        if not self.degraded:
            self.degraded = True
            log.warning("LLM unreachable: degraded mode (transcription and archive keep running)")
            self.bot.say("degraded")  # announced once, on the way in
        self.next_probe = now + self.cfg.get("probe_interval_s", 300)

    def _probe(self, now: float) -> None:
        if self.llm.ping():
            self.degraded, self.failures = False, 0
            log.info("LLM reachable again: degraded mode off")
            self.bot.say("restored")
        else:
            self.next_probe = now + self.cfg.get("probe_interval_s", 300)
