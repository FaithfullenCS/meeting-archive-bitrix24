from __future__ import annotations

import asyncio
import json
import math
from unittest.mock import AsyncMock

import httpx
import pytest

from meeting_archive.app import meeting_view
from meeting_archive.archive import clean_metadata, followup_state
from meeting_archive.notifications import Notifications
from test_service import record, service as service_fixture

service = service_fixture


@pytest.mark.parametrize('duration,expected', [(42, 'short_call'), (59, 'short_call'), (60, None),
    (None, None), (0, None), (-1, None), ('42', None), (True, None), (math.nan, None), (math.inf, None)])
def test_duration_boundary(duration, expected):
    assert followup_state(record(durationSeconds=duration)) == expected


def test_finished_and_ready_text_take_precedence():
    short = record(durationSeconds=42)
    assert followup_state({**short, 'endDate': None}) is None
    assert followup_state(short, saved=True) == 'saved'
    assert followup_state({**short, 'outcomes': ['transcription']}) == 'available'
    assert followup_state(clean_metadata({**short, 'transcription': {'segments': [{'text': 'Речь'}]}})) == 'available'


@pytest.mark.asyncio
@pytest.mark.parametrize('download', [False, True])
async def test_short_call_waits_only_for_requested_audio(service, monkeypatch, download):
    service.settings.auto_download_audio = download
    item = record(durationSeconds=42, tracks=[])
    meeting = service.db.upsert(service.settings.portal, clean_metadata(item))
    assert meeting_view(meeting)['bitrix'] == 'short_call'
    service.request_download(meeting['id'])
    job = service.db.claim(('fetch',))
    monkeypatch.setattr(service.client, 'followup', AsyncMock(return_value=item))
    assert await service.fetch(job, lambda *_: None) is (not download)
    assert service.db.meeting(meeting['id'])['bitrix'] == 'short_call'
    if download:
        await service.perform(job, lambda *_: None)
        assert service.db.rows('SELECT message FROM jobs WHERE id=?', (job['id'],))[0]['message'] == 'Ожидаем аудиозапись Bitrix24'
    folder = service.archive.folder(service.db.meeting(meeting['id']))
    assert json.loads((folder / 'meeting.json').read_text('utf-8'))['states']['bitrix'] == 'short_call'


@pytest.mark.asyncio
@pytest.mark.parametrize('auto_local', [False, True])
async def test_late_audio_and_text_without_duplicate_download(service, monkeypatch, auto_local):
    service.settings.auto_download_audio = True
    service.settings.auto_local = auto_local
    short = record(durationSeconds=42, tracks=[])
    meeting = service.db.upsert(service.settings.portal, clean_metadata(short))
    service.request_download(meeting['id'])
    job = service.db.claim(('fetch',))
    with_audio = {**short, 'tracks': [{'trackId': 1, 'fileName': 'voice.webm', 'fileSize': 5, 'relUrl': '/recording'}]}
    responses = iter([short, with_audio, {**with_audio, 'outcomes': ['transcription'],
        'transcription': {'segments': [{'start': 0, 'end': 1, 'text': 'Речь'}]}}])
    monkeypatch.setattr(service.client, 'followup', AsyncMock(side_effect=lambda *_: next(responses)))
    monkeypatch.setattr(service, 'schedule_local_pending', lambda: None)
    downloads = []
    def handler(request):
        downloads.append(request)
        return httpx.Response(200, content=b'audio')
    await service.client.http.aclose()
    service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert not await service.fetch(job, lambda *_: None)
    assert await service.fetch(job, lambda *_: None)
    assert service.db.meeting(meeting['id'])['audio'] == 'saved'
    assert bool(service.db.get_state('auto_local_pending:' + str(meeting['id']))) is auto_local
    assert await service.fetch(job, lambda *_: None)
    assert service.db.meeting(meeting['id'])['bitrix'] == 'saved'
    assert len(downloads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('audio,requested,done', [('saved', True, True), ('not_saved', False, True), ('not_saved', True, False)])
async def test_restart_reconciles_existing_queue(service, audio, requested, done):
    meeting = service.db.upsert(service.settings.portal, record(durationSeconds=42))
    service.db.update_meeting(meeting['id'], audio=audio, bitrix='waiting')
    job_id = service.db.enqueue('fetch', meeting['id'], {'download_audio': requested, 'automatic': True}, next_at=9999999999)
    service.reconcile_short_calls()
    service.reconcile_short_calls()
    assert service.db.meeting(meeting['id'])['bitrix'] == 'short_call'
    assert service.db.rows('SELECT state FROM jobs WHERE id=?', (job_id,))[0]['state'] == ('done' if done else 'queued')


@pytest.mark.asyncio
async def test_saved_and_imported_records_and_later_ready_text(service):
    meeting = service.db.upsert(service.settings.portal, clean_metadata(record(durationSeconds=42)))
    service.db.update_meeting(meeting['id'], bitrix='saved')
    service.reconcile_short_calls()
    assert meeting_view(service.db.meeting(meeting['id']))['bitrix'] == 'saved'
    imported = service.db.upsert('local-import', record(999, durationSeconds=42), 'import')
    assert meeting_view(imported)['bitrix'] == 'not_saved'
    service.db.update_meeting(meeting['id'], bitrix='short_call')
    service.db.upsert(service.settings.portal, clean_metadata(record(durationSeconds=42, outcomes=['transcription'])))
    assert meeting_view(service.db.meeting(meeting['id']))['bitrix'] == 'available'


@pytest.mark.asyncio
async def test_short_call_notification_does_not_claim_transcript(service, monkeypatch):
    meeting = service.db.upsert(service.settings.portal, record(durationSeconds=42))
    service.db.update_meeting(meeting['id'], bitrix='short_call', audio='saved')
    notices = Notifications(service)
    notices.pending['download'] = {1: meeting['id']}
    monkeypatch.setattr(notices, 'enabled', lambda *_: True)
    monkeypatch.setattr('meeting_archive.notifications.asyncio.sleep', AsyncMock())
    notices.send = AsyncMock()
    await notices.flush('download')
    title, message, _ = notices.send.call_args.args
    assert title == 'Аудиозапись сохранена'
    assert 'текст Bitrix24 не предоставлен' in message


@pytest.mark.asyncio
async def test_import_attaches_audio_to_existing_short_call(service, tmp_path, monkeypatch):
    meeting = service.db.upsert(service.settings.portal, clean_metadata(record(durationSeconds=42)))
    service.db.update_meeting(meeting['id'], bitrix='short_call')
    audio = tmp_path / 'recording.webm'
    audio.write_bytes(b'synthetic audio')
    service.request_import(audio, meeting['id'])
    job = service.db.claim(('import',))
    monkeypatch.setattr('meeting_archive.service.asyncio.sleep', AsyncMock())
    await service.import_job(job, lambda *_: None)
    assert len(service.db.rows('SELECT * FROM meetings')) == 1
    assert service.db.meeting(meeting['id'])['audio'] == 'saved'
    assert service.db.meeting(meeting['id'])['bitrix'] == 'short_call'


@pytest.mark.asyncio
async def test_short_call_job_finishes_without_a_transcription_claim(service, monkeypatch):
    item = record(durationSeconds=42)
    meeting = service.db.upsert(service.settings.portal, clean_metadata(item))
    job_id = service.request_download(meeting['id'])
    monkeypatch.setattr(service.client, 'followup', AsyncMock(return_value=item))
    monkeypatch.setattr(service.notifications, 'completed', lambda *_: setattr(service, 'alive', False))
    service.alive = True
    await asyncio.wait_for(service.job_loop(('fetch',)), 2)
    job = service.db.rows('SELECT * FROM jobs WHERE id=?', (job_id,))[0]
    assert job['state'] == 'done'
    assert job['message'] == 'Короткий звонок: текст Bitrix24 не предоставлен'
    assert not service.db.rows("SELECT * FROM jobs WHERE kind='transcribe'")


@pytest.mark.asyncio
async def test_catalogue_persists_short_call_and_clears_it_when_text_is_ready(service, monkeypatch):
    async def short(*_):
        yield [record(durationSeconds=42)]
    monkeypatch.setattr(service.client, 'catalogue', short)
    await service.scan(full=True)
    assert service.db.rows('SELECT bitrix FROM meetings')[0]['bitrix'] == 'short_call'
    async def ready(*_):
        yield [record(durationSeconds=42, outcomes=['transcription'])]
    monkeypatch.setattr(service.client, 'catalogue', ready)
    await service.scan(full=True)
    assert meeting_view(service.db.rows('SELECT * FROM meetings')[0])['bitrix'] == 'available'


@pytest.mark.asyncio
async def test_short_call_deferral_respects_pause_and_missing_model(service, monkeypatch):
    meeting = service.db.upsert(service.settings.portal, clean_metadata(record(durationSeconds=42)))
    service.settings.auto_local = True
    service.settings.paused = True
    monkeypatch.setattr(service, 'transcription_status', lambda: {'ready': False, 'reason': 'Модель не готова'})
    service.defer_local(meeting['id'], ['voice.webm'])
    key = 'auto_local_pending:' + str(meeting['id'])
    assert json.loads(service.db.get_state(key)) == ['voice.webm']
    assert not service.db.rows("SELECT * FROM jobs WHERE kind='transcribe'")
    service.settings.paused = False
    service.schedule_local_pending()
    assert service.db.get_state('auto_local_error') == 'Модель не готова'
    assert json.loads(service.db.get_state(key)) == ['voice.webm']
