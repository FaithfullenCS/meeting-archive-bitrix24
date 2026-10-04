from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from meeting_archive.bitrix import BitrixClient, BitrixError
from meeting_archive.service import Service, retry_delay


@pytest.fixture
async def service(tmp_path, settings, vault):
    home = tmp_path / "home"
    settings.save(home)
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    result = Service(home, vault=vault, client=client)
    # Injected client must use the same mutable settings as the service.
    client.settings = result.settings
    yield result
    await client.close()
    result.db.close()


def record(call_id=123, **overrides):
    return {"callId": call_id, "uuid": "session-" + str(call_id), "startDate": "2026-10-01T09:00:00+03:00",
            "endDate": "2026-10-01T10:00:00+03:00", "durationSeconds": 3600,
            "participants": [{"userId": 41, "name": "Synthetic user"}],
            "overview": {"topic": "Synthetic meeting"}, **overrides}


@pytest.mark.asyncio
async def test_initial_scan_metadata_only_even_if_server_returns_private_materials(service, monkeypatch):
    async def catalogue(*_):
        yield [record(tracks=[{"url": "https://synthetic.bitrix24.ru/?auth=synthetic-secret"}],
                      transcription={"segments": [{"text": "private synthetic speech"}]})]

    async def unexpected_followup(*_):
        pytest.fail("Initial metadata scan requested transcripts or recordings")

    monkeypatch.setattr(service.client, "catalogue", catalogue)
    monkeypatch.setattr(service.client, "followup", unexpected_followup)
    await service.scan(full=True)
    rows = service.db.rows("SELECT * FROM meetings")
    assert len(rows) == 1
    metadata = json.loads(rows[0]["metadata"])
    assert "tracks" not in metadata and "transcription" not in metadata
    assert not service.db.rows("SELECT * FROM jobs")
    assert not Path(service.settings.archive_root).exists()


@pytest.mark.asyncio
async def test_scan_filters_other_users_even_when_admin_endpoint_returns_them(service, monkeypatch):
    async def catalogue(*_):
        yield [record(123), record(124, participants=[{"userId": 99}])]

    monkeypatch.setattr(service.client, "catalogue", catalogue)
    await service.scan(full=True)
    assert [row["call_id"] for row in service.db.rows("SELECT * FROM meetings")] == ["123"]


@pytest.mark.asyncio
async def test_auto_download_only_after_enabled_boundary_and_pause_does_not_enqueue(service, monkeypatch):
    async def catalogue(*_):
        yield [record(123, endDate="2026-10-01T09:59:59+03:00"), record(124, endDate="2026-10-01T10:00:00+03:00")]

    monkeypatch.setattr(service.client, "catalogue", catalogue)
    service.settings.auto_download = True
    service.settings.auto_since = "2026-10-01T10:00:00+03:00"
    await service.scan(full=True)
    jobs = service.db.rows("SELECT * FROM jobs")
    assert len(jobs) == 1
    assert service.db.meeting(jobs[0]["meeting_id"])["call_id"] == "124"
    assert json.loads(jobs[0]["payload"])["automatic"] is True
    service.db.job_update(jobs[0]["id"], state="done")
    service.settings.paused = True
    await service.scan(full=True)
    assert len(service.db.rows("SELECT * FROM jobs")) == 1


@pytest.mark.asyncio
async def test_access_failure_preserves_previous_catalogue_and_scan_cursor(service, monkeypatch):
    service.db.upsert(service.settings.portal, record())
    service.db.set_state("last_scan:" + service.settings.portal, "2026-09-01T00:00:00+00:00")

    async def catalogue(*_):
        raise BitrixError("synthetic access denied", auth=True)
        yield []

    monkeypatch.setattr(service.client, "catalogue", catalogue)
    await service.scan(full=True)
    assert len(service.db.rows("SELECT * FROM meetings")) == 1
    assert service.catalogue["error"] == "synthetic access denied"
    assert service.auth_error == "synthetic access denied"
    assert service.db.get_state("last_scan:" + service.settings.portal) == "2026-09-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_late_transcript_is_saved_without_redownloading_audio(service, monkeypatch):
    service.settings.auto_download_audio = True
    meeting = service.db.upsert(service.settings.portal, record())
    job_id = service.request_download(meeting["id"])
    job = service.db.claim(("fetch",))
    transcript = {"segments": [{"start": 0, "end": 1, "userId": 41, "userName": "Synthetic user", "text": "Тест"}]}
    responses = iter([record(tracks=[{"trackId": 1, "fileName": "voice.webm", "fileSize": 5, "relUrl": "/recording"}]),
                      record(tracks=[{"trackId": 1, "fileName": "voice.webm", "fileSize": 5, "relUrl": "/recording"}],
                             transcription=transcript)])
    downloads = []

    async def followup(*_):
        return next(responses)

    def handler(request):
        downloads.append(request)
        return httpx.Response(200, content=b"audio")

    monkeypatch.setattr(service.client, "followup", followup)
    await service.client.http.aclose()
    service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await service.perform(job, lambda *_: None)
    saved = service.db.meeting(meeting["id"])
    assert saved["audio"] == "saved" and saved["bitrix"] == "waiting"
    queued = service.db.rows("SELECT * FROM jobs WHERE id=?", (job_id,))[0]
    assert queued["state"] == "queued"
    assert queued["next_at"] > time.time()
    assert await service.fetch(job, lambda *_: None) is True
    assert service.db.meeting(meeting["id"])["bitrix"] == "saved"
    assert len(downloads) == 1
    assert (Path(saved["folder"]) / "bitrix/transcript.txt").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [record(999), record(participants=[{"userId": 99}]), record(uuid="different-session")])
async def test_fetch_rejects_wrong_call_session_or_missing_current_participant(service, monkeypatch, response):
    meeting = service.db.upsert(service.settings.portal, record())

    async def followup(*_):
        return response

    monkeypatch.setattr(service.client, "followup", followup)
    with pytest.raises(ValueError):
        await service.fetch({"meeting_id": meeting["id"]}, lambda *_: None)
    assert not Path(service.settings.archive_root).exists()


def test_watch_requires_stable_finished_file_and_avoids_duplicates(service, tmp_path, monkeypatch):
    folder = tmp_path / "incoming"
    folder.mkdir()
    source = folder / "voice.wav"
    source.write_bytes(b"first")
    service.settings.watch_folder = str(folder)
    clock = [1000.0]
    monkeypatch.setattr("meeting_archive.service.time.time", lambda: clock[0])
    service.watch_once()
    assert not service.db.rows("SELECT * FROM jobs")
    clock[0] = 1030
    source.write_bytes(b"changed and still writing")
    service.watch_once()
    clock[0] = 1089
    service.watch_once()
    assert not service.db.rows("SELECT * FROM jobs")
    clock[0] = 1090
    monkeypatch.setattr("meeting_archive.service.exclusive_readable", lambda _: False)
    service.watch_once()
    assert not service.db.rows("SELECT * FROM jobs")
    monkeypatch.setattr("meeting_archive.service.exclusive_readable", lambda _: True)
    service.watch_once()
    assert len(service.db.rows("SELECT * FROM jobs")) == 1
    service.watch_once()
    assert len(service.db.rows("SELECT * FROM jobs")) == 1


def test_paused_queue_still_allows_manual_requests(service):
    automatic = service.db.enqueue("fetch", 1, {"automatic": True})
    manual = service.db.enqueue("fetch", 2, {"automatic": False})
    assert service.db.claim(("fetch",), manual_only=True)["id"] == manual
    assert service.db.claim(("fetch",), manual_only=True) is None
    assert service.db.claim(("fetch",))["id"] == automatic


@pytest.mark.asyncio
async def test_cancelled_manual_job_stays_cancelled_after_restart(service):
    meeting = service.db.upsert(service.settings.portal, record())
    job_id = service.request_download(meeting["id"])
    await service.cancel(job_id)
    assert service.db.rows("SELECT state FROM jobs WHERE id=?", (job_id,))[0]["state"] == "cancelled"
    assert service.db.claim(("fetch",)) is None


def test_cpu_requires_explicit_confirmation_before_queueing(service):
    service.settings.device = "cpu"
    service.settings.cpu_confirmed = False
    with pytest.raises(ValueError, match="CPU"):
        service.request_transcribe(1)
    assert not service.db.rows("SELECT * FROM jobs")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["voice.webm", "audio/voice.webm", "audio\\voice.webm"])
async def test_selected_saved_audio_reaches_worker_with_old_and_ui_names(service, monkeypatch, name):
    meeting = service.db.upsert(service.settings.portal, record())
    folder = service.archive.ensure(meeting)
    audio = folder / "audio/voice.webm"
    audio.write_bytes(b"synthetic recording")
    service.db.update_meeting(meeting["id"], folder=str(folder), audio="saved")
    monkeypatch.setattr(service.module, "require_engine", lambda _: None)
    received = []

    async def transcribe(job_id, payload, external, progress):
        received.extend(payload["files"])
        Path(payload["output"]).mkdir()
        return {"model": "large-v3", "engine": "whisper", "seconds": 1, "audio_seconds": 30}

    monkeypatch.setattr(service.module, "transcribe", transcribe)
    job_id = service.request_transcribe(meeting["id"], sample=True, file_names=[name])
    job = service.db.claim(("transcribe",))
    assert job["id"] == job_id
    # Preserve the old relative path form to verify retries of existing jobs too.
    job["payload"] = json.dumps({**json.loads(job["payload"]), "file_names": [name]})
    await service.transcribe_job(job, lambda *_: None)
    assert received == [str(audio)]
    test = json.loads(service.db.get_state("model_test"))
    assert test["engine"] == "whisper" and test["device"] == "cuda"
    assert test["settings"]["vad"] == service.settings.vad


@pytest.mark.parametrize("names", [["../voice.wav"], ["audio/../voice.wav"], ["C:\\voice.wav"], ["missing.wav"], []])
def test_invalid_or_missing_selected_audio_never_enters_queue(service, monkeypatch, names):
    meeting = service.db.upsert(service.settings.portal, record())
    folder = service.archive.ensure(meeting)
    (folder / "audio/voice.wav").write_bytes(b"synthetic audio")
    service.db.update_meeting(meeting["id"], folder=str(folder), audio="saved")
    monkeypatch.setattr(service.module, "require_engine", lambda _: None)
    with pytest.raises(ValueError):
        service.request_transcribe(meeting["id"], file_names=names)
    assert not service.db.rows("SELECT * FROM jobs")
    assert service.db.meeting(meeting["id"])["local"] == "not_saved"


@pytest.mark.parametrize("engine", ["parakeet", "gigaam"])
def test_automatic_nonwhisper_engine_queues_with_stale_legacy_adapter(service, tmp_path, monkeypatch, engine):
    service.settings.auto_local = True
    service.settings.engine = engine
    service.settings.external_engine = str(tmp_path / "removed-legacy-app")
    meeting = service.db.upsert(service.settings.portal, record())
    audio = Path(service.settings.archive_root) / "saved-meeting/audio/voice.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"synthetic recording fixture")
    service.db.update_meeting(meeting["id"], folder=str(audio.parent.parent), audio="saved")
    pending_key = "auto_local_pending:" + str(meeting["id"])
    service.db.set_state(pending_key, json.dumps([audio.name]))
    checked_paths = []

    def python(external=""):
        checked_paths.append(external)
        if external:
            raise ValueError("Legacy application no longer exists")
        return "synthetic-owned-python"

    monkeypatch.setattr(service.module, "python", python)
    # Model readiness has dedicated structural tests; this test exercises the
    # automatic queue with an available selected engine and a stale old adapter.
    monkeypatch.setattr(service.module, "require_engine", lambda _: None)
    service.schedule_local_pending()

    jobs = service.db.rows("SELECT * FROM jobs WHERE kind='transcribe'")
    assert checked_paths == []
    assert len(jobs) == 1
    payload = json.loads(jobs[0]["payload"])
    assert payload["settings"]["engine"] == engine
    assert payload["external_engine"] == ""
    assert payload["automatic"] is True
    assert payload["file_names"] == [audio.name]
    assert service.db.get_state(pending_key) == "[]"
    assert service.db.meeting(meeting["id"])["local"] == "queued"


def test_automatic_whisper_keeps_explicit_legacy_adapter_preflight(service, tmp_path, monkeypatch):
    service.settings.auto_local = True
    service.settings.engine = "whisper"
    service.settings.external_engine = str(tmp_path / "removed-legacy-app")
    checked_paths = []

    def unavailable(external=""):
        checked_paths.append(external)
        raise ValueError("Legacy application no longer exists")

    monkeypatch.setattr(service.module, "python", unavailable)
    service.schedule_local_pending()
    assert checked_paths == [service.settings.external_engine]
    assert not service.db.rows("SELECT * FROM jobs")


def test_followup_retry_delay_increases_but_keeps_daily_observation():
    assert retry_delay(time.time()) == 60
    assert retry_delay(time.time() - 3600) == 300
    assert retry_delay(time.time() - 48 * 3600) == 86400
