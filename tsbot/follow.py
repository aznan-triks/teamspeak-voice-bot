"""Following people: which channel each bot goes to, and when a second bot is needed.

Only decisions, from the client list the first bot sees (where the bots really are comes from that list too);
`run()` carries them out. People "on the list" are those with an identity; the bots themselves never count.
"""
from __future__ import annotations

import logging
from collections import Counter

log = logging.getLogger(__name__)


class Follower:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.wanted: list = []  # channels that should have a bot, most known players first
        self.since = 0.0  # since when `wanted` has not changed
        self.asked: dict = {}  # seat -> (channel, when): moves asked, not seen done yet
        self.blocked: dict = {}  # channel -> until when: the bot could not get in (password, rights)
        self.seen = False  # known players seen once: from then on, a change waits for people to settle

    def decide(self, clients: list, bot_uids: list, now: float) -> dict:
        """`bot_uids[i]`: UID of seat i (seat 0 always runs; None while a seat is not connected yet).
        Returns {"move": {seat: channel}, "start": bool, "stop": bool}."""
        if None in bot_uids:
            return {"move": {}, "start": False, "stop": False}  # a bot still connecting would count as a player
        where = {c.get("uid"): c.get("channel_id") for c in clients}
        at = [where.get(uid) for uid in bot_uids]
        self._check_moves(at, now)
        counts = Counter(c["channel_id"] for c in clients
                         if c.get("identity") and c.get("uid") not in bot_uids and self.blocked.get(c.get("channel_id"), 0) <= now)
        # Most known players first; on a tie, a channel that already has a bot, so nobody moves for nothing.
        wanted = sorted(counts, key=lambda ch: (-counts[ch], ch not in at, ch))[: self.cfg.get("max_bots", 2)]
        if wanted != self.wanted:
            # At start the bot goes at once (live: 13-18 s after each restart); later, people may still be moving.
            self.since = now if self.seen else now - self.cfg.get("move_after_s", 3)
            self.wanted = wanted
        self.seen = self.seen or bool(wanted)
        out = {"move": {}, "start": False, "stop": False}
        if now - self.since < self.cfg.get("move_after_s", 3):
            return out  # people still moving around: stay put
        second = len(bot_uids) > 1
        if second and len(wanted) < 2 and (at[1] in wanted or now - self.since >= self.cfg.get("stop_after_s", 60)):
            out["stop"] = True  # no longer needed (at once if it sits where the first bot must go)
        if not wanted:
            return out  # nobody known online: the first bot stays where it is
        if len(wanted) > 1 and not second:
            out["start"] = True
        targets = [ch for ch in wanted if ch not in at[: len(wanted)]]
        for seat, channel in enumerate(at[: len(wanted)]):
            if channel in wanted or channel is None or not targets or (out["stop"] and seat == 1):
                continue
            target = targets.pop(0)
            if self.asked.get(seat, (None,))[0] != target:
                self.asked[seat] = (target, now)
                out["move"][seat] = target
        return out

    def _check_moves(self, at: list, now: float) -> None:
        for seat, (channel, when) in list(self.asked.items()):
            if seat < len(at) and at[seat] == channel:
                del self.asked[seat]
            elif now - when >= self.cfg.get("move_timeout_s", 15):
                log.warning("following: seat %d could not join channel %s, left alone for a while", seat + 1, channel)
                self.blocked[channel] = now + self.cfg.get("blocked_s", 600)
                del self.asked[seat]
