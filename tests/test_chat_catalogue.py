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


@pytest.mark.asyncio
async def test_personal_dialog_name_requires_matching_internal_chat_id(settings, vault):
    import httpx
    from meeting_archive.bitrix import BitrixClient
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"result": {"result": {
            "personal_42": {"id": 42, "name": "Синтетический собеседник"},
            "personal_43": {"id": 999, "name": "Другой диалог"}}}})
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        assert await client.personal_chat_titles({42: 99, 43: 100}) == {42: "Личный диалог: Синтетический собеседник"}
        assert seen[0]["cmd"] == {"personal_42": "im.dialog.get?DIALOG_ID=99", "personal_43": "im.dialog.get?DIALOG_ID=100"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_personal_dialog_fallback_is_cached_and_preserves_participants(service):
    data = clean_metadata({"callId": 1, "chatId": 42, "participants": [
        {"userId": service.settings.user_id, "name": "Я"}, {"userId": 99, "name": "Собеседник"}]})
    service.client.chat_titles = AsyncMock(return_value=({}, {"chat_42": {"error": "DIALOG_ID_EMPTY"}}))
    service.client.personal_chat_titles = AsyncMock(return_value={42: "Личный диалог: Собеседник"})
    assert (await service.hydrate_chats([data]))[0]["chatTitle"] == "Личный диалог: Собеседник"
    service.client.personal_chat_titles.assert_awaited_once_with({42: 99}, {service.settings.user_id: "Я", 99: "Собеседник"})
    await service.hydrate_chats([data])
    service.client.personal_chat_titles.assert_awaited_once()
    view = meeting_view(service.db.upsert(service.settings.portal, data))
    assert [p["label"] for p in view["participants"]] == ["Я", "Собеседник"]


def test_ambiguous_or_group_participants_do_not_identify_a_personal_dialog(service):
    current = service.settings.user_id
    items = [{"chatId": 42, "participants": [{"userId": current}, {"userId": peer}]} for peer in [99, 100]]
    assert service.personal_chat_peers(items) == {}
    assert service.personal_chat_peers([{"chatId": 42, "participants": [{"userId": 99}, {"userId": 100}]}]) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,returned_id,expected", [("private",42,{42:"Личный диалог: Собеседник"}), ("chat",42,{}), ("private",999,{})])
async def test_empty_private_name_uses_followup_name_only_after_confirmation(settings, vault, kind, returned_id, expected):
    import httpx
    from meeting_archive.bitrix import BitrixClient
    client = BitrixClient(settings,vault,transport=httpx.MockTransport(lambda _: httpx.Response(200,json={"result":{"result":{
        "personal_42":{"id":returned_id,"type":kind,"name":""}}}})))
    try:
        assert await client.personal_chat_titles({42:99},{99:"Собеседник"}) == expected
    finally:
        await client.close()
