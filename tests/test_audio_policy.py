import json
from unittest.mock import AsyncMock

import httpx
import pytest

from test_service import service as service_fixture, record
from test_app import ui as ui_fixture, login

service = service_fixture
ui = ui_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize('automatic', [False, True])
async def test_text_only_fetch_does_not_download_or_wait_for_audio(service, monkeypatch, automatic):
    service.settings.auto_download_audio = False
    meeting = service.db.upsert(service.settings.portal, record())
    job_id = service.request_download(meeting['id'], automatic=automatic)
    job = service.db.claim(('fetch',))
    transcript = {'segments': [{'start': 0, 'end': 1, 'text': 'Synthetic text'}]}
    monkeypatch.setattr(service.client, 'followup', AsyncMock(return_value=record(
        transcription=transcript, tracks=[{'trackId': 1, 'fileName': 'voice.wav', 'fileSize': 5, 'relUrl': '/recording'}])))
    async def forbidden(*args, **kwargs):
        pytest.fail('Text-only download fetched audio')
    monkeypatch.setattr('meeting_archive.archive.Archive.download', forbidden)
    await service.perform(job, lambda *_: None)
    saved = service.db.meeting(meeting['id'])
    assert saved['bitrix'] == 'saved'
    assert saved['audio'] != 'saved'
    assert service.db.rows('SELECT state FROM jobs WHERE id=?', (job_id,))[0]['state'] == 'running'  # perform finished; job_loop marks done.
    assert not service.db.rows("SELECT * FROM jobs WHERE kind='transcribe'")


@pytest.mark.asyncio
async def test_audio_policy_cannot_disable_with_auto_local(ui):
    client, service, _ = ui
    csrf = await login(client)
    headers = {'X-CSRF-Token': csrf}
    assert (await client.post('/api/settings', headers=headers, json={'auto_download_audio': False})).status_code == 200
    assert (await client.post('/api/settings', headers=headers, json={'auto_local': True})).status_code == 200
    assert service.settings.auto_download_audio is True
    rejected = await client.post('/api/settings', headers=headers, json={'auto_download_audio': False})
    assert rejected.status_code == 400
    assert service.settings.auto_download_audio is True
    assert (await client.post('/api/settings', headers=headers, json={'auto_local': False, 'auto_download_audio': False})).status_code == 200


@pytest.mark.asyncio
async def test_explicit_audio_only_download_finishes_without_followup(service, monkeypatch):
    service.settings.auto_download_audio = False
    meeting = service.db.upsert(service.settings.portal, record())
    service.request_download(meeting['id'], audio_only=True)
    job = service.db.claim(('fetch',))
    monkeypatch.setattr(service.client, 'followup', AsyncMock(return_value=record(
        tracks=[{'trackId': 1, 'fileName': 'voice.wav', 'fileSize': 5, 'relUrl': '/recording'}])))
    await service.client.http.aclose()
    service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'voice')))
    assert await service.fetch(job, lambda *_: None)
    saved = service.db.meeting(meeting['id'])
    assert saved['audio'] == 'saved'
    assert saved['bitrix'] != 'saved'
    files=list((service.archive.folder(saved)/'audio').iterdir())
    assert len(files)==1 and files[0].read_bytes()==b'voice'
    assert service.settings.auto_download_audio is False


@pytest.mark.asyncio
async def test_audio_action_promotes_pending_text_task_without_losing_text_intent(service):
    meeting = service.db.upsert(service.settings.portal, record())
    original = service.request_download(meeting['id'], automatic=True)
    promoted = service.request_download(meeting['id'], audio_only=True)
    assert original == promoted
    payload = json.loads(service.db.rows('SELECT payload FROM jobs WHERE id=?',(original,))[0]['payload'])
    assert payload['automatic'] is False and payload['download_audio'] is True
    assert not payload.get('audio_only')


def test_new_profile_defaults_and_legacy_auto_local_invariant(tmp_path, settings):
    from meeting_archive.settings import Settings
    assert not Settings().auto_local and not Settings().auto_download_audio
    settings.auto_local, settings.auto_download_audio = True, False
    home = tmp_path/'legacy-profile'
    settings.save(home)
    loaded = Settings.load(home)
    assert loaded.auto_local and loaded.auto_download_audio


@pytest.mark.asyncio
async def test_audio_button_api_does_not_enable_global_audio_setting(ui):
    client, service, _ = ui
    meeting = service.db.upsert(service.settings.portal,record())
    csrf = await login(client)
    response = await client.post('/api/download',json={'ids':[meeting['id']],'audio_only':True},headers={'X-CSRF-Token':csrf})
    assert response.status_code==200
    payload = json.loads(service.db.rows('SELECT payload FROM jobs WHERE id=?',(response.json()['jobs'][0],))[0]['payload'])
    assert payload['audio_only'] and payload['download_audio']
    assert not service.settings.auto_download_audio


@pytest.mark.asyncio
async def test_scheduler_interval_and_bootstrap_timing_match_runtime(service, monkeypatch):
    pauses=[]
    async def stop_after_sleep(seconds):
        pauses.append(seconds)
        service.alive=False
    monkeypatch.setattr('meeting_archive.service.asyncio.sleep',stop_after_sleep)
    monkeypatch.setattr(service,'scan',AsyncMock())
    service.alive=True
    await service.scheduler()
    timing=service.automation_timing()
    assert pauses==[60]
    assert timing['catalogue_poll_seconds']==60 and timing['queue_poll_seconds']==.5
    assert timing['material_retry_seconds']==[60,300,86400]


@pytest.mark.asyncio
async def test_manual_download_keeps_audio_intent_when_retry_turns_automatic(service,monkeypatch):
    service.settings.auto_download_audio=True
    meeting=service.db.upsert(service.settings.portal,record())
    job_id=service.request_download(meeting['id'])
    transcript={'segments':[{'start':0,'end':1,'text':'Synthetic text'}]}
    responses=iter([record(transcription=transcript),record(transcription=transcript,
        tracks=[{'trackId':1,'fileName':'voice.wav','fileSize':5,'relUrl':'/recording'}])])
    async def followup(*args):
        return next(responses)
    monkeypatch.setattr(service.client,'followup',followup)
    await service.client.http.aclose()
    service.client.http=httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=b'voice')))
    await service.perform(service.db.claim(('fetch',)),lambda *_:None)
    queued=service.db.rows('SELECT * FROM jobs WHERE id=?',(job_id,))[0]
    payload=json.loads(queued['payload'])
    assert payload['automatic'] and payload['download_audio']
    service.settings.auto_download_audio=False
    service.db.job_update(job_id,next_at=0)
    assert await service.fetch(service.db.claim(('fetch',)),lambda *_:None)
    assert service.db.meeting(meeting['id'])['audio']=='saved'


@pytest.mark.asyncio
async def test_audio_request_during_portal_response_is_not_lost(service, monkeypatch):
    meeting = service.db.upsert(service.settings.portal, record())
    job_id = service.request_download(meeting['id'], automatic=True)
    job = service.db.claim(('fetch',))
    async def followup(*args):
        assert service.request_download(meeting['id'], audio_only=True) == job_id
        return record(transcription={'segments':[{'start':0,'end':1,'text':'Synthetic'}]},
                      tracks=[{'trackId':1,'fileName':'voice.wav','fileSize':5,'relUrl':'/recording'}])
    monkeypatch.setattr(service.client,'followup',followup)
    await service.client.http.aclose()
    service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=b'voice')))
    assert await service.fetch(job,lambda *_:None)
    saved = service.db.meeting(meeting['id'])
    assert saved['audio'] == 'saved' and saved['bitrix'] == 'saved'


@pytest.mark.asyncio
async def test_text_only_manual_retry_keeps_choice_after_setting_changes(service, monkeypatch):
    meeting = service.db.upsert(service.settings.portal, record())
    job_id = service.request_download(meeting['id'])
    tracks = [{'trackId': 1, 'fileName': 'voice.wav', 'fileSize': 5, 'relUrl': '/recording'}]
    responses = iter([record(tracks=tracks), record(tracks=tracks,
        transcription={'segments': [{'start': 0, 'end': 1, 'text': 'Synthetic'}]})])
    async def followup(*args):
        return next(responses)
    async def forbidden(*args, **kwargs):
        pytest.fail('Text-only retry downloaded audio')
    monkeypatch.setattr(service.client, 'followup', followup)
    monkeypatch.setattr('meeting_archive.archive.Archive.download', forbidden)
    await service.perform(service.db.claim(('fetch',)), lambda *_: None)
    queued = service.db.rows('SELECT * FROM jobs WHERE id=?', (job_id,))[0]
    assert queued['state'] == 'queued'
    assert json.loads(queued['payload']) == {'automatic': True, 'download_audio': False}
    service.settings.auto_download_audio = True
    service.db.job_update(job_id, next_at=0)
    assert await service.fetch(service.db.claim(('fetch',)), lambda *_: None)
    assert service.db.meeting(meeting['id'])['audio'] != 'saved'


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [1, 2])
async def test_manual_single_and_bulk_api_capture_text_only_policy(ui, count):
    client, service, _ = ui
    ids = [service.db.upsert(service.settings.portal, record(call_id=123+i))['id'] for i in range(count)]
    csrf = await login(client)
    response = await client.post('/api/download', json={'ids': ids}, headers={'X-CSRF-Token': csrf})
    assert response.status_code == 200 and len(response.json()['jobs']) == count
    for job_id in response.json()['jobs']:
        payload = json.loads(service.db.rows('SELECT payload FROM jobs WHERE id=?', (job_id,))[0]['payload'])
        assert payload == {'automatic': False, 'download_audio': False}


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_audio', [False, True])
async def test_new_text_request_updates_pending_choice_but_preserves_explicit_audio(service, explicit_audio):
    meeting = service.db.upsert(service.settings.portal, record())
    service.settings.auto_download_audio = True
    original = service.request_download(meeting['id'], audio_only=explicit_audio)
    service.settings.auto_download_audio = False
    assert service.request_download(meeting['id']) == original
    payload = json.loads(service.db.rows('SELECT payload FROM jobs WHERE id=?', (original,))[0]['payload'])
    assert payload['download_audio'] is explicit_audio
    assert not payload['audio_only']
