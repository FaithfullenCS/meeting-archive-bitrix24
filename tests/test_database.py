from __future__ import annotations

import json

from meeting_archive.db import Database


def test_uuid_hydration_keeps_existing_archive_and_requests(tmp_path):
    db = Database(tmp_path / "state.db")
    first = db.upsert("synthetic.bitrix24.ru", {"callId": 123, "startDate": "2026-10-01"})
    db.update_meeting(first["id"], folder=str(tmp_path / "existing"), requested=1)
    hydrated = db.upsert("synthetic.bitrix24.ru", {"callId": 123, "uuid": "session-123", "startDate": "2026-10-01"})
    later = db.upsert("synthetic.bitrix24.ru", {"callId": 123, "startDate": "2026-10-01", "overview": {"topic": "New title"}})
    assert hydrated["id"] == first["id"] == later["id"]
    assert later["uuid"] == "session-123"
    assert later["folder"] == str(tmp_path / "existing")
    assert later["requested"] == 1
    assert len(db.rows("SELECT * FROM meetings")) == 1
    assert json.loads(later["metadata"])["overview"]["topic"] == "New title"
    db.close()


def test_distinct_session_uuid_and_portal_are_distinct_identity(tmp_path):
    db = Database(tmp_path / "state.db")
    a = db.upsert("synthetic.bitrix24.ru", {"callId": 123, "uuid": "a"})
    b = db.upsert("synthetic.bitrix24.ru", {"callId": 123, "uuid": "b"})
    c = db.upsert("other.bitrix24.ru", {"callId": 123, "uuid": "a"})
    assert len({a["id"], b["id"], c["id"]}) == 3
    db.close()


def test_queue_survives_restart_without_duplicate_or_lost_cancellation(tmp_path):
    path = tmp_path / "state.db"
    db = Database(path)
    download = db.enqueue("download", 123, {"request": "manual"})
    assert db.enqueue("download", 123, {"request": "manual"}) == download
    assert db.claim(("download",))["id"] == download
    cancelled = db.enqueue("download", 124)
    db.job_update(cancelled, state="cancelled")
    install = db.enqueue("install", payload={"model": "small"})
    assert db.claim(("install",))["id"] == install
    db.close()
    reopened = Database(path)
    claimed = reopened.claim(("download",))
    assert claimed["id"] == download
    assert claimed["attempts"] == 2
    assert reopened.claim(("download",)) is None
    states = {row["id"]: row["state"] for row in reopened.rows("SELECT * FROM jobs")}
    assert states[cancelled] == "cancelled"
    assert states[install] == "failed"
    reopened.close()


def test_delayed_job_is_not_claimed_early(tmp_path):
    import time
    db = Database(tmp_path / "state.db")
    delayed = db.enqueue("download", 123, next_at=time.time() + 3600)
    assert db.claim(("download",)) is None
    db.job_update(delayed, next_at=0)
    assert db.claim(("download",))["id"] == delayed
    db.close()


def test_manual_download_promotes_existing_automatic_fetch_without_duplicate(tmp_path):
    import time
    db = Database(tmp_path / "state.db")
    queued = db.enqueue("fetch", 123, {"automatic": True}, next_at=time.time() + 86400)
    promoted = db.enqueue("fetch", 123, {"automatic": False})
    assert promoted == queued
    assert len(db.rows("SELECT * FROM jobs")) == 1
    job = db.claim(("fetch",), manual_only=True)
    assert job["id"] == queued
    assert json.loads(job["payload"])["automatic"] is False
    assert db.enqueue("fetch", 123, {"automatic": True}) == queued
    assert len(db.rows("SELECT * FROM jobs")) == 1
    db.close()
