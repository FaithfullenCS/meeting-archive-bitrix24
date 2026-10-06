from __future__ import annotations

import json
import asyncio
import time
from datetime import datetime, timezone, timedelta
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from meeting_archive.app import create_app
from meeting_archive.bitrix import BitrixClient, BitrixError
from meeting_archive.chat_model import category, normalize, text_html
from meeting_archive.chat_storage import ChatStore
from meeting_archive.service import Service


def message(id, chat=101, **values):
    return {"id": id, "chat_id": chat, "author_id": 42, "date": "2026-09-18T10:00:00+03:00", "text": f"Сообщение {id}", **values}


@pytest.fixture
def chat_service(tmp_path, settings, vault):
    home = tmp_path / "profile"
    settings.portal, settings.user_id = "synthetic.bitrix24.ru", 42
    settings.chat_archive_root = str(tmp_path / "Chats")
    settings.save(home)
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"synthetic file")))
    service = Service(home, vault=vault, client=client)
    client.settings = service.settings
    yield service
    service.db.close()


def seed(service, id=101):
    store = service.chat_archive.store()
    store.upsert_chat(id, f"chat{id}", title="Синтетическая беседа", participants=[{"id": 42, "name": "Участник"}])
    return store


def test_portable_identity_versions_and_cross_month_context(chat_service):
    store = seed(chat_service)
    store.save_page(101, {"messages": [message(1, date="2026-08-30T10:00:00Z")], "users": [{"id": 42, "name": "Автор"}]})
    reply = message(2, text="[quote]Снимок[/quote]\nОтвет", params={"REPLY_ID": 1})
    forwarded = message(3, author_id=7, text="Пересылаю", forward=[{"id": 50, "chatId": 909, "userId": 8}, {"id": 51, "chatId": 909, "userId": 9}])
    page = {"messages": [reply, forwarded], "additionalMessages": [message(50, 909, author_id=8, text="Исходная версия"), message(51, 909, author_id=9)], "users": [{"id": 7, "name": "Отправитель"}, {"id": 8, "name": "Автор оригинала"}]}
    store.save_page(101, page)
    store.save_page(101, page)
    assert store.query(chat=101)["total"] == 3
    assert not store.versions(101, 3)
    current = store.message(101, 3)
    assert current["author_id"] == 7 and current["relations"][0]["author_id"] == 8
    assert [link["message_id"] for link in current["relations"]] == [50, 51]
    store.save_page(101, {"messages": [message(1, date="2026-08-30T10:00:00Z", text="Правка")]})
    assert len(store.versions(101, 1)) == 1
    assert store.context(101, 909, 50)[0]["text"] == "Исходная версия"
    md = (store.chat_folder(101) / "messages/2026-09.md").read_text("utf-8")
    assert "2026-08.md#message-1" in md and "Автор оригинала" in md
    notes = store.chat_folder(101) / "notes/user.md"
    notes.write_text("Keep my notes", "utf-8")
    store.flush(101)
    assert notes.read_text("utf-8") == "Keep my notes"
    # The same portable files rebuild an empty index, including old revisions.
    for table in ("ca_chats", "ca_messages", "ca_versions", "ca_context", "ca_files", "ca_work"):
        chat_service.db.execute(f"DELETE FROM {table} WHERE account=?", (store.account,))
    store.recover()
    assert store.query(chat=101)["total"] == 3 and len(store.versions(101, 1)) == 1
    other = ChatStore(chat_service.db, store.root, store.portal, 43)
    assert other.query()["total"] == 0 and not other.chats()


def test_quotes_are_not_attributed_and_markup_cannot_execute():
    value = normalize(message(1, text="[quote]Кто-то сказал[/quote] >> цитата", replaces={"1": "замена"}), 101)
    assert value["quote"] and not value["relations"]
    rendered = text_html('<script>run()</script>[url=javascript:evil]open[/url][b]Жирный[/b][unknown]текст[/unknown]')
    assert "<script>" not in rendered and "href=\"javascript" not in rendered
    assert "<strong>Жирный</strong>" in rendered and "[unknown]" in rendered
    assert text_html('[url=https://[bad]Synthetic text[/url]')=='[url=https://[bad]Synthetic text[/url]'


@pytest.mark.parametrize("name,flags,expected", [("photo.png", {}, "images"), ("plan.xlsx", {}, "documents"), ("unknown.dat", {"isVoice": True}, "audio"), ("clip.mp4", {}, "video"), ("backup.zip", {}, "other")])
def test_attachment_categories(name, flags, expected):
    assert category({"name": name, **flags}) == expected


def test_filters_pagination_and_no_scope_changes(chat_service):
    store = seed(chat_service)
    store.save_page(101, {"messages": [message(i, author_id=42 if i % 2 else 7, text="ПРОВЕРКА цитаты" if i==4 else "текст", params={"FILE_ID": [99]} if i == 4 else {}) for i in range(1,61)], "files": [{"id":99,"name":"doc.pdf","size":15}]})
    assert len(store.query(chat=101)["items"]) == 50
    assert len(store.query(chat=101,offset=50)["items"]) == 10
    assert store.query(q="проверка", attachment="documents", direction="incoming")["items"][0]["id"] == 4
    assert store.query(participant=999)["total"] == 0
    assert chat_service.settings.chat_scope == "all" and not chat_service.settings.chat_auto_save


async def test_latest_pages_all_chats_before_history_and_idempotent_backfill(chat_service):
    service, calls = chat_service, []
    async def call(method, params, **kwargs):
        calls.append((method, dict(params)))
        if method == "im.recent.list":
            return {"items": [{"type":"chat", "id":101,"chat_id":101,"title":"Чат A"}, {"type":"chat","id":102,"chat_id":102,"title":"Чат B"}], "hasMore":False}
        if method == "im.dialog.get":
            return {"id":int(params["DIALOG_ID"].removeprefix("chat"))}
        if method == "im.chat.user.list":
            return [42]
        if method == "im.user.list.get":
            return [{"id":42,"name":"Участник"}]
        id = params["CHAT_ID"]
        before = params.get("LAST_ID", 431)
        return {"messages": [message(i,id) for i in range(min(430,before-1),max(0,min(430,before-1)-200),-1)]}
    service.client.call = call
    service.chat_archive.request_discovery()
    for _ in range(12):
        await service.chat_archive.step()
    searches = [params for method,params in calls if method == "im.dialog.messages.search"]
    assert searches[0]["CHAT_ID"] == 101 and searches[1]["CHAT_ID"] == 102
    store = service.chat_archive.store()
    assert store.query(chat=101)["total"] == 430 and store.query(chat=102)["total"] == 430
    assert store.chat(101)["history_complete"] and store.chat(101)["coverage"] == "source_boundary"
    service.chat_archive.request_history()
    for _ in range(6):
        await service.chat_archive.step()
    assert store.query(chat=101)["total"] == 430 and not store.versions(101, 1)


async def test_projection_failure_keeps_cursor_recovery_and_no_duplicate(chat_service, monkeypatch):
    service = chat_service
    store = seed(service)
    store.enqueue(101,"history",{"cursor":0})
    async def call(*args, **kwargs):
        return {"messages":[message(i) for i in range(1,201)]}
    service.client.call=call
    original=ChatStore.flush
    def fail(self, chat):
        raise OSError("Synthetic disk full")
    monkeypatch.setattr(ChatStore,"flush",fail)
    # Avoid startup recovery swallowing the intended injected page failure.
    service.chat_archive.recovered.add(store.account)
    await service.chat_archive.step()
    work=service.db.rows("SELECT data FROM ca_work WHERE account=?",(store.account,))[0]
    assert json.loads(work["data"])["cursor"] == 0 and store.query()["total"] == 200
    monkeypatch.setattr(ChatStore,"flush",original)
    store.recover()
    service.db.execute("UPDATE ca_work SET next_at=0")
    await service.chat_archive.step()
    assert store.query()["total"] == 200 and not store.versions(101,1)
    assert json.loads(service.db.rows("SELECT data FROM ca_work WHERE account=?",(store.account,))[0]["data"])["cursor"] == 1


async def test_tariff_limit_and_access_failure_not_empty_archive(chat_service):
    store = seed(chat_service)
    store.enqueue(101,"history")
    async def tariff(*args,**kwargs):
        return {"messages":[message(1)],"tariffRestrictions":{"isHistoryLimitExceeded":True}}
    chat_service.client.call=tariff
    await chat_service.chat_archive.step()
    assert store.chat(101)["coverage"] == "tariff_limited"
    store.enqueue(101,"new",{"stop_id":1})
    async def denied(*args,**kwargs):
        raise BitrixError("Synthetic denied",code="ACCESS_ERROR")
    chat_service.client.call=denied
    await chat_service.chat_archive.step()
    assert store.chat(101)["coverage"] == "access_lost" and store.query()["total"] == 1
    assert not store.message(101,1)["deleted"]


async def test_events_versions_delete_and_ack_after_projection(chat_service):
    service = chat_service
    store = seed(service)
    store.save_page(101,{"messages":[message(1,text="Первый текст")]})
    event = {"eventId":3,"type":"ONIMV2MESSAGEUPDATE","data":{"chat":{"id":101},"message":{"id":1,"text":"Правка"}}}
    calls=[]
    async def call(method,params,**kwargs):
        calls.append((method,params))
        if method == "im.v2.Event.get":
            return {"events":[event],"nextOffset":4}
        return True
    service.client.call=call
    await service.chat_archive.events(store)
    assert store.message(101,1)["text"] == "Правка" and len(store.versions(101,1))==1
    await service.chat_archive.events(store)
    assert calls[-1][1]["offset"]==4 and len(store.versions(101,1))==1
    event.update(eventId=5,type="ONIMV2MESSAGEDELETE",data={"chat":{"id":101},"message":{"id":1}})
    await service.chat_archive.events(store)
    assert store.message(101,1)["deleted"] and store.message(101,1)["text"]=="Правка"
    assert (store.folder/"events.jsonl").exists()


async def test_manual_file_bypasses_disabled_types_and_size_limit(chat_service):
    service = chat_service
    store = seed(service)
    content=b"synthetic file"
    store.save_page(101,{"messages":[message(1,params={"FILE_ID":[77]})],"files":[{"id":77,"name":"CON.pdf","size":len(content),"downloadUrl":"https://invalid.test/?token=SECRET"}]})
    async def call(method,params,**kwargs):
        assert method == "im.v2.File.download"
        return {"downloadUrl":"https://synthetic.bitrix24.ru/download"}
    service.client.call=call
    service.chat_archive.queue_file(store,101,77,automatic=True)
    assert store.file(101,77)["state"]=="not_saved"
    service.chat_archive.queue_file(store,101,77)
    await service.chat_archive.file_step()
    file=store.file(101,77)
    assert file["state"]=="saved" and Path(store.chat_folder(101)/file["path"]).read_bytes()==content
    assert "SECRET" not in (store.chat_folder(101)/"chat.json").read_text("utf-8")
    assert "_CON.pdf" in file["path"]


async def test_attachment_history_opt_in_and_disable_preserves_downloaded(chat_service):
    service=chat_service
    store=seed(service)
    store.save_page(101,{"messages":[message(1,params={"FILE_ID":[77]})],"files":[{"id":77,"name":"image.png"}]})
    old=replace(service.settings)
    service.settings.chat_auto_save=True
    service.settings.chat_download_images=True
    await service.chat_archive.settings_changed(old)
    assert store.file(101,77)["state"]=="not_saved"
    old=replace(service.settings)
    service.settings.chat_download_history=True
    await service.chat_archive.settings_changed(old)
    assert store.file(101,77)["state"]=="queued"
    old=replace(service.settings)
    service.settings.chat_download_images=False
    await service.chat_archive.settings_changed(old)
    assert store.file(101,77)["state"]=="not_saved"


async def test_api_account_isolation_csrf_and_shared_settings(chat_service):
    service=chat_service
    store=seed(service)
    store.save_page(101,{"messages":[message(1,text="Синтетический пример")]})
    app=create_app(service,launch_token="test-capability",manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url="http://127.0.0.1:8765") as client:
        assert (await client.get("/api/chat-archive")).status_code==401
        await client.get("/?launch=test-capability")
        csrf=(await client.get("/api/bootstrap")).json()["csrf"]
        for asset in ("chat-archive.js", "chat-archive.css"):
            assert (await client.get("/static/"+asset)).status_code==200
        assert (await client.post("/api/chat-archive/sync")).status_code==403
        headers={"X-CSRF-Token":csrf}
        response=await client.post("/api/settings",json={"chat_poll_seconds":300,"chat_download_documents":True},headers=headers)
        assert response.status_code==200 and service.settings.chat_poll_seconds==300
        assert (await client.get("/api/chat-archive/messages?q=синтетический")).json()["total"]==1
        assert (await client.post("/api/chat-archive/backfill",json={"date_from":"bad"},headers=headers)).status_code==400
        response=await client.post("/api/chat-archive/export",json={"ids":[101]},headers=headers)
        assert response.status_code==200 and (await client.get(response.json()["url"])).status_code==200
        service.settings.user_id=43
        assert (await client.get("/api/chat-archive/messages")).json()["total"]==0


@pytest.mark.parametrize("damage", ["invalid", "truncated", "missing"])
def test_corrupt_recovery_leaves_source_and_can_resume(chat_service, damage):
    store=seed(chat_service)
    store.save_page(101,{"messages":[message(1)]})
    path=store.chat_folder(101)/"messages/2026-09.jsonl"
    good=path.read_bytes()
    manifest=(store.chat_folder(101)/"chat.json").read_bytes()
    chat_service.db.execute("DELETE FROM ca_chats WHERE account=?",(store.account,))
    chat_service.db.execute("DELETE FROM ca_messages WHERE account=?",(store.account,))
    if damage=="missing":
        path.unlink()
    else:
        path.write_text("{broken" if damage=="invalid" else "",encoding="utf-8")
    with pytest.raises(ValueError):
        store.recover()
    assert not store.chats() and (store.chat_folder(101)/"chat.json").read_bytes()==manifest
    path.write_bytes(good)
    store.recover()
    assert store.query()["total"]==1


def test_later_original_repairs_portable_link_without_changing_quote(chat_service):
    store=seed(chat_service)
    store.save_page(101,{"messages":[message(10,text="Ответ",reply={"id":1,"text":"Снимок цитаты"})]})
    path=store.chat_folder(101)/"messages/2026-09.md"
    assert "Оригинал недоступен" in path.read_text("utf-8")
    store.save_page(101,{"messages":[message(1,date="2026-08-01T10:00:00Z",text="Последняя редакция оригинала")]})
    text=path.read_text("utf-8")
    assert "2026-08.md#message-1" in text and "Снимок цитаты" in text


async def test_reaction_event_is_idempotent_and_preserves_relations(chat_service):
    service=chat_service
    store=seed(service)
    store.save_page(101,{"messages":[message(1,quote={"id":99,"text":"Цитата"},fileIds=[77])]})
    event={"eventId":3,"type":"ONIMV2REACTIONCHANGE","data":{"chat":{"id":101},"message":{"id":1},"user":{"id":7},"reaction":"like","action":"add"}}
    async def call(method,params,**kwargs):
        return {"events":[event],"nextOffset":4} if method=="im.v2.Event.get" else True
    service.client.call=call
    await service.chat_archive.events(store)
    await service.chat_archive.events(store)
    value=store.message(101,1)
    assert value["reactions"]["reactionCounters"]["like"]==1
    assert value["file_ids"]==[77] and value["relations"][0]["kind"]=="quote"


async def test_slow_file_does_not_hold_message_sync_lock(chat_service):
    service=chat_service
    store=seed(service)
    store.save_page(101,{"messages":[message(1,params={"FILE_ID":[77]})],"files":[{"id":77,"name":"doc.txt"}]})
    waiting,release=asyncio.Event(),asyncio.Event()
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            waiting.set()
            await release.wait()
            yield b"synthetic"
    await service.client.http.aclose()
    service.client.http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=SlowStream())))
    async def call(method,params,**kwargs):
        return {"downloadUrl":"https://synthetic.bitrix24.ru/file"} if method=="im.v2.File.download" else {"messages":[message(2)]}
    service.client.call=call
    service.chat_archive.queue_file(store,101,77)
    file_task=asyncio.create_task(service.chat_archive.file_step())
    await asyncio.wait_for(waiting.wait(),2)
    store.enqueue(101,"new",{"stop_id":1})
    try:
        await asyncio.wait_for(service.chat_archive.step(),1)
        assert store.message(101,2)
    finally:
        release.set()
        await file_task


async def test_file_contents_same_id_preserved_on_redownload(chat_service):
    service=chat_service
    store=seed(service)
    store.save_page(101,{"messages":[message(1,fileIds=[77])],"files":[{"id":77,"name":"same.txt"}]})
    data={"body":b"first"}
    await service.client.http.aclose()
    service.client.http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=data["body"])))
    async def call(*args,**kwargs):
        return {"downloadUrl":"https://synthetic.bitrix24.ru/file"}
    service.client.call=call
    service.chat_archive.queue_file(store,101,77)
    await service.chat_archive.file_step()
    data["body"]=b"second"
    # Metadata may be unchanged; an explicit manual refresh still re-downloads.
    service.chat_archive.queue_file(store,101,77)
    await service.chat_archive.file_step()
    assert len(store.file(101,77)["contents"])==2
    assert all((store.chat_folder(101)/entry["path"]).is_file() for entry in store.file(101,77)["contents"])


async def test_excluded_event_content_is_not_archived(chat_service):
    service=chat_service
    store=seed(service)
    service.settings.chat_excluded_ids=[101]
    event={"eventId":5,"type":"ONIMV2MESSAGEADD","data":{"chat":{"id":101},"message":message(9,text="Excluded synthetic content")}}
    async def call(method,params,**kwargs):
        return {"events":[event],"nextOffset":6} if method=="im.v2.Event.get" else True
    service.client.call=call
    await service.chat_archive.events(store)
    assert not store.message(101,9)
    journal=(store.folder/"events.jsonl").read_text("utf-8")
    assert "Excluded synthetic content" not in journal and '"skipped":true' in journal


def test_structured_quote_keeps_received_excerpt_and_name_lookup_is_not_edit(chat_service):
    store=seed(chat_service)
    page={"messages":[message(2,text="Reply",quote={"id":1,"text":"Original received excerpt"}),message(3,text="Forward",forward={"id":1})],"additionalMessages":[message(1,text="Current original text")]}
    store.save_page(101,page)
    assert store.query(kind="quote")["total"]==1
    assert store.message(101,2)["relations"][0]["excerpt"]=="Original received excerpt"
    page["users"]=[{"id":7,"name":"Synthetic author"}]
    page["additionalMessages"][0]["text"]="Changed original text"
    store.save_page(101,page)
    assert not store.versions(101,2)
    assert not store.versions(101,3) and store.message(101,3)["relations"][0]["excerpt"]=="Current original text"


async def test_new_account_auto_download_cutoff_is_fixed_and_timezone_aware(chat_service):
    service=chat_service
    store=seed(service)
    service.settings.chat_auto_save=True
    service.settings.chat_download_documents=True
    for key in ("last_discovery", "recent_check_at", "audit_at"):
        store.set_state(key,time.time())
    await service.chat_archive.step()
    cutoff=store.state("auto_since")
    assert cutoff
    raw=message(1,date=datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-5))).isoformat(),fileIds=[77])
    async def call(method,params,**kwargs):
        return {"messages":[raw],"files":[{"id":77,"name":"new.txt","size":9}]}
    service.client.call=call
    store.enqueue(101,"new",{"stop_id":0,"automatic":True},0)
    await service.chat_archive.step()
    assert store.state("auto_since")==cutoff and store.file(101,77)["state"]=="queued"


async def test_event_enable_before_connection_applies_only_to_first_account(chat_service):
    service=chat_service
    service.settings.user_id=0
    old=replace(service.settings)
    service.settings.chat_events=True
    await service.chat_archive.settings_changed(old)
    assert service.db.get_state("chat_events:pending_consent")=="1"
    service.settings.user_id=42
    calls=[]
    async def call(method,params,**kwargs):
        calls.append(method)
        return {"events":[],"nextOffset":0} if method=="im.v2.Event.get" else True
    service.client.call=call
    await service.chat_archive.step()
    assert "im.v2.Event.subscribe" in calls
    service.settings.user_id=43
    calls.clear()
    await service.chat_archive.step()
    assert not calls and not service.db.get_state("chat_events:pending_consent")


async def test_recovered_automatic_file_keeps_download_policy(chat_service):
    service=chat_service
    store=seed(service)
    store.save_page(101,{"messages":[message(1,fileIds=[77])],"files":[{"id":77,"name":"file.txt","size":9}]})
    store.file_update(101,77,state="running",automatic=1,attempts=2,next_at=0,error="Synthetic retry")
    store.flush(101)
    for table in ("ca_chats", "ca_messages", "ca_files"):
        service.db.execute("DELETE FROM "+table+" WHERE account=?",(store.account,))
    store.recover()
    file=store.file(101,77)
    assert file["state"]=="queued" and file["automatic"]==1 and file["attempts"]==2
    service.settings.chat_auto_save=True
    service.settings.chat_download_documents=True
    service.settings.paused=True
    async def forbidden(*args,**kwargs):
        raise AssertionError("An automatic file must not become manual after recovery")
    service.client.call=forbidden
    await service.chat_archive.file_step()
    assert store.file(101,77)["state"]=="queued"
