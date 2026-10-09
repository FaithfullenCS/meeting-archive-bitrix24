import asyncio
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from meeting_archive.bitrix import BitrixClient, BitrixError
from meeting_archive.service import Service


@pytest.fixture
def batch_service(tmp_path, settings, vault):
    settings.chat_archive_root = str(tmp_path / 'Chats')
    settings.chat_auto_save = True
    settings.auth_mode = 'webhook'
    vault.update(webhook='https://synthetic.bitrix24.ru/rest/41/synthetic-key/')
    settings.save(tmp_path / 'profile')
    client = BitrixClient(settings, vault)
    client.request_interval = 0
    service = Service(tmp_path / 'profile', vault=vault, client=client)
    client.settings = service.settings
    store = service.chat_archive.store()
    for key, value in {'collection_policy': 5, 'last_discovery': time.time(), 'auto_since': '2026-01-01T00:00:00Z',
                       'recent_check_at': time.time(), 'audit_at': time.time()}.items():
        store.set_state(key, value)
    service.chat_archive.recovered.add(store.account)
    yield service
    service.db.close()


def queue_chats(service, n=3):
    store = service.chat_archive.store()
    for id in range(1, n + 1):
        store.upsert_chat(id, f'chat{id}', participants_at=time.time(), source_last_message_id=1)
        service.chat_archive.schedule_chat(store, store.chat(id), True)
    return store


def response(commands, error_chat=None):
    results, errors = {}, {}
    for key, command in commands.items():
        method, query = command.split('?', 1)
        params = parse_qs(query)
        assert method == 'im.dialog.messages.search'
        assert params['ORDER[ID]'] == ['DESC']
        assert params['DATE_FROM'] == ['2026-01-01T00:00:00Z']
        id = int(params['CHAT_ID'][0])
        if id == error_chat:
            errors[key] = {'error': 'ACCESS_ERROR', 'error_description': 'Synthetic denied'}
        else:
            results[key] = {'messages': [{'id': 1, 'date': '2026-10-01T00:00:00Z', 'text': 'Synthetic', 'author_id': 41}]}
    return {'result': {'result': results, 'result_error': errors}}


async def test_mixed_batch_denial_does_not_lose_success_or_repeat_denied(batch_service):
    service = batch_service
    store = queue_chats(service)
    calls = []
    def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, json=response(payload['cmd'], error_chat=2))
    await service.client.http.aclose()
    service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    await service.chat_archive.step()
    assert len(calls) == 1 and len(calls[0]['cmd']) == 3 and calls[0]['halt'] == 0
    assert store.query(chat=1)['total'] == store.query(chat=3)['total'] == 1
    assert store.chat(2)['access_blocked'] and store.query(chat=2)['total'] == 0
    for _ in range(3):
        await service.chat_archive.step()
    assert len(calls) == 1
    assert service.client.metrics['batch']['requests'] == 1
    assert service.client.metrics['im.dialog.messages.search']['subrequests'] == 3
    await service.client.close()


async def test_inner_method_budget_is_respected(batch_service):
    service = batch_service
    calls = []
    def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, json={'result': {'result': {}, 'result_error': {
            'a': {'error': 'OPERATION_TIME_LIMIT'}}, 'result_time': {'a': {'operating_reset_at': time.time()+120, 'operating': 250}}}})
    await service.client.http.aclose()
    service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    first = await service.client.batch_pages({'a': ('im.dialog.messages.search', {'CHAT_ID': 1})})
    assert isinstance(first['a'], BitrixError) and first['a'].retry_at > time.time()
    second = await service.client.batch_pages({'b': ('im.dialog.messages.search', {'CHAT_ID': 2})})
    assert len(calls) == 1 and second['b'].code == 'RATE_LIMITED'
    await service.client.close()


async def test_missing_result_never_advances_cursor(batch_service):
    service = batch_service
    store = queue_chats(service, 2)
    await service.client.http.aclose()
    service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        'result': {'result': {'page_0': {'messages': []}}, 'result_error': {}}})))
    await service.chat_archive.step()
    work = store.db.rows('SELECT data FROM ca_work WHERE account=? AND chat=2', (store.account,))
    assert len(work) == 1 and json.loads(work[0]['data'])['cursor'] == 0
    assert not store.db.rows('SELECT data FROM ca_work WHERE account=? AND chat=1', (store.account,))
    await service.client.close()


async def test_stop_during_batch_prevents_new_pages_and_survives_restart(batch_service):
    service = batch_service
    store = queue_chats(service, 2)
    entered, release = asyncio.Event(), asyncio.Event()
    async def transport(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=response(json.loads(request.content)['cmd']))
    await service.client.http.aclose()
    service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    task = asyncio.create_task(service.chat_archive.step())
    await entered.wait()
    service.chat_archive.stop_requested = store.account
    release.set()
    await task
    service.chat_archive.control('stop')
    assert store.query(chat=1)['total'] == store.query(chat=2)['total'] == 0
    assert not service.db.rows('SELECT * FROM ca_work')
    service.chat_archive.stop_requested = None
    store.recover()
    assert service.chat_archive.stopped(store)
    await service.client.close()


async def test_734_chats_use_bounded_adaptive_batches(batch_service, record_property):
    service = batch_service
    store = queue_chats(service, 734)
    sizes = []
    def transport(request):
        payload = json.loads(request.content)
        sizes.append(len(payload['cmd']))
        return httpx.Response(200, json=response(payload['cmd']))
    await service.client.http.aclose()
    service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    started = time.monotonic()
    for _ in range(80):
        if not service.db.rows('SELECT * FROM ca_work WHERE account=?', (store.account,)):
            break
        await service.chat_archive.step()
    elapsed = time.monotonic() - started
    assert store.summary()['messages'] == 734
    assert not service.db.rows('SELECT * FROM ca_work WHERE account=?', (store.account,))
    assert sizes[0] == 10 and max(sizes) <= 50 and sum(sizes) == 734 and len(sizes) <= 74
    record_property('http_requests', len(sizes))
    record_property('synthetic_seconds', elapsed)
    record_property('unbatched_http_requests', 734)
    before = len(sizes)
    for chat in store.chats():
        service.chat_archive.schedule_chat(store, chat, True)
    await service.chat_archive.step()
    assert len(sizes) == before  # No repeated sweep for unchanged heads.
    await service.client.close()
