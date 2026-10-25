"""Following people across channels, and the second bot, without TeamSpeak."""
import unittest

from tsbot.follow import Follower

BOT1, BOT2 = "bot1=", "bot2="


def people(*where, bots=None):
    """where: (identity or None, channel) per person; bots: channel of each running bot."""
    clients = [{"uid": f"p{i}=", "identity": who, "channel_id": ch} for i, (who, ch) in enumerate(where)]
    return clients + [{"uid": uid, "identity": None, "channel_id": ch} for uid, ch in zip((BOT1, BOT2), bots or [])]


class FollowTest(unittest.TestCase):
    def setUp(self):
        self.f = Follower({"move_after_s": 10, "stop_after_s": 60})

    def test_goes_where_most_known_players_are_once_it_is_stable(self):
        clients = people(("Ilker", 5), ("Cotty", 5), (None, 9), (None, 9), (None, 9), bots=[1])
        self.assertEqual(self.f.decide(clients, [BOT1], 0)["move"], {})  # people may still be moving around
        self.assertEqual(self.f.decide(clients, [BOT1], 10)["move"], {0: 5})  # strangers do not count
        self.assertEqual(self.f.decide(clients, [BOT1], 12)["move"], {})  # asked once, not again

    def test_second_bot_when_players_split_then_stopped(self):
        split = people(("Ilker", 5), ("Cotty", 7), bots=[5])
        self.assertTrue(self.f.decide(split, [BOT1], 0)["start"] is False)
        self.assertTrue(self.f.decide(split, [BOT1], 10)["start"])
        out = self.f.decide(people(("Ilker", 5), ("Cotty", 7), bots=[5, 1]), [BOT1, BOT2], 12)
        self.assertEqual(out["move"], {1: 7})  # the first bot stays with Ilker, the second goes to Cotty
        together = people(("Ilker", 7), ("Cotty", 7), bots=[5, 7])
        self.f.decide(together, [BOT1, BOT2], 20)
        out = self.f.decide(together, [BOT1, BOT2], 30)
        self.assertEqual((out["stop"], out["move"]), (True, {0: 7}))  # the second sits where the first must go

    def test_second_bot_kept_a_while_when_elsewhere(self):
        self.f.decide(people(("Ilker", 5), bots=[5, 7]), [BOT1, BOT2], 0)
        self.assertFalse(self.f.decide(people(("Ilker", 5), bots=[5, 7]), [BOT1, BOT2], 30)["stop"])
        self.assertTrue(self.f.decide(people(("Ilker", 5), bots=[5, 7]), [BOT1, BOT2], 60)["stop"])

    def test_channel_it_cannot_join_is_left_alone(self):
        clients = people(("Ilker", 5), bots=[1])
        self.f.decide(clients, [BOT1], 0)
        self.assertEqual(self.f.decide(clients, [BOT1], 10)["move"], {0: 5})
        with self.assertLogs("tsbot.follow", "WARNING"):
            self.f.decide(clients, [BOT1], 30)  # still not there 20 s later: password or rights
        self.assertEqual(self.f.decide(clients, [BOT1], 100)["move"], {})

    def test_nobody_known_online_stays_put(self):
        self.assertEqual(self.f.decide(people((None, 5), bots=[1]), [BOT1], 100)["move"], {})


if __name__ == "__main__":
    unittest.main()
