import hashlib
import json
import time

import httpx
import pytest

from meeting_archive.archive import atomic_text
from meeting_archive.chat_storage import ChatStore
from meeting_archive.chat_model import canonical
from meeting_archive.chat_queue import queue_view
from meeting_archive.app import create_app
from meeting_archive.recovery import restore_local, verify_local
from meeting_archive.service import Service


def saved_meeting(service, metadata, *, sample=False):
    meeting = service.db.upsert(service.settings.portal, metadata)
    folder = service.archive.ensure(meeting)
    audio = folder / 'audio/demo.wav'
    audio.write_bytes(b'synthetic audio')
    service.archive.remember_audio(folder, audio, digest=hashlib.sha256(audio.read_bytes()).hexdigest())
    atomic_text(folder / 'bitrix/transcript.md', 'Synthetic follow-up')
    atomic_text(folder / 'local/demo/transcript.txt', 'Synthetic local text')
    atomic_text(folder / 'local/demo/run.json', json.dumps({'sample': sample}))
    atomic_text(folder / 'notes/keep.md', 'Synthetic note')
    service.db.update_meeting(meeting['id'], folder=str(folder), audio='saved', bitrix='saved', local='tested' if sample else 'saved')
    service.archive.manifest(service.db.meeting(meeting['id']))
    return folder


@pytest.fixture
def recovery_service(tmp_path, settings, vault):
    settings.chat_archive_root = str(tmp_path / 'Chats')
    settings.save(tmp_path / 'profile')
    service = Service(tmp_path / 'profile', vault=vault)
    yield service
    service.db.close()


@pytest.mark.parametrize('sample', [False, True])
def test_fresh_index_restores_materials_without_writes_or_jobs(recovery_service, meeting, sample):
    service = recovery_service
    folder = saved_meeting(service, json.loads(meeting['metadata']), sample=sample)
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob('*') if p.is_file()}
    service.db.execute('DELETE FROM meetings')
    report = restore_local(service)
    restored = service.db.rows('SELECT * FROM meetings')[0]
    assert report['restored'] == 1 and report['errors'] == 0 and report['backup']
    assert restored['folder'] == str(folder) and restored['audio'] == restored['bitrix'] == 'saved'
    assert restored['local'] == ('tested' if sample else 'saved') and restored['requested'] == 0
    assert not service.db.rows('SELECT * FROM jobs')
    assert before == {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob('*') if p.is_file()}
    assert restore_local(service)['restored'] == 0
    assert len(service.db.rows('SELECT * FROM meetings')) == 1


def test_corrupt_manifest_does_not_hide_good_record_and_missing_audio_not_saved(recovery_service, meeting):
    service = recovery_service
    folder = saved_meeting(service, json.loads(meeting['metadata']))
    (folder / 'audio/demo.wav').unlink()
    bad = service.archive.root / 'bad/meeting.json'
    atomic_text(bad, '{broken')
    service.db.execute('DELETE FROM meetings')
    report = restore_local(service)
    assert report['restored'] == 1 and report['errors'] == 1 and report['missing'] == 1
    assert service.db.rows('SELECT audio FROM meetings')[0]['audio'] == 'not_saved'
    assert bad.read_text('utf-8') == '{broken'


def test_checksum_verification_separate_from_quick_restore(recovery_service, meeting):
    service = recovery_service
    folder = saved_meeting(service, json.loads(meeting['metadata']))
    audio = folder / 'audio/demo.wav'
    audio.write_bytes(b'changedxx audio')
    assert verify_local(service)['errors'] == 1
    assert audio.read_bytes() == b'changedxx audio'


def portable_chat(service, id=101):
    store = service.chat_archive.store()
    store.upsert_chat(id, f'chat{id}', title='Synthetic chat', participants_at=time.time())
    store.save_page(id, {'messages': [{'id': 1, 'date': '2026-10-01T00:00:00Z', 'author_id': 41, 'text': 'Synthetic'}]})
    service.chat_archive.request_history([id])
    store.flush(id)
    return store


def test_offline_single_account_restores_chats_and_holds_work(recovery_service):
    service = recovery_service
    store = portable_chat(service)
    folder = store.chat_folder(101)
    marker = (folder / 'chat.json').read_bytes()
    for table in ('ca_chats', 'ca_work', 'ca_messages'):
        service.db.execute(f'DELETE FROM {table}')
    service.settings.portal, service.settings.user_id = '', 0
    report = restore_local(service)
    assert report['restored'] == 1 and report['errors'] == 0
    assert service.chat_archive.store().account == store.account
    assert store.query(chat=101)['total'] == 1
    assert json.loads(service.db.rows('SELECT data FROM ca_work')[0]['data'])['held']
    assert (folder / 'chat.json').read_bytes() == marker
    assert queue_view(service.chat_archive)['held'] == 1
    service.chat_archive.control('continue_recovered')
    assert not json.loads(service.db.rows('SELECT data FROM ca_work')[0]['data']).get('held')


def test_tolerant_chat_recovery_preserves_corrupt_source(recovery_service):
    service = recovery_service
    store = portable_chat(service)
    marker = store.chat_folder(101) / 'chat.json'
    portable_chat(service, 102)
    for table in ('ca_chats', 'ca_work', 'ca_messages'):
        service.db.execute(f'DELETE FROM {table}')
    marker.write_text('{broken', 'utf-8')
    report = store.recover(tolerant=True, hold_work=True)
    assert report['restored'] == 1 and report['errors'] == 1
    assert marker.read_text('utf-8') == '{broken'
    assert store.chat(102)


async def test_stop_is_durable_cancels_manual_and_blocks_network(recovery_service):
    service = recovery_service
    store = portable_chat(service)
    store.enqueue(101, 'metadata')
    service.chat_archive.control('stop')
    assert not service.db.rows('SELECT * FROM ca_work')
    async def forbidden(*args, **kwargs):
        raise AssertionError('Stopped engine must not request Bitrix')
    service.client.call = forbidden
    await service.chat_archive.step()
    await service.chat_archive.file_step()
    service.db.execute("DELETE FROM state WHERE key LIKE 'ca:%'")
    store.recover()
    assert service.chat_archive.stopped(store)
    assert not service.db.rows('SELECT * FROM jobs')
    service.chat_archive.control('resume')
    assert not service.chat_archive.stopped(store)
    assert not service.db.rows("SELECT * FROM ca_work WHERE kind='history'")


async def test_recovery_and_stop_api_security(recovery_service):
    service = recovery_service
    portable_chat(service)
    app = create_app(service, launch_token='synthetic-recovery', manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://127.0.0.1:8765') as client:
        assert (await client.post('/api/archive/recover')).status_code == 401
        await client.get('/?launch=synthetic-recovery')
        headers = {'x-csrf-token': (await client.get('/api/bootstrap')).json()['csrf'], 'Origin': 'http://127.0.0.1:8765'}
        body = {'account': service.chat_archive.store().account, 'action': 'stop', 'confirm': True}
        assert (await client.post('/api/chat-archive/control', json={**body, 'account': 'other'}, headers=headers)).status_code == 400
        assert (await client.post('/api/chat-archive/control', json=body, headers=headers)).status_code == 200
        assert (await client.get('/api/bootstrap')).json()['chat_queue']['sync']['stopped']


def test_unchanged_messages_do_not_rewrite_month(recovery_service):
    service = recovery_service
    store = portable_chat(service)
    month = store.chat_folder(101) / 'messages/2026-10.jsonl'
    before = month.stat().st_mtime_ns
    store.save_page(101, {'messages': [{'id': 1, 'date': '2026-10-01T00:00:00Z', 'author_id': 41, 'text': 'Synthetic'}]})
    assert month.stat().st_mtime_ns == before


def test_multiple_offline_accounts_are_not_merged(recovery_service):
    service = recovery_service
    portable_chat(service)
    other = ChatStore(service.db, service.settings.chat_archive_root, 'other.bitrix24.ru', 99)
    other.upsert_chat(101, 'chat101')
    other.flush(101)
    service.settings.portal, service.settings.user_id = '', 0
    service.chat_archive.local_accounts = None
    assert not service.chat_archive.store().portal
    service.db.set_state('local_chat_account:' + service.settings.chat_archive_root, canonical({'portal': 'other.bitrix24.ru', 'user_id': 99}))
    assert service.chat_archive.store().account == other.account


async def test_startup_recovers_offline_without_source_calls(recovery_service, meeting):
    service = recovery_service
    saved_meeting(service, json.loads(meeting['metadata']))
    portable_chat(service)
    for table in ('meetings', 'ca_chats', 'ca_work', 'ca_messages'):
        service.db.execute(f'DELETE FROM {table}')
    service.settings.portal, service.settings.user_id = '', 0
    async def forbidden(*args, **kwargs):
        raise AssertionError('Offline recovery must not call the source')
    service.client.call = forbidden
    await service.start()
    try:
        assert service.recovery_status['restored'] == 2
        assert service.chat_archive.store().query(chat=101)['total'] == 1
        assert service.db.rows('SELECT local FROM meetings')[0]['local'] == 'saved'
        assert not service.db.rows('SELECT * FROM jobs')
    finally:
        await service.stop()


def test_recovery_fills_existing_catalogue_row_without_overwriting_archive(recovery_service):
    service = recovery_service
    store = portable_chat(service)
    marker = store.chat_folder(101) / 'chat.json'
    before = marker.read_bytes()
    service.db.execute('DELETE FROM ca_messages')
    service.db.execute('DELETE FROM ca_work')
    # The old app can already have rediscovered this chat before local indexing.
    store.upsert_chat(101, 'chat101', title='Fresh catalogue title')
    result = store.recover(tolerant=True, hold_work=True)
    assert result['restored'] == 1 and result['errors'] == 0
    assert store.query(chat=101)['total'] == 1 and store.chat(101)['title'] == 'Fresh catalogue title'
    assert store.chat(101)['manual_history_requested']
    assert marker.read_bytes() == before
    assert store.recover(tolerant=True, hold_work=True)['restored'] == 0


def test_existing_folder_wrong_states_are_repaired_without_touching_files(recovery_service, meeting):
    service = recovery_service
    folder = saved_meeting(service, json.loads(meeting['metadata']))
    marker = folder / 'meeting.json'
    before = marker.read_bytes()
    id = service.db.rows('SELECT id FROM meetings')[0]['id']
    service.db.update_meeting(id, audio='not_saved', bitrix='not_saved', local='not_saved', requested=1)
    assert restore_local(service)['restored'] == 1
    restored = service.db.meeting(id)
    assert restored['audio'] == restored['bitrix'] == restored['local'] == 'saved'
    assert restored['requested'] == 1 and marker.read_bytes() == before
