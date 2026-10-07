from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY, portal TEXT NOT NULL, call_id TEXT NOT NULL, uuid TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '', audio TEXT NOT NULL DEFAULT 'not_saved',
                bitrix TEXT NOT NULL DEFAULT 'not_saved', local TEXT NOT NULL DEFAULT 'not_saved',
                requested INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'bitrix',
                UNIQUE(portal, call_id, uuid));
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, meeting_id INTEGER, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, created REAL NOT NULL, error TEXT NOT NULL DEFAULT '',
                progress REAL NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT 'В очереди');
            CREATE TABLE IF NOT EXISTS watched (
                path TEXT PRIMARY KEY, signature TEXT NOT NULL, stable_since REAL NOT NULL,
                imported_signature TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            UPDATE jobs SET state='queued', message='Продолжение после перезапуска'
                WHERE state='running' AND kind != 'install';
            UPDATE jobs SET state='failed', error='Установка прервана. Повторите установку.', message='Установка прервана'
                WHERE state='running' AND kind='install';
        """)
        self.connection.commit()

    def execute(self, sql: str, args=()):
        with self.lock:
            cursor = self.connection.execute(sql, args)
            self.connection.commit()
            return cursor

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.connection.execute(sql, args).fetchall()]

    def upsert(self, portal: str, metadata: dict, source: str = "bitrix") -> dict:
        call_id, uuid = str(metadata["callId"]), metadata.get("uuid") or ""
        with self.lock:
            if not uuid:
                existing = self.rows("SELECT uuid,metadata FROM meetings WHERE portal=? AND call_id=?",
                                     (portal, call_id))
                if len(existing) == 1:
                    uuid = existing[0]["uuid"]
                elif len(existing) > 1:
                    matched = [row for row in existing if metadata.get("startDate") and
                               json.loads(row["metadata"]).get("startDate") == metadata["startDate"]]
                    if len(matched) != 1:
                        raise ValueError("UUID отсутствует: нельзя определить сессию совещания однозначно")
                    uuid = matched[0]["uuid"]
                if uuid:
                    metadata = {**metadata, "uuid": uuid}
            # Hydrate a provisional record when session UUID becomes available.
            if uuid:
                old = self.rows("SELECT id FROM meetings WHERE portal=? AND call_id=? AND uuid=''", (portal, call_id))
                exact = self.rows("SELECT id FROM meetings WHERE portal=? AND call_id=? AND uuid=?", (portal, call_id, uuid))
                if old and not exact:
                    self.execute("UPDATE meetings SET uuid=? WHERE id=?", (uuid, old[0]["id"]))
            self.execute("""INSERT INTO meetings(portal,call_id,uuid,metadata,source) VALUES(?,?,?,?,?)
                ON CONFLICT(portal,call_id,uuid) DO UPDATE SET metadata=excluded.metadata""",
                (portal, call_id, uuid, json.dumps(metadata, ensure_ascii=False), source))
            return self.rows("SELECT * FROM meetings WHERE portal=? AND call_id=? AND uuid=?", (portal, call_id, uuid))[0]

    def meeting(self, meeting_id: int) -> dict:
        rows = self.rows("SELECT * FROM meetings WHERE id=?", (meeting_id,))
        if not rows:
            raise ValueError("Совещание не найдено")
        return rows[0]

    def update_meeting(self, meeting_id: int, **values) -> None:
        allowed = {"folder", "audio", "bitrix", "local", "requested", "metadata"}
        if not values.keys() <= allowed:
            raise ValueError("Недопустимое поле совещания")
        self.execute(f"UPDATE meetings SET {','.join(k+'=?' for k in values)} WHERE id=?", (*values.values(), meeting_id))

    def enqueue(self, kind: str, meeting_id: int | None = None, payload: dict | None = None, next_at=0) -> int:
        payload = payload or {}
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.lock:
            if kind in {"fetch", "transcribe"}:
                active = self.rows("""SELECT id,payload,state FROM jobs WHERE kind=? AND meeting_id IS ?
                                      AND state IN ('queued','running') ORDER BY id""", (kind, meeting_id))
                if kind == "transcribe":
                    # A manual request promotes an identical waiting automatic
                    # run; changing its model/options still creates a new run.
                    options = {k: v for k, v in payload.items() if k != "automatic"}
                    active = [row for row in active if {k: v for k, v in json.loads(row["payload"]).items()
                                                       if k != "automatic"} == options]
            else:
                active = self.rows("""SELECT id,payload,state FROM jobs WHERE kind=? AND meeting_id IS ? AND payload=?
                                      AND state IN ('queued','running')""", (kind, meeting_id, payload_json))
            if active:
                if kind in {"fetch", "transcribe"} and not payload.get("automatic"):
                    promoted = {**json.loads(active[0]["payload"]), "automatic": False}
                    if kind == "fetch":
                        if payload.get("audio_only"):
                            promoted["download_audio"] = True
                            promoted["audio_requested"] = True
                        else:
                            promoted["download_audio"] = bool(payload.get("download_audio") or
                                                              promoted.get("audio_requested") or promoted.get("audio_only"))
                            promoted["audio_only"] = False
                    self.execute("UPDATE jobs SET payload=?,next_at=0,error='' WHERE id=?",
                                 (json.dumps(promoted, ensure_ascii=False, sort_keys=True), active[0]["id"]))
                return active[0]["id"]
            return self.execute("INSERT INTO jobs(kind,meeting_id,payload,next_at,created) VALUES(?,?,?,?,?)",
                                (kind, meeting_id, payload_json, next_at, time.time())).lastrowid

    def claim(self, kinds: tuple[str, ...], manual_only=False, blocked_automatic: tuple[str, ...] = ()) -> dict | None:
        with self.lock:
            condition = " AND COALESCE(json_extract(payload,'$.automatic'),0)!=1" if manual_only else ""
            extra = ()
            if blocked_automatic and not manual_only:
                condition += f" AND (COALESCE(json_extract(payload,'$.automatic'),0)!=1 OR kind NOT IN ({','.join('?' for _ in blocked_automatic)}))"
                extra = blocked_automatic
            rows = self.rows(f"SELECT * FROM jobs WHERE state='queued' AND next_at<=? AND kind IN ({','.join('?' for _ in kinds)}){condition} ORDER BY id LIMIT 1", (time.time(), *kinds, *extra))
            if not rows:
                return None
            job = rows[0]
            self.execute("UPDATE jobs SET state='running', attempts=attempts+1 WHERE id=?", (job["id"],))
            job["attempts"] += 1
            return job

    def job_update(self, job_id: int, **values):
        allowed = {"state", "next_at", "error", "progress", "message"}
        if not values.keys() <= allowed:
            raise ValueError("Недопустимое поле задания")
        self.execute(f"UPDATE jobs SET {','.join(k+'=?' for k in values)} WHERE id=?", (*values.values(), job_id))

    def get_state(self, key: str, default="") -> str:
        rows = self.rows("SELECT value FROM state WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_state(self, key: str, value: str):
        self.execute("INSERT OR REPLACE INTO state(key,value) VALUES(?,?)", (key, value))

    def close(self):
        with self.lock:
            self.connection.close()
