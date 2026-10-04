import json
from unittest.mock import AsyncMock

import pytest

from meeting_archive.archive import clean_metadata
from meeting_archive.app import meeting_view
from meeting_archive.bitrix import BitrixError
from test_app import login, ui as ui_fixture
from test_service import service as service_fixture

ui = ui_fixture
service = service_fixture


@pytest.mark.asyncio
async def test_titles_are_cached_and_chat_permission_denial_does_not_remove_catalogue(service):
    service.client.chat_title = AsyncMock(return_value="Синтетический проект")
    data = clean_metadata({"callId": 1, "chatId": 42})
    assert (await service.hydrate_chat(data))["chatTitle"] == "Синтетический проект"
    await service.hydrate_chat(clean_metadata({"callId": 2, "chatId": 42}))
    service.client.chat_title.assert_awaited_once()
    service.client.chat_title = AsyncMock(side_effect=BitrixError("insufficient_scope", auth=True))
    result = await service.hydrate_chat(clean_metadata({"callId": 3, "chatId": 43}))
    assert result["chatId"] == 43 and "chatTitle" not in result
    assert not service.auth_error and "im" in service.chat_warning


@pytest.mark.asyncio
async def test_chat_filter_is_separate_from_topic_and_combines_with_it(ui):
    client, service, _ = ui
    await login(client)
    for cid, title in [(41, "Проект А"), (42, "Проект Б")]:
        service.db.upsert(service.settings.portal, {"callId": cid, "chatId": cid, "chatTitle": title,
                                                  "overview": {"topic": "Одна тема"}})
    result = (await client.get("/api/meetings", params={"chat": "Проект А", "q": "Одна"})).json()
    assert len(result["items"]) == 1 and result["items"][0]["chat_id"] == 41
    assert (await client.get("/api/meetings", params={"chat": "42"})).json()["items"][0]["chat_title"] == "Проект Б"
    assert not (await client.get("/api/meetings", params={"chat": "42", "q": "нет"})).json()["items"]


def test_chat_id_and_title_are_preserved_in_meeting_view(service):
    row = service.db.upsert(service.settings.portal, {"callId": 1, "chatId": 12, "chatTitle": "Чат"})
    assert meeting_view(row)["chat_title"] == "Чат"
    assert json.loads(row["metadata"])["chatId"] == 12


@pytest.mark.asyncio
async def test_batch_warms_existing_catalogue_and_deduplicates_chat_ids(service):
    from test_service import record
    for call_id in range(3):
        service.db.upsert(service.settings.portal, record(call_id + 1, chatId=42))
    service.client.chat_titles = AsyncMock(return_value=({42: 'Один чат'}, {}))
    async def empty_catalogue(*_):
        yield []
    service.client.catalogue = empty_catalogue
    await service.scan()
    service.client.chat_titles.assert_awaited_once_with([42])
    assert all(json.loads(row['metadata'])['chatTitle'] == 'Один чат' for row in service.db.rows('SELECT * FROM meetings'))
    await service.scan()
    service.client.chat_titles.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_choices_group_meetings_and_multiselect_unions_chats(ui):
    client, service, _ = ui
    await login(client)
    for call_id, chat_id in [(1, 42), (2, 42), (3, 43), (4, 44)]:
        service.db.upsert(service.settings.portal, {'callId': call_id, 'chatId': chat_id, 'chatTitle': f'Чат {chat_id}'})
    choices = (await client.get('/api/chats')).json()['items']
    assert choices[0] == {'id': '42', 'label': 'Чат 42', 'count': 2}
    assert (await client.get('/api/meetings', params={'chats': '42,43'})).json()['total'] == 3
    assert (await client.get('/api/meetings', params={'chats': '42,43', 'q': '4'})).json()['total'] == 0
