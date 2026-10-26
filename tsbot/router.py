"""Talking to the bot: wake word → LLM → spoken confirmation → plugin action. Plus degraded mode."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from . import plugins
from .plugins import ActionEvent, ProposedAction

from . import SPOKEN

log = logging.getLogger(__name__)
# "Yes" and "no" are recognised without the LLM: faster, and it still works when the LLM is slow.
YES = re.compile(r"^\W*(oui|ouais|yes|vas-y|valide|je valide|confirme|d'accord|ok|go)\b", re.I)
NO = re.compile(r"^\W*(non|nan|no|annule|stop|laisse tomber)\b", re.I)
TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}


def plain(text: str) -> str:
    """Words only: "Ola ! Je suis BoToP" and "Ola, je suis BoToP" are the same answer."""
    return re.sub(r"\W+", " ", text).strip().lower()


def repeats(text: str, before: str) -> bool:
    """Same answer in other words: most words of the shorter one are in the other
    ("Mammouth est premier avec 54 points" / "Le premier, c'est Mammouth avec 54 points")."""
    a, b = ({w for w in plain(s).split() if len(w) > 2} for s in (text, before))
    if min(len(a), len(b)) < 3:
        return plain(text) == plain(before)
    return len(a & b) / min(len(a), len(b)) >= 0.6


def no_relaunch(text: str) -> str:
    """The small model ends its answers with a question to keep talking ("Et toi, t'es où ?"), whatever its
    instructions say: those questions are dropped, as long as something else is left to say."""
    sentences = re.split(r"(?<=[.!?…])\s+", text.strip())
    kept = [s for s in sentences if not s.endswith("?")]
    return " ".join(kept) if kept else text


def changelog(path: str, versions: int) -> str:
    """"## 0.3.0 — date" and its "Plain words" line, for the latest versions: what the bot says when asked what is new."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    found = re.findall(r"^## (.+)\n+\*\*Plain words:\*\* (.+)$", text, re.M)[:versions]
    return "\n".join(f"- {version} : {words}" for version, words in found)


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
        news = changelog(cfg.get("changelog_file", "CHANGELOG.md"), cfg.get("changelog_versions", 3))
        if news:  # in the fixed part of the prompt: read once, cached by the LLM
            self.prompt += "\nNOUVEAUTÉS (ton changelog, du plus récent ; réponds en français) :\n" + news + "\n"
        if cfg.get("personality"):
            self.prompt += "\nPERSONNALITÉ :\n" + cfg["personality"].strip() + "\n"
        self.conversing_until = 0.0  # after the bot answered, people can go on talking to it without its name
        self.asked_back = False  # its last words were a question ("clarify")
        self.started_at = time.time()  # the bot is asked why it left: it was restarted (updates)
        self.partner, self.replied_ms = None, 0  # who the bot last answered, and when it started saying it (Unix ms)
        self.wake_words = cfg.get("wake_words", ["hector"])
        one = [w for w in self.wake_words if " " not in w.strip()]
        two = [w for w in self.wake_words if " " in w.strip()]
        # A two-word name is often a common phrase too ("c'est beau top" heard for "c'est au top"): it only counts
        # at the start of a sentence, after one word at most ("Allô, beau top ?"). Its words may be split by a
        # comma or a full stop ("Top, top." heard live for "top top").
        parts = ([r"\b(?:" + "|".join(map(re.escape, one)) + r")\b"] if one else []) + (
            [r"(?:^|[.!?]\s+)(?:\w+,?\s+)?(?:" + "|".join(r"\W+".join(map(re.escape, w.split())) for w in two) + r")\b"]
            if two else [])
        self.wake = re.compile("|".join(parts) or r"(?!)", re.I)
        self.actions: dict = {}
        self.pending: dict | None = None
        self.failures, self.degraded, self.next_probe = 0, False, 0.0

    def start(self, now: float | None = None) -> None:
        """Once connected: collect the plugins' actions and check that the LLM answers."""
        self.actions = {a.name: (p, a) for p in self.loaded if hasattr(p, "actions") for a in p.actions()}
        if not self.llm.ping():
            self._degrade(time.monotonic() if now is None else now)
        elif self.cfg.get("warm_up", False):
            # The LLM reads the long prompt once now, so the first real question is answered fast.
            try:
                self.llm.chat(self.prompt, self.user_message())
            except Exception as error:
                log.warning("LLM warm-up failed: %s", error)

    def handle(self, t, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._expire(now)
        if self.degraded:
            if now >= self.next_probe:
                self._probe(now)
            return  # degraded: keep listening and archiving, take no action
        # Said before the bot's reply to this person began (queued while the LLM thought): already answered.
        if t.speaker.uid == self.partner and t.started_ms < self.replied_ms:
            return
        same = self.pending is not None and self.pending["action"].requested_by.uid == t.speaker.uid
        addressed = self.addressed = bool(self.wake.search(t.text))
        # A plain "oui"/"non" settles it at once; "Non Hector, plutôt deux points" goes to the LLM, and so does a
        # longer answer: live, "Non, pas un point, mets-en plus" cancelled the action instead of changing it.
        short = len(t.text.split()) <= self.cfg.get("yes_no_max_words", 3)
        if same and not addressed and short and YES.match(t.text):
            return self._finish(True)
        if same and not addressed and short and NO.match(t.text):
            return self._finish(False)
        if addressed:
            plugins.dispatch(self.loaded, "on_command", t)
        # Only the person the bot is talking with may go on without its name: others chatting are left alone.
        # A lone "Merci." or "Ah." (often noise misheard) does not go on the conversation, unless it answers the
        # bot's own question ("Sur quel top ?" → "Mauvais.").
        self.following = (now < self.conversing_until and t.speaker.uid == self.partner
                          and (self.asked_back or len(t.text.split()) >= self.cfg.get("follow_up_min_words", 3)))
        spoken_to = addressed or same or self.following
        if not (spoken_to or (self.cfg.get("watch_for_actions") and self.actions)):
            return
        asked = time.monotonic()
        reply = self._ask(now)
        if reply is None:
            return
        kind = reply.get("type")
        if not spoken_to and kind != "propose":
            return  # nobody talks to it: it only speaks up to propose an action (points...)
        if kind in ("answer", "clarify", "explain") and isinstance(reply.get("text"), str):
            text = no_relaunch(reply["text"]) if kind == "answer" else reply["text"]
            me = f"[{self.bot.me.nickname}] " if self.bot.me else "[bot] "
            mine = [line[len(me):] for line in self.bot.window if line.startswith(me)][-4:]
            if any(repeats(text, s) for s in mine):
                log.info(SPOKEN + "repeated answer: %s", text[:80])  # small models say the same thing again
                # Called by name, silence is worse (live: "botop, j'ai pas compris" got nothing four times in a row):
                # asked once more for something new, and said even if repeated (it was asked to repeat).
                again = self._ask(now, self.cfg.get("repeat_note", "RÉPÉTITION : tu viens déjà de dire ça. Réponds à la "
                                  "dernière ligne avec du neuf ; ne le redis que si on te demande de répéter.")) if addressed else None
                if not (again and again.get("type") in ("answer", "clarify", "explain") and isinstance(again.get("text"), str)):
                    return
                kind, text = again["type"], no_relaunch(again["text"]) if again["type"] == "answer" else again["text"]
            self.replied_ms = int(time.time() * 1000)
            seconds = self.bot.say(text)
            # After a question of its own, the answer may come without its name, even with follow-ups turned off.
            self.asked_back = kind == "clarify"
            wait = max(self.cfg.get("follow_up_s", 30), self.cfg.get("confirm_timeout_s", 20) if self.asked_back else 0)
            self.conversing_until = now + time.monotonic() - asked + seconds + wait
            self.partner = t.speaker.uid
        elif kind == "propose":
            self._propose(reply, t, now, asked, spoken_to)
        elif kind == "confirm" and same:
            self._finish(reply.get("decision") == "oui")

    def user_message(self, note: str = "") -> str:
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
            # Last: a line that changes after the transcription keeps the LLM's cache of what comes before.
            f"BOT : en ligne depuis {int((time.time() - self.started_at) // 60)} min" + (f", {self.cfg['about']}" if self.cfg.get("about") else ""),
            f"CONVERSATION EN COURS : {'oui' if getattr(self, 'following', False) else 'non'}",
            f"APPELÉ PAR SON NOM : {'oui' if getattr(self, 'addressed', False) else 'non'}",
            *([note] if note else []),
        ])

    def _ask(self, now: float, note: str = "") -> dict | None:
        started = time.time()
        try:
            reply = self.llm.chat(self.prompt, self.user_message(note))
        except Exception as error:
            self._timed(started, False)
            self.failures += 1
            log.warning("LLM failure %d/%d: %s", self.failures, self.cfg.get("max_failures", 3), error)
            if self.failures >= self.cfg.get("max_failures", 3):
                self._degrade(now)
            return None
        self._timed(started, True)
        self.failures = 0
        log.info(SPOKEN + "LLM: %s", json.dumps(reply, ensure_ascii=False))
        return reply

    def _timed(self, started: float, ok: bool) -> None:
        """Saved for the statistics page: how long the LLM took."""
        self.bot.archive.add_llm_call(int(started * 1000), int((time.time() - started) * 1000), ok)

    def _propose(self, reply: dict, t, now: float, asked: float, spoken_to: bool = True) -> None:
        entry = self.actions.get(reply.get("action"))
        confirm = reply.get("confirm_text")
        args = reply.get("args")
        # People say yes to what they hear: every name in the action must be in the question (seen live:
        # "Ilker ... ?" read out for a point to someone else).
        if (entry is None or not valid(args, entry[1].params) or not isinstance(confirm, str) or not confirm.strip()
                or any(isinstance(v, str) and v.lower() not in confirm.lower() for v in args.values())):
            log.warning(SPOKEN + "invalid proposal ignored: %s", reply)
            if spoken_to:
                self.bot.say("invalid")
            return
        if t.speaker.identity is None:  # an unknown person can never trigger an action
            if spoken_to:
                self.bot.say("unknown_speaker")
            return
        if self.pending:
            self._close("replaced")
        action = ProposedAction(entry[1].name, reply["args"], confirm, t.speaker, (t.archive_id,))
        row = self.bot.archive.add_action(action, int(time.time() * 1000))
        self.partner, self.replied_ms = t.speaker.uid, int(time.time() * 1000)  # a "oui" said before the question is not a yes
        seconds = self.bot.say(confirm)
        # The waiting time starts once the question has been read out, after the LLM and the voice took their time.
        self.pending = {"action": action, "row": row, "plugin": entry[0], "deadline": now + time.monotonic() - asked + seconds + self.cfg.get("confirm_timeout_s", 20)}
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
        except Exception as error:
            log.exception("action %s failed", p["action"].name)
            self._close("failed", str(error))
            self.bot.say("failed")
            # The real reason, for when it is asked why: live, it made one up ("a hundred points is too much").
            self.bot._remember(f"[{self.bot.me.nickname if self.bot.me else 'bot'}] (raison de l'échec : {error})")
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
