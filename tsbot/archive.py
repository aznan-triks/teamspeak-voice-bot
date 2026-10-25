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
);
CREATE TABLE IF NOT EXISTS actions (
    id            INTEGER PRIMARY KEY,
    created_ms    INTEGER NOT NULL,
    name          TEXT NOT NULL,
    args          TEXT NOT NULL,
    requested_uid TEXT,
    identity      TEXT,
    utterance_ids TEXT,
    status        TEXT NOT NULL,
    result        TEXT
)"""


class Archive:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def _insert(self, table: str, row: dict) -> int:
        cols = ", ".join(row)
        cur = self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        self.db.commit()  # commit right away: a crash loses at most the sentence being processed
        return cur.lastrowid

    def add(self, **row) -> int:
        row["server_groups"] = json.dumps(list(row.get("server_groups") or []), ensure_ascii=False)
        return self._insert("utterances", row)

    def add_action(self, action, created_ms: int) -> int:
        return self._insert("actions", {
            "created_ms": created_ms, "name": action.name, "args": json.dumps(action.args, ensure_ascii=False),
            "requested_uid": action.requested_by.uid, "identity": action.requested_by.identity,
            "utterance_ids": json.dumps(list(action.transcript_ids)), "status": "proposed",
        })

    def set_action_status(self, action_id: int, status: str, result: str | None = None) -> None:
        self.db.execute("UPDATE actions SET status = ?, result = ? WHERE id = ?", (status, result, action_id))
        self.db.commit()

    def export_jsonl(self, out) -> int:
        n = 0
        for row in self.db.execute("SELECT * FROM utterances ORDER BY id"):
            item = dict(row)
            item["server_groups"] = json.loads(item["server_groups"] or "[]")
            out.write(json.dumps(item, ensure_ascii=False) + "\n")
            n += 1
        return n
