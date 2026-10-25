"""Wake word, spoken confirmation, degraded mode and the LLM client, with a fake LLM and a fake voice."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tsbot.archive import Archive
from tsbot.llm import LLM, parse_reply
from tsbot.plugins import Action, Speaker, Transcription
from tsbot.router import Router, valid
from tsbot.voice import Bot

ALICE = Speaker(3, "alice=", "al", ("Modo",), "Alice")
BOB = Speaker(4, "bob=", "bob", (), "Bob")
NOBODY = Speaker(5, "x=", "x", (), None)
SCHEMA = {
    "type": "object",
    "properties": {"player": {"type": "string", "enum": ["Alice", "Bob"]}, "points": {"type": "integer"}},
    "required": ["player", "points"],
}
PROPOSE = {"type": "propose", "action": "scores.add", "args": {"player": "Bob", "points": 1}, "confirm_text": "Un point pour Bob, je valide ?"}


class FakeLLM:
    def __init__(self, *replies, up=True):
        self.replies, self.up, self.calls = list(replies), up, 0

    def ping(self):
        return self.up

    def chat(self, system, user):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeVoice:
    phrases = {}

    def __init__(self):
        self.said = []

    def say(self, what):
        self.said.append(what)
        return what, 1.0


class Scores:
    def __init__(self):
        self.done = []

    def actions(self):
        return [Action("scores.add", "Ajoute des points", SCHEMA)]

    def context(self):
        return {"joueurs": ["Alice", "Bob"]}

    def execute(self, action):
        self.done.append(action.args)
        return "C'est fait."


def utterance(speaker, text, n=[0], started_ms=None):
    n[0] += 1
    return Transcription(n[0], speaker, int(time.time() * 1000) if started_ms is None else started_ms, 1000, text)


class WindowTest(unittest.TestCase):
    def test_conversation_is_trimmed_by_chunks(self):
        bot = Bot(None, Archive(":memory:"), window=20)
        for i in range(30):
            bot.heard(utterance(BOB, f"phrase {i}"))
        self.assertEqual((len(bot.window), bot.window[0]), (30, "[Bob] phrase 0"))  # same start: the LLM reuses its cache
        bot.heard(utterance(BOB, "phrase 30"))
        self.assertEqual((len(bot.window), bot.window[0], bot.window[-1]), (20, "[Bob] phrase 11", "[Bob] phrase 30"))


class RouterTest(unittest.TestCase):
    def setup(self, llm, prompt_file="prompts/system_fr.txt"):
        self.voice, self.plugin = FakeVoice(), Scores()
        self.archive = Archive(":memory:")
        self.bot = Bot(self.voice, self.archive)
        self.bot.me = Speaker(9, "bot=", "Jarvis", (), None)
        self.router = Router({"wake_words": ["jarvis"], "prompt_file": prompt_file}, llm, self.bot, [self.plugin])
        self.router.start(now=0)

    def hear(self, speaker, text, now=1.0, started_ms=None):
        t = utterance(speaker, text, started_ms=started_ms)
        self.bot.heard(t)
        self.router.handle(t, now=now)

    def statuses(self):
        return [r[0] for r in self.archive.db.execute("SELECT status FROM actions ORDER BY id")]

    def test_propose_then_yes_from_same_person(self):
        llm = FakeLLM(PROPOSE)
        self.setup(llm)
        self.hear(ALICE, "on parle de tout et de rien")  # not addressed: no LLM call
        self.assertEqual(llm.calls, 0)
        self.hear(ALICE, "Jarvis, un point pour Bob")
        self.hear(BOB, "oui")  # someone else cannot confirm
        self.assertEqual(self.plugin.done, [])
        self.hear(ALICE, "Oui, vas-y")  # recognised without calling the LLM again
        self.assertEqual((llm.calls, self.plugin.done), (1, [{"player": "Bob", "points": 1}]))
        self.assertEqual(self.voice.said, ["Un point pour Bob, je valide ?", "C'est fait."])
        self.assertEqual(self.statuses(), ["done"])

    def test_no_cancels_and_silence_expires(self):
        self.setup(FakeLLM(PROPOSE, PROPOSE))
        self.hear(ALICE, "jarvis un point pour Bob")
        self.hear(ALICE, "non laisse tomber")
        self.hear(ALICE, "jarvis un point pour Bob", now=2)
        self.hear(BOB, "bref", now=60)  # 20 s timeout passed
        self.assertEqual(self.statuses(), ["cancelled", "expired"])
        self.assertEqual(self.plugin.done, [])

    def test_waiting_starts_after_a_slow_llm(self):
        class SlowLLM(FakeLLM):
            def chat(self, system, user):
                time.sleep(1)
                return super().chat(system, user)

        self.setup(SlowLLM(PROPOSE))
        self.router.cfg["confirm_timeout_s"] = 0.5
        self.hear(ALICE, "jarvis un point pour Bob", now=1)
        self.hear(ALICE, "oui", now=3)  # 1 s of LLM + 1 s of speech + 0.5 s to answer: still in time
        self.assertEqual(self.statuses(), ["done"])

    def test_warm_up_reads_the_prompt_once_at_start(self):
        llm = FakeLLM({"type": "ignore"}, RuntimeError("down"))
        self.setup(llm)
        self.assertEqual(llm.calls, 0)  # off by default
        self.router.cfg["warm_up"] = True
        self.router.start(now=0)
        self.router.start(now=0)  # a failed warm-up is not a failure of the bot
        self.assertEqual((llm.calls, self.router.degraded, self.router.failures), (2, False, 0))

    def test_people_can_go_on_talking_without_the_name(self):
        answer = {"type": "answer", "text": "Salut !"}
        llm = FakeLLM(answer, {"type": "ignore"})
        self.setup(llm)
        self.router.cfg["follow_up_s"] = 30
        self.hear(ALICE, "jarvis ça va ?", now=1)
        self.assertIn("APPELÉ PAR SON NOM : oui", self.router.user_message())  # called by name: it must answer
        self.hear(BOB, "Alice tu lances ?", now=5)  # someone else chatting: left alone
        self.assertEqual(llm.calls, 1)
        self.hear(ALICE, "et toi t'en penses quoi", now=10)  # the same person, within 30 s: the LLM decides
        self.assertIn("CONVERSATION EN COURS : oui", self.router.user_message())
        self.assertIn("APPELÉ PAR SON NOM : non", self.router.user_message())
        self.hear(ALICE, "bon on lance la partie", now=100)  # long after: not sent to the LLM
        self.assertEqual(llm.calls, 2)

    def test_the_same_answer_is_never_said_twice(self):
        answer = {"type": "answer", "text": "Salut !"}
        self.setup(FakeLLM(answer, answer))
        self.hear(ALICE, "jarvis ça va ?", now=1)
        self.hear(ALICE, "jarvis tu m'entends ?", now=5)
        self.assertEqual(self.voice.said, ["Salut !"])

    def test_an_answer_differing_only_by_punctuation_is_not_said_again(self):
        self.setup(FakeLLM({"type": "answer", "text": "Ola, je suis là."}, {"type": "answer", "text": "Ola ! Je suis là"}))
        self.hear(ALICE, "jarvis ça va ?", now=1)
        self.hear(ALICE, "jarvis tu m'entends ?", now=5)
        self.assertEqual(self.voice.said, ["Ola, je suis là."])

    def test_same_answer_in_other_words_and_relaunch_questions(self):
        # Real exchange of 2026-10-25: the same answer three times, each ending with a question to relaunch.
        self.setup(FakeLLM(
            {"type": "answer", "text": "Mammouth est le premier Scatophile avec 54 points. Et toi, tu es quoi dans ce classement ?"},
            {"type": "answer", "text": "Le premier Scatophile, c'est Mammouth avec 54 points. Quant à toi, Ilker, tu es où ?"},
            {"type": "clarify", "text": "Tu parles de qui ?"}))
        self.hear(ALICE, "jarvis c'est qui le premier Scatophile ?", now=1)
        self.hear(ALICE, "jarvis c'est à toi que je parlais", now=5)
        self.hear(ALICE, "jarvis donne des points", now=9)
        self.assertEqual(self.voice.said, ["Mammouth est le premier Scatophile avec 54 points.", "Tu parles de qui ?"])

    def test_lines_said_while_the_bot_was_thinking_are_not_answered_again(self):
        llm = FakeLLM({"type": "answer", "text": "Salut !"}, {"type": "answer", "text": "Encore ?"})
        self.setup(llm)
        self.router.cfg["follow_up_s"] = 30
        before = int(time.time() * 1000) - 5000
        self.hear(ALICE, "jarvis ça va ?", now=1)
        self.hear(ALICE, "jarvis t'es là ou quoi ?", now=2, started_ms=before)  # queued during the first answer
        self.assertEqual(llm.calls, 1)
        self.hear(BOB, "jarvis et moi alors ?", now=3, started_ms=before)  # someone else still gets an answer
        self.assertEqual(llm.calls, 2)

    def test_a_lone_word_does_not_go_on_the_conversation(self):
        llm = FakeLLM({"type": "answer", "text": "Salut !"})
        self.setup(llm)
        self.router.cfg["follow_up_s"] = 30
        self.hear(ALICE, "jarvis ça va ?", now=1)
        self.hear(ALICE, "Merci.", now=5)  # often a noise misheard: no LLM call
        self.assertEqual(llm.calls, 1)

    def test_personality_is_added_to_the_prompt(self):
        self.setup(FakeLLM())
        router = Router({"wake_words": ["jarvis"], "personality": "Tu es drôle."}, FakeLLM(), self.bot, [])
        self.assertTrue(router.prompt.endswith("PERSONNALITÉ :\nTu es drôle.\n"))

    def test_correction_with_wake_word_goes_to_the_llm(self):
        llm = FakeLLM(PROPOSE, dict(PROPOSE, args={"player": "Alice", "points": 2}, confirm_text="Deux points pour Alice ?"))
        self.setup(llm)
        self.hear(ALICE, "jarvis un point pour Bob")
        self.hear(ALICE, "Non Jarvis, plutôt deux points pour Alice")
        self.assertEqual(llm.calls, 2)
        self.assertEqual(self.statuses(), ["replaced", "proposed"])

    def test_invalid_or_unknown_speaker_never_acts(self):
        bad = dict(PROPOSE, args={"player": "Robert", "points": 1})
        self.setup(FakeLLM(bad, PROPOSE))
        self.hear(ALICE, "jarvis un point pour Robert")
        self.hear(NOBODY, "jarvis un point pour Bob")
        self.assertEqual(self.voice.said, ["invalid", "unknown_speaker"])
        self.assertEqual(self.statuses(), [])

    def test_degraded_mode_announced_once_then_restored(self):
        llm = FakeLLM(OSError("down"), ValueError("bad json"), OSError("down"))
        self.setup(llm)
        for i in range(5):
            self.hear(ALICE, "jarvis tu m'entends ?", now=1 + i)
        self.assertEqual(llm.calls, 3)  # no LLM call once degraded
        self.assertEqual(self.voice.said, ["degraded"])
        self.hear(ALICE, "jarvis ?", now=400)  # probe after 5 minutes: LLM is back
        self.assertEqual(self.voice.said, ["degraded", "restored"])

    def test_llm_down_at_start(self):
        self.setup(FakeLLM(up=False))
        self.assertEqual(self.voice.said, ["degraded"])


class ScoreboardExampleTest(unittest.TestCase):
    def test_points_add_up_and_survive_restart(self):
        import tempfile
        from pathlib import Path

        from tsbot.plugins import ProposedAction
        from tsbot.plugins_examples.scoreboard import Plugin

        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"path": str(Path(tmp) / "s.json"), "players": ["Bob"], "games": ["Fléchettes"]}
            add = lambda n: ProposedAction("scoreboard.add_points", {"player": "Bob", "game": "Fléchettes", "points": n}, "?", ALICE, ())
            self.assertTrue(valid(add(1).args, Plugin(cfg).actions()[0].params))
            self.assertEqual(Plugin(cfg).execute(add(1)), "C'est noté : Bob a maintenant 1 point sur Fléchettes.")
            self.assertEqual(Plugin(cfg).execute(add(2)), "C'est noté : Bob a maintenant 3 points sur Fléchettes.")


class HelpersTest(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(valid({"player": "Bob", "points": 2}, SCHEMA))
        self.assertFalse(valid({"player": "Bob"}, SCHEMA))  # missing
        self.assertFalse(valid({"player": "Bob", "points": True}, SCHEMA))  # bool is not an integer
        self.assertFalse(valid({"player": "Bob", "points": 1, "x": 1}, SCHEMA))  # unknown key
        self.assertFalse(valid("Bob", SCHEMA))

    def test_parse_reply(self):
        self.assertEqual(parse_reply('<think>hmm</think>```json\n{"type":"ignore"}\n```'), {"type": "ignore"})
        with self.assertRaises(ValueError):
            parse_reply("pas de JSON ici")

    def test_llm_client_over_http(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen["auth"] = self.headers.get("Authorization")
                answer = {"choices": [{"message": {"content": '{"type":"answer","text":"Salut"}'}}]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(answer).encode())

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            llm = LLM({"base_url": f"http://127.0.0.1:{server.server_port}/v1/", "model": "m", "api_key": "k"})
            self.assertEqual(llm.chat("sys", "user"), {"type": "answer", "text": "Salut"})
            self.assertEqual((seen["auth"], seen["body"]["response_format"], seen["body"]["model"]), ("Bearer k", {"type": "json_object"}, "m"))
        finally:
            server.shutdown()
            server.server_close()


class ThinkingTest(unittest.TestCase):
    def test_thinking_switch_is_sent_only_when_chosen(self):
        self.assertEqual(LLM({"base_url": "x", "thinking": "off"}).extra, {"thinking": {"type": "disabled"}})
        self.assertEqual(LLM({"base_url": "x", "thinking": "auto"}).extra, {})
        self.assertEqual(LLM({"base_url": "x"}).extra, {})


if __name__ == "__main__":
    unittest.main()
