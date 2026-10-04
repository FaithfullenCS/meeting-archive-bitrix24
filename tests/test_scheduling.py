from datetime import datetime, timezone

import pytest

from meeting_archive.db import Database
from meeting_archive.scheduling import default_schedule, validate_schedule, window_status


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


def test_overnight_window_belongs_to_day_when_it_starts():
    window = {"mode": "window", "days": [0], "start": "22:00", "end": "06:00"}
    assert window_status(window, at(5, 21, 59))["allowed"] is False  # Monday
    assert window_status(window, at(5, 22))["allowed"] is True
    assert window_status(window, at(6, 5, 59))["allowed"] is True
    closed = window_status(window, at(6, 6))
    assert not closed["allowed"]
    assert closed["next_at"].startswith("2026-10-12T22:00")
    assert not window_status(window, at(6, 23))["allowed"]


def test_immediate_and_equal_times_are_explicit_all_day_windows():
    assert window_status(default_schedule(), at(5, 14))["allowed"]
    window = {"mode": "window", "days": [0], "start": "00:00", "end": "00:00"}
    assert window_status(window, at(5, 14))["allowed"]
    assert not window_status(window, at(6, 0))["allowed"]


@pytest.mark.parametrize("change", [{"days": []}, {"days": [True]}, {"days": [7]}, {"start": "24:00"}, {"end": "12:99"}, {"mode": "cron"}])
def test_invalid_windows_are_rejected(change):
    with pytest.raises(ValueError):
        validate_schedule({**default_schedule(), **change})


def test_automatic_queue_waits_while_manual_and_install_jobs_can_run(tmp_path):
    db = Database(tmp_path / "queue.sqlite")
    automatic = db.enqueue("transcribe", 1, {"automatic": True})
    manual = db.enqueue("transcribe", 2, {"automatic": False})
    install = db.enqueue("install", payload={"packages": [{"engine": "whisper", "model": "tiny"}]})
    assert db.claim(("transcribe", "install"), blocked_automatic=("transcribe",))["id"] == manual
    assert db.claim(("transcribe", "install"), blocked_automatic=("transcribe",))["id"] == install
    assert db.claim(("transcribe", "install"), blocked_automatic=("transcribe",)) is None
    assert db.claim(("transcribe", "install"))["id"] == automatic
    db.close()


def test_download_window_does_not_block_other_automatic_work(tmp_path):
    db = Database(tmp_path / "queue.sqlite")
    fetch = db.enqueue("fetch", 1, {"automatic": True})
    local = db.enqueue("transcribe", 1, {"automatic": True})
    assert db.claim(("fetch", "transcribe"), blocked_automatic=("fetch",))["id"] == local
    assert db.claim(("fetch",))["id"] == fetch
    db.close()
