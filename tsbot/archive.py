"""SQLite archive of transcriptions (one row per utterance)."""
from __future__ import annotations

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS utterances (
    id            INTEGER PRIMARY KEY,
    started_ms    INTEGER NOT NULL,
    duration_ms   INTEGER NOT NULL,
    client_id     INTEGER,
    uid           TEXT,
    nickname      TEXT,
    identity      TEXT,
    server_groups TEXT,
    channel_id    INTEGER,
    text          TEXT NOT NULL,
    stt_ms        INTEGER
)"""


class Archive:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(SCHEMA)

    def add(self, **row) -> int:
        row["server_groups"] = json.dumps(list(row.get("server_groups") or []), ensure_ascii=False)
        cols = ", ".join(row)
        cur = self.db.execute(f"INSERT INTO utterances ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        self.db.commit()  # commit right away: a crash loses at most the sentence being processed
        return cur.lastrowid

    def export_jsonl(self, out) -> int:
        n = 0
        for row in self.db.execute("SELECT * FROM utterances ORDER BY id"):
            item = dict(row)
            item["server_groups"] = json.loads(item["server_groups"] or "[]")
            out.write(json.dumps(item, ensure_ascii=False) + "\n")
            n += 1
        return n
