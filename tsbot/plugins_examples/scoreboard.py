"""Example plugin with a voice action: a tiny scoreboard saved in a JSON file.

Say "Hector, un point pour Bob sur fléchettes": the bot asks for confirmation, then calls execute().
Use it as a template for a plugin that talks to a real application.
"""
from __future__ import annotations

import json
from pathlib import Path

from tsbot.plugins import Action


class Plugin:
    def __init__(self, config: dict):
        self.path = Path(config.get("path", "data/scoreboard.json"))
        self.players = config.get("players", [])
        self.games = config.get("games", [])

    def actions(self):
        return [Action(
            "scoreboard.add_points",
            "Ajoute (ou retire, si négatif) des points à un joueur sur un jeu du tableau des scores.",
            {
                "type": "object",
                "properties": {
                    "player": {"type": "string", "enum": self.players},
                    "game": {"type": "string", "enum": self.games},
                    "points": {"type": "integer"},
                },
                "required": ["player", "game", "points"],
            },
        )]

    def context(self):
        return {"joueurs": self.players, "jeux": self.games}

    def web(self, req):
        """The site's door to this plugin: GET /api/plugin/tsbot.plugins_examples.scoreboard/scores."""
        if req.method == "GET" and req.path == "scores":
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        raise LookupError(req.path)

    def execute(self, action):
        a = action.args
        scores = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        game = scores.setdefault(a["game"], {})
        game[a["player"]] = game.get(a["player"], 0) + a["points"]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # never leave a half-written file
        total = game[a["player"]]
        return f"C'est noté : {a['player']} a maintenant {total} point{'s' if abs(total) > 1 else ''} sur {a['game']}."
