from __future__ import annotations

import json

import httpx
import pytest

from meeting_archive.archive import clean_metadata, followup_state
from meeting_archive.bitrix import BitrixClient, BitrixError
from meeting_archive.call_discovery import discover, structured_calls
from test_service import record, service as service_fixture

service = service_fixture


def message(mid, call_id=None, **extra):
    return {"id": mid, "author_id": 0, "date": "2026-10-05T13:13:02+03:00",
            "params": {"COMPONENT_PARAMS": {"CALL_ID": call_id}}, **extra}


def short_record(**extra):
    return record(124, chatId=23787, durationSeconds=91, outcomes=[],
                  tracks=[{"trackId": 8361, "type": "record", "duration": 43}], **extra)


@pytest.mark.parametrize("extra,expected", [({}, "short_call"),
    ({"outcomes": ["transcription"]}, "available"), ({"outcomes": ["summary"]}, "waiting"),
    ({"endDate": ""}, "waiting"), ({"tracks": [{"type": "record", "duration": 60}]}, "waiting"),
    ({"tracks": [{"type": "record", "duration": True}]}, "waiting"),
    ({"tracks": [{"type": "record", "duration": 43}, {"type": "record", "duration": 40}]}, "waiting")])
def test_short_recording_with_longer_session_roundtrips_without_private_tracks(extra, expected):
    item = {**short_record(), **extra}
    metadata = clean_metadata(item)
    assert followup_state(item) == expected
    assert followup_state(metadata) == expected
    assert "tracks" not in metadata
    assert followup_state(item, saved=True) == "saved"


def test_only_structured_service_call_ids_are_evidence():
    assert structured_calls([message(1, 124), message(2, "124"), message(3, "bad"),
        message(4, True), message(5, 777, author_id=41),
        message(6, None, text="BitrixGPT call 888"),
        message(7, None, params={"COMPONENT_ID": "CallMessage", "COMPONENT_PARAMS": {"CALL_ID": 125}}, author_id=41)]) == [124, 125]


async def setup_discovery(service, monkeypatch, pages):
    async def recent(offset=0):
        return {"items": [{"id": 289, "chat_id": 23787, "type": "user"}], "hasMore": False}

    async def messages(dialog, chat, before=0):
        assert dialog == "289" and chat == 23787
        return pages.get(before, [])

    async def get(call_id):
        assert call_id == "124"
        return short_record()

    monkeypatch.setattr(service.client, "recent_dialogs", recent)
    monkeypatch.setattr(service.client, "dialog_messages", messages)
    monkeypatch.setattr(service.client, "followup_metadata", get)


@pytest.mark.asyncio
async def test_scan_finds_missing_call_in_same_chat_and_auto_enqueues_once(service, monkeypatch):
    async def catalogue(*_):
        yield [record(chatId=23787)]
    monkeypatch.setattr(service.client, "catalogue", catalogue)
    await setup_discovery(service, monkeypatch, {0: [message(100, 124)]})
    service.settings.auto_download = True
    service.settings.auto_local = True
    service.settings.auto_since = "2026-10-01T00:00:00+00:00"
    await service.scan(full=True)
    await service.scan()
    rows = service.db.rows("SELECT * FROM meetings ORDER BY call_id")
    assert [r["call_id"] for r in rows] == ["123", "124"]
    assert rows[1]["bitrix"] == "short_call"
    assert len(service.db.rows("SELECT * FROM jobs")) == 2
    assert "tracks" not in json.loads(rows[1]["metadata"])


@pytest.mark.asyncio
async def test_discovery_works_without_any_followup_catalogue_entry_while_paused(service, monkeypatch):
    async def catalogue(*_):
        yield []
    monkeypatch.setattr(service.client, "catalogue", catalogue)
    await setup_discovery(service, monkeypatch, {0: [message(100, 124)]})
    service.settings.auto_download = True
    service.settings.paused = True
    await service.scan()
    assert service.db.rows("SELECT call_id FROM meetings") == [{"call_id": "124"}]
    assert not service.db.rows("SELECT * FROM jobs")


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [{"callId": 777}, {"chatId": 888}, {"uuid": ""},
                                    {"participants": [{"userId": 99}]}])
async def test_discovered_call_requires_matching_session_chat_and_participation(service, monkeypatch, override):
    await setup_discovery(service, monkeypatch, {0: [message(100, 124)]})
    async def get(*_):
        return {**short_record(), **override}
    monkeypatch.setattr(service.client, "followup_metadata", get)
    assert [p async for p in discover(service, "2000-01-01T00:00:00Z", "2026-10-06T00:00:00Z")] == []
    assert not service.db.rows("SELECT * FROM meetings")


@pytest.mark.asyncio
async def test_backfill_resumes_and_catchup_does_not_lose_busy_chat_calls(service, monkeypatch):
    await setup_discovery(service, monkeypatch, {0: [message(100)], 100: [message(90)], 90: [message(80, 124)]})
    batches = [p async for p in discover(service, "2000-01-01T00:00:00Z", "2026-10-06T00:00:00Z")]
    assert batches == []  # Bounded first pass: latest plus one older page.
    batches = [p async for p in discover(service, "2000-01-01T00:00:00Z", "2026-10-06T00:00:00Z")]
    assert batches[0][0]["callId"] == 124
    key = f"call_discovery:{service.settings.portal}:{service.settings.user_id}:23787:cursor"
    assert json.loads(service.db.get_state(key))["before"] == 80
    await setup_discovery(service, monkeypatch, {0: [message(200)], 200: [message(150)], 150: [message(100, 124)]})
    _ = [p async for p in discover(service, "2000-01-01T00:00:00Z", "2026-10-06T00:00:00Z")]
    assert json.loads(service.db.get_state(key))["catchup"] == 150
    _ = [p async for p in discover(service, "2000-01-01T00:00:00Z", "2026-10-06T00:00:00Z")]
    state = json.loads(service.db.get_state(key))
    assert state["head"] == 200 and "catchup" not in state


@pytest.mark.asyncio
async def test_unavailable_call_does_not_hide_next_call_and_is_retried(service, monkeypatch):
    await setup_discovery(service, monkeypatch, {0: [message(100, 122), message(101, 124)]})
    async def get(call_id):
        if call_id == "122":
            raise BitrixError("not ready", retryable=True)
        return short_record()
    monkeypatch.setattr(service.client, "followup_metadata", get)
    pages = [p async for p in discover(service, "2000-01-01T00:00:00Z", "2026-10-06T00:00:00Z")]
    assert pages[0][0]["callId"] == 124
    key = f"call_discovery:{service.settings.portal}:{service.settings.user_id}:23787:cursor"
    state = json.loads(service.db.get_state(key))
    assert "122" in state["pending"]
    state["pending"]["122"] = 0
    service.db.set_state(key, json.dumps(state))
    async def recovered(call_id):
        return {**short_record(), "callId": int(call_id), "uuid": "session-" + call_id}
    monkeypatch.setattr(service.client, "followup_metadata", recovered)
    await setup_discovery(service, monkeypatch, {0: []})
    monkeypatch.setattr(service.client, "followup_metadata", recovered)
    pages = [p async for p in discover(service, "2000-01-01T00:00:00Z", "2026-10-06T00:00:00Z")]
    assert pages[0][0]["callId"] == 122
    assert not json.loads(service.db.get_state(key))["pending"]


@pytest.mark.asyncio
async def test_optional_im_failure_does_not_break_followup_catalogue(service, monkeypatch):
    async def recent(*_):
        raise BitrixError("INSUFFICIENT_SCOPE", auth=True)
    async def catalogue(*_):
        yield [record()]
    monkeypatch.setattr(service.client, "recent_dialogs", recent)
    monkeypatch.setattr(service.client, "catalogue", catalogue)
    await service.scan()
    assert len(service.db.rows("SELECT * FROM meetings")) == 1
    assert service.catalogue["error"] == ""


@pytest.mark.asyncio
async def test_fetch_short_recording_finishes_and_defers_existing_local_queue(service, monkeypatch):
    row = service.db.upsert(service.settings.portal, clean_metadata(short_record()))
    service.settings.auto_local = True
    service.settings.auto_download_audio = True
    job_id = service.request_download(row["id"])
    job = service.db.rows("SELECT * FROM jobs WHERE id=?", (job_id,))[0]
    async def get(*_):
        return short_record()
    async def download(self, client, folder, track, progress):
        path = folder / "audio.ogg"
        path.write_bytes(b"synthetic")
        return path, True
    deferred = []
    monkeypatch.setattr(service.client, "followup", get)
    monkeypatch.setattr("meeting_archive.archive.Archive.download", download)
    monkeypatch.setattr(service, "defer_local", lambda mid, files: deferred.append((mid, files)))
    assert await service.fetch(job, lambda *_: None)
    saved = service.db.meeting(row["id"])
    assert saved["audio"] == "saved" and saved["bitrix"] == "short_call"
    assert deferred == [(row["id"], ["audio.ogg"])]
    service.reconcile_short_calls()
    assert service.db.rows("SELECT state FROM jobs WHERE id=?", (job_id,)) == [{"state": "done"}]


@pytest.mark.asyncio
async def test_metadata_get_and_chat_history_use_correct_api_without_transcript(settings, vault):
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path.endswith("call.followup.get"):
            return httpx.Response(200, json={"result": {"item": short_record()}})
        return httpx.Response(200, json={"result": {"chat_id": 999, "messages": []}})
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        await client.followup_metadata("124")
        with pytest.raises(BitrixError, match="другого чата"):
            await client.dialog_messages("289", 23787, 100)
    finally:
        await client.close()
    assert "transcription" not in requests[0][1]["select"]
    assert requests[1][0] == "/rest/im.dialog.messages.get"
    assert requests[1][1]["LAST_ID"] == 100
