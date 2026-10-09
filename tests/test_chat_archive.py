from __future__ import annotations

import json
import asyncio
import time
import threading
from datetime import datetime, timezone, timedelta
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from meeting_archive.app import create_app
from meeting_archive.bitrix import BitrixClient, BitrixError
from meeting_archive.chat_model import category, normalize, text_html
from meeting_archive.chat_storage import ChatStore
from meeting_archive.chat_model import chat_classification
from meeting_archive.chat_queue import queue_view, queue_action
from meeting_archive.chat_cleanup import material_plan, remove_materials
from meeting_archive.chat_queue import cancel_backlog, cancel_selected
from meeting_archive.service import Service


async def test_catalogue_orders_unsaved_chats_by_source_message_date(chat_service):
    store = seed(chat_service)
    store.update_chat(101, source_last_message_at="2026-09-18T10:00:00+03:00", source_last_message_id=5)
    store.save_page(101, {"messages": [message(1, date="2026-10-01T00:00:00Z")]})
    store.upsert_chat(102, "chat102", source_last_message_at="2026-09-18T10:00:00Z", source_last_message_id=6)
    store.upsert_chat(103, "7", type="user", title="Демо: неактивный сотрудник", peer_active=False,
                      source_last_message_at="2026-09-18T11:00:00+03:00", source_last_message_id=7)
    app = create_app(chat_service, launch_token="synthetic-order", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        await client.get("/?launch=synthetic-order")
        items = (await client.get("/api/chat-archive")).json()["items"]
    assert [item["id"] for item in items] == [102, 103, 101]
    assert items[1]["peer_active"] is False and items[1]["count"]["n"] == 0


async def test_extra_recent_keeps_inactive_dialog_without_saving_text(chat_service):
    store = seed(chat_service)
    store.update_chat(101, source_recent_seen_at=time.time(), source_last_message_at="2026-10-01T00:00:00Z")
    async def call(method, params, **kwargs):
        assert method == "im.recent.get"
        assert params["SKIP_DIALOG"] == "N"
        return [
            {"id":"chat101", "chat_id":101, "type":"chat", "message":{"id":1,"date":"2020-01-01T00:00:00Z"}},
            {"id":7,"chat_id":102,"type":"user","title":"Демо: бывший сотрудник", "user":{"active":False},
             "message":{"id":10,"date":"2026-09-18T13:00:00+03:00","text":"Do not store this text"}},
        ]
    chat_service.client.call = call
    await chat_service.chat_archive.extra_recent(store)
    assert store.chat(102)["peer_active"] is False
    assert store.chat(102)["source_last_message_at"] == "2026-09-18T10:00:00+00:00"
    assert not store.chat(102)["auto_history"] and store.query(chat=102)["total"] == 0
    assert store.chat(101)["source_last_message_at"] == "2026-10-01T00:00:00Z"
    store.flush(102)
    assert "Do not store" not in (store.chat_folder(102) / "chat.json").read_text("utf-8")
    # Unchanged supplemental inventory is read no more than daily.
    async def forbidden(*args, **kwargs):
        raise AssertionError("Repeated supplement must not call the portal")
    chat_service.client.call = forbidden
    await chat_service.chat_archive.extra_recent(store)


async def test_unavailable_extra_list_preserves_primary_discovery(chat_service):
    store = seed(chat_service)
    async def call(method, params, **kwargs):
        if method == "im.recent.get":
            raise BitrixError("Method unavailable", code="METHOD_NOT_FOUND")
        return {"items":[{"id":"chat102","chat_id":102,"type":"chat","message":{"id":5,"date":"2026-10-01T00:00:00Z"}}],"hasMore":False}
    chat_service.client.call = call
    await chat_service.chat_archive.discover(store, False)
    assert store.state("inventory_ready") and store.chat(102)["source_last_message_id"] == 5
    assert store.state("extra_recent_warning") and store.query(chat=102)["total"] == 0


async def test_new_self_message_not_starved_by_large_catalogue(chat_service, monkeypatch):
    service, store = chat_service, chat_service.chat_archive.store()
    service.settings.chat_auto_save = True
    service.settings.chat_poll_seconds = 60
    now = [time.time()]
    monkeypatch.setattr("meeting_archive.chat_sync.time.time", lambda: now[0])
    for key, value in {
        "collection_policy": 5,
        "inventory_ready": True,
        "last_discovery": now[0],
        "auto_since": "2026-01-01T00:00:00Z",
        "recent_check_at": now[0],
        "audit_at": now[0],
        "extra_recent_at": now[0],
    }.items():
        store.set_state(key, value)
    for id in range(1, 81):
        store.upsert_chat(
            id, "42" if id == 80 else f"chat{id}", type="user" if id == 80 else "chat", participants_at=now[0]
        )
        service.chat_archive.schedule_chat(store, store.chat(id), True)

    async def call(method, params, **kwargs):
        if method == "im.recent.list":
            return {
                "items": [
                    {
                        "id": 42 if id == 80 else f"chat{id}",
                        "chat_id": id,
                        "type": "user" if id == 80 else "chat",
                        "message": {"id": 2 if id == 80 else 1, "date": "2026-12-01T00:00:00Z"},
                    }
                    for id in range(1, 81)
                ],
                "hasMore": False,
            }
        assert method == "im.dialog.messages.search"
        return (
            {"messages": [message(2, 80, date="2026-12-01T00:00:00Z")]} if params["CHAT_ID"] == 80 else {"messages": []}
        )

    service.client.call = call
    for _ in range(90):
        await service.chat_archive.step()
        now[0] += 1
    assert store.message(80, 2), (
        "New self message must be checked even when a new polling cycle starts before the catalogue drains"
    )


async def test_history_does_not_change_order_without_remote_date(chat_service):
    store = seed(chat_service)
    store.upsert_chat(102, "chat102")
    app = create_app(chat_service, launch_token="synthetic-order-stable", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        await client.get("/?launch=synthetic-order-stable")
        before = [c["id"] for c in (await client.get("/api/chat-archive")).json()["items"]]
        store.save_page(101, {"messages": [message(1, date="2026-12-01T00:00:00Z")]})
        after = [c["id"] for c in (await client.get("/api/chat-archive")).json()["items"]]
    assert before == after


def test_task_collections_new_paths_and_legacy_links(chat_service):
    store = seed(chat_service)
    legacy = store.folder / "chats/chat-101"
    (legacy / "notes").mkdir(parents=True)
    (legacy / "notes/kept.md").write_text("Synthetic existing note", "utf-8")
    classification = chat_classification(
        {"chat": {"type": "tasksTask", "entity_type": "TASKS_TASK", "entity_id": "900"}}
    )
    assert classification["task_id"] == 900
    assert not chat_classification({"name": "TASKS_TASK in a title"})
    store.update_chat(101, **classification)
    store.upsert_chat(102, "chat102", **classification)
    store.save_page(101, {"messages": [message(1)]})
    store.save_page(102, {"messages": [message(2, 102)]})
    assert store.chat_folder(101) == legacy and (legacy / "notes/kept.md").is_file()
    assert store.chat_folder(102) == store.folder / "chats/tasks/chat-102"
    index = json.loads((store.folder / "collections/tasks.json").read_text("utf-8"))
    assert {c["id"] for c in index["chats"]} == {101, 102}
    assert store.query(type="tasks")["total"] == 2 and store.query(type="conversations")["total"] == 0
    for table in ("ca_chats", "ca_messages"):
        store.db.execute(f"DELETE FROM {table} WHERE account=?", (store.account,))
    store.recover()
    assert store.query(type="tasks")["total"] == 2


async def test_queue_includes_chat_files_outcomes_and_account_scoped_actions(chat_service):
    store = seed(chat_service)
    store.enqueue(101, "history", {"automatic": False}, 2)
    store.save_page(
        101, {"messages": [message(1, params={"FILE_ID": [77]})], "files": [{"id": 77, "name": "demo.txt", "size": 1}]}
    )
    chat_service.chat_archive.queue_file(store, 101, 77)
    entries = queue_view(chat_service.chat_archive)["items"]
    assert {e["kind"] for e in entries} >= {"history", "file"}
    history = next(e for e in entries if e["kind"] == "history")
    queue_action(chat_service.chat_archive, history["id"], "cancel")
    assert not store.db.rows("SELECT kind FROM ca_work WHERE account=? AND kind='history'", (store.account,))
    assert any(e["state"] == "cancelled" for e in queue_view(chat_service.chat_archive)["items"])
    queue_action(chat_service.chat_archive, history["id"], "retry")
    assert store.db.rows("SELECT kind FROM ca_work WHERE account=? AND kind='history'", (store.account,))
    chat_service.settings.user_id = 43
    with pytest.raises(ValueError, match="аккаунта"):
        queue_action(chat_service.chat_archive, history["id"], "cancel")


async def test_self_chat_alias_and_unchanged_heads_do_not_requeue(chat_service):
    store = seed(chat_service)
    service = chat_service
    service.settings.chat_auto_save = True
    store.set_state("inventory_ready", True)
    store.set_state("auto_since", "2026-01-01T00:00:00Z")

    async def call(method, params, **kwargs):
        if method == "im.recent.get":
            return []
        return {
            "items": [
                {
                    "id": 42,
                    "chat_id": 101,
                    "type": "user",
                    "title": "Мои заметки",
                    "user": {"name": "Демо владелец"},
                    "message": {"id": 1, "date": "2026-12-01T00:00:00Z"},
                }
            ],
            "hasMore": False,
        }

    service.client.call = call
    await service.chat_archive.discover(store, True)
    assert store.chat(101)["is_self"] and "Демо владелец" in store.chat(101)["aliases"]
    store.db.execute("DELETE FROM ca_work WHERE account=?", (store.account,))
    store.update_chat(101, sync_head_id=1, new_checked_at=time.time(), participants_at=time.time())
    service.chat_archive.schedule_chat(store, store.chat(101), True)
    assert not store.db.rows("SELECT kind FROM ca_work WHERE account=? AND kind='new'", (store.account,))


async def test_manual_history_has_turn_under_continuous_new_work(chat_service):
    store=seed(chat_service)
    service=chat_service
    service.settings.chat_auto_save=True
    for key,value in {"collection_policy":5,"last_discovery":time.time(),"recent_check_at":time.time(),"audit_at":time.time(),"inventory_ready":True}.items():
        store.set_state(key,value)
    store.enqueue(101,"new",{"automatic":True},-1)
    store.enqueue(101,"history",{"automatic":False},2)
    kinds=[]
    async def process(store,row):
        kinds.append(row["kind"])
        if row["kind"]=="history":
            store.db.execute("DELETE FROM ca_work WHERE account=? AND kind='history'",(store.account,))
    service.chat_archive.process_work=process
    for _ in range(5):
        await service.chat_archive.step()
    assert "history" in kinds


async def test_message_worker_recovers_from_unexpected_exception(chat_service,monkeypatch):
    service=chat_service
    calls=[]
    async def step():
        calls.append(1)
        if len(calls)==1:
            raise RuntimeError("synthetic transient failure")
        service.alive=False
    original_sleep=asyncio.sleep
    async def sleep(seconds):
        await original_sleep(0)
    service.chat_archive.step=step
    service.alive=True
    monkeypatch.setattr("meeting_archive.chat_sync.asyncio.sleep",sleep)
    await service.chat_archive.loop()
    assert len(calls)==2 and "synthetic transient failure" in service.chat_archive.error


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
    # Per-method mocks model batch transport as independent outcomes. Actual
    # batch encoding, rate budgets and HTTP counts have separate integration tests.
    async def batch_commands(commands):
        outcomes = {}
        for key, (method, params) in commands.items():
            if "ORDER[ID]" in params:
                params = {k: v for k, v in params.items() if k != "ORDER[ID]"}
                params["ORDER"] = {"ID": "DESC"}
            try:
                outcomes[key] = await service.client.call(method, params, v3=False)
            except (BitrixError, ValueError, OSError, httpx.HTTPError) as exc:
                outcomes[key] = exc
        return outcomes
    client.batch_pages = batch_commands
    yield service
    service.db.close()


def seed(service, id=101):
    store = service.chat_archive.store()
    store.upsert_chat(id, f"chat{id}", title="Синтетическая беседа", participants=[{"id": 42, "name": "Участник"}], participants_at=time.time())
    return store


def cleanup_seed(service):
    store = seed(service)
    service.settings.chat_download_documents = True
    service.settings.chat_auto_save = True
    store.set_state("collection_policy", 5)
    store.set_state("auto_since", "2020-01-01T00:00:00Z")
    store.save_page(101, {"messages": [message(1, fileIds=[77])], "files": [{"id": 77, "name": "demo.txt", "size": 9}]})
    folder = store.chat_folder(101)
    saved = folder / "attachments/file-77/hash_demo.txt"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_bytes(b"synthetic")
    data = store.file(101, 77)
    data.update(path=saved.relative_to(folder).as_posix(), contents=[{"path": saved.relative_to(folder).as_posix(), "sha256": "synthetic", "size": 9}])
    store.file_update(101, 77, state="saved", data=json.dumps(data))
    (folder / "notes/keep.md").write_text("Synthetic note", "utf-8")
    store.flush(101)
    return store, folder


def test_cancel_backlog_keeps_new_message_polling_and_meeting_scope(chat_service):
    store, _ = cleanup_seed(chat_service)
    store.enqueue(101, "history", {"automatic": False}, 2)
    store.enqueue(101, "period:2026-01-01:2026-01-31", {"automatic": False}, 2)
    store.enqueue(101, "new", {"automatic": True}, 0)
    chat_service.chat_archive.current_work = None
    result = cancel_backlog(chat_service.chat_archive)
    assert result["cancelled"] >= 2
    assert not store.db.rows("SELECT 1 FROM ca_work WHERE account=? AND chat=? AND kind='history'", (store.account, 101))
    assert not store.db.rows("SELECT 1 FROM ca_work WHERE account=? AND chat=? AND kind LIKE 'period:%'", (store.account, 101))
    assert store.db.rows("SELECT 1 FROM ca_work WHERE account=? AND chat=? AND kind='new'", (store.account, 101))


def test_cancel_selected_handles_all_queue_pages_and_leaves_running_visible(chat_service):
    store = seed(chat_service)
    store.enqueue(101, "history", {"automatic": False}, 2)
    store.enqueue(101, "new", {"automatic": True}, 0)
    items = queue_view(chat_service.chat_archive, all_items=True)["items"]
    result = cancel_selected(chat_service.chat_archive, [item["id"] for item in items])
    assert result["cancelled"] >= 1 and result["requested"] == len(items)
    assert result["running"] == 0
    assert not store.db.rows("SELECT 1 FROM ca_work WHERE account=? AND chat=? AND kind='history'", (store.account, 101))
    assert not store.db.rows("SELECT 1 FROM ca_work WHERE account=? AND chat=? AND kind='new'", (store.account, 101))


def test_bulk_cancel_uses_one_snapshot_and_persists_across_recovery(chat_service, monkeypatch):
    import meeting_archive.chat_queue as queue
    store = seed(chat_service)
    for number in range(779):
        day = (datetime(2020, 1, 1) + timedelta(days=number)).date().isoformat()
        store.enqueue(101, f"period:{day}:{day}", {"automatic": False}, 2)
    store.flush(101)
    ids = [item["id"] for item in queue.queue_view(chat_service.chat_archive, all_items=True)["items"]]
    original = queue.queue_view
    calls = []
    def snapshot(*args, **kwargs):
        calls.append(1)
        assert len(calls) == 1, "Bulk cancellation must not look jobs up in a changing/pruned queue"
        return original(*args, **kwargs)
    monkeypatch.setattr(queue, "queue_view", snapshot)
    result = queue.cancel_selected(chat_service.chat_archive, ids + ["already-finished-synthetic"])
    assert result["cancelled"] == 779 and result["skipped"] == 1
    assert not store.db.rows("SELECT kind FROM ca_work WHERE account=?", (store.account,))
    store.db.execute("DELETE FROM ca_chats WHERE account=?", (store.account,))
    store.recover()
    assert not store.db.rows("SELECT kind FROM ca_work WHERE account=?", (store.account,))


@pytest.mark.parametrize("kind", ["new", "history", "metadata"])
async def test_access_error_stops_polling_until_explicit_retry(chat_service, monkeypatch, kind):
    service = chat_service
    store = seed(service)
    service.settings.chat_auto_save = True
    clock = [time.time()]
    monkeypatch.setattr("meeting_archive.chat_sync.time.time", lambda: clock[0])
    for key, value in {"collection_policy":5,"auto_since":"2020-01-01T00:00:00Z",
                       "last_discovery":clock[0],"recent_check_at":clock[0],"audit_at":clock[0]}.items():
        store.set_state(key, value)
    service.chat_archive.recovered.add(store.account)
    store.enqueue(101, kind, {"automatic": kind == "new"}, 2)
    calls = []
    async def denied(method, params, **kwargs):
        calls.append(method)
        raise BitrixError("Bitrix24: ACCESS_ERROR", code="ACCESS_ERROR")
    service.client.call = denied
    await service.chat_archive.step()
    failed = next(item for item in queue_view(service.chat_archive)["items"] if item["kind"] == kind)
    assert failed["state"] == "failed" and not failed.get("will_retry", False)
    for _ in range(3):
        clock[0] += 60
        store.set_state("last_discovery", clock[0])
        service.chat_archive.schedule_chat(store, store.chat(101), True)
        await service.chat_archive.step()
    assert len(calls) == 1, "Access denied must not be retried automatically"
    queue_action(service.chat_archive, failed["id"], "retry")
    await service.chat_archive.step()
    assert len(calls) == 2, "Explicit Retry must permit another source request"


async def test_bulk_cancel_api_ignores_finished_keys_and_rejects_changed_account(chat_service):
    service = chat_service
    store = seed(service)
    store.enqueue(101, "history", {"automatic": False}, 2)
    store.save_page(101, {"messages": [message(1)]})
    app = create_app(service, launch_token="synthetic-bulk", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://127.0.0.1:8765") as client:
        await client.get("/?launch=synthetic-bulk")
        csrf = (await client.get("/api/bootstrap")).json()["csrf"]
        headers = {"X-CSRF-Token": csrf}
        selected = (await client.get("/api/chat-archive/queue?all_items=1")).json()
        ids = [item["id"] for item in selected["items"]] + ["synthetic-finished"]
        body = {"ids": ids, "account": selected["account"], "confirm": True}
        assert (await client.post("/api/chat-archive/queue/bulk-cancel", json=body)).status_code == 403
        assert (await client.post("/api/chat-archive/queue/bulk-cancel", json={**body,"confirm":False}, headers=headers)).status_code == 400
        assert (await client.post("/api/chat-archive/queue/bulk-cancel", json={**body,"account":"other-synthetic-account"}, headers=headers)).status_code == 400
        assert store.db.rows("SELECT kind FROM ca_work WHERE account=?", (store.account,))
        response = await client.post("/api/chat-archive/queue/bulk-cancel", json=body, headers=headers)
        assert response.status_code == 200 and response.json()["cancelled"] == 1 and response.json()["skipped"] == 1
        again = await client.post("/api/chat-archive/queue/bulk-cancel", json=body, headers=headers)
        assert again.status_code == 200 and again.json()["cancelled"] == 0
    assert store.query(chat=101)["total"] == 1


async def test_legacy_access_loss_and_automatic_files_make_no_source_requests(chat_service):
    service = chat_service
    store, _ = cleanup_seed(service)
    store.update_chat(101, coverage="access_lost", error="Bitrix24: ACCESS_ERROR")
    store.enqueue(101, "new", {"automatic": True}, 0)
    store.file_update(101, 77, state="queued", automatic=1)
    store.set_state("last_discovery", time.time())
    service.chat_archive.recovered.add(store.account)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Known denied chats must not call the source")
    service.client.call = forbidden
    service.chat_archive.schedule_chat(store, store.chat(101), True)
    await service.chat_archive.step()
    await service.chat_archive.file_step()
    assert store.file(101, 77)["state"] == "unavailable"
    assert not next(item for item in queue_view(service.chat_archive)["items"] if item["kind"] == "new")["will_retry"]


async def test_chat_message_removal_clears_index_and_does_not_refetch_old(chat_service):
    store, folder = cleanup_seed(chat_service)
    store.save_page(101, {"messages": [message(1, fileIds=[77], text="Changed synthetic") ]})
    for event_id, event in enumerate([
        {"data": {"chat": {"id": 101}, "message": {"text": "Synthetic removed"}}},
        {"data": {"message": {"chatId": 101, "text": "Synthetic removed"}}},
        {"data": {"chatId": 101, "message": {"text": "Synthetic removed"}}},
        {"chatId": 101, "skipped": True},
        {"data": {"chatId": 102, "message": {"text": "Synthetic preserved"}}},
    ], 1):
        store.db.execute("INSERT INTO ca_events VALUES(?,?,?)", (store.account, event_id, json.dumps(event)))
    store.enqueue(101, "history", {"automatic": False}, 2)
    export = chat_service.home / "exports" / (store.account + ".zip")
    export.parent.mkdir()
    export.write_bytes(b"synthetic export")
    plan = material_plan(chat_service.chat_archive, [101], ["messages"])
    remove_materials(chat_service.chat_archive, chat_service.home, plan["ids"], plan["targets"], plan["token"])
    assert not store.query(chat=101)["total"] and not store.versions(101, 1)
    assert not (folder / "messages").exists() and not (folder / "versions").exists()
    assert (folder / "notes/keep.md").read_text("utf-8") == "Synthetic note"
    assert (folder / "attachments/file-77/hash_demo.txt").exists() and not export.exists()
    events = store.db.rows("SELECT id FROM ca_events WHERE account=?", (store.account,))
    assert [r["id"] for r in events] == [5]
    journal = (store.folder / "events.jsonl").read_text("utf-8")
    assert "Synthetic removed" not in journal and "Synthetic preserved" in journal
    assert not store.chat(101)["manual_history_requested"] and store.chat(101)["message_delete_after"]
    chat_service.chat_archive.schedule_chat(store, store.chat(101), True)
    queued = json.loads(store.db.rows("SELECT data FROM ca_work WHERE account=? AND kind='new'", (store.account,))[0]["data"])
    assert queued["date_from"] == store.chat(101)["message_delete_after"]
    async def call(method, params, **kwargs):
        return {"messages": [message(1)]}
    chat_service.client.call = call
    row = store.db.rows("SELECT * FROM ca_work WHERE account=? AND kind='new'", (store.account,))[0]
    await chat_service.chat_archive.process_work(store, row)
    assert not store.query(chat=101)["total"]
    chat_service.chat_archive.request_history([101])
    assert not store.chat(101)["message_delete_after"]


def test_chat_attachment_removal_suppresses_automatic_until_manual_download(chat_service):
    store, folder = cleanup_seed(chat_service)
    plan = material_plan(chat_service.chat_archive, [101], ["attachments/file-77"])
    remove_materials(chat_service.chat_archive, chat_service.home, plan["ids"], plan["targets"], plan["token"])
    assert store.query(chat=101)["total"] == 1 and not (folder / "attachments/file-77").exists()
    file = store.file(101, 77)
    assert file["locally_deleted"] and not file.get("path") and not file.get("contents")
    chat_service.chat_archive.queue_file(store, 101, 77, automatic=True, history_opt_in=True)
    assert store.file(101, 77)["state"] == "not_saved"
    store.save_page(101, {"messages": [message(1, fileIds=[77])], "files": [{"id": 77, "name": "demo.txt"}]})
    assert store.file(101, 77)["locally_deleted"]
    chat_service.chat_archive.queue_file(store, 101, 77)
    assert store.file(101, 77)["state"] == "queued" and not store.file(101, 77).get("locally_deleted")


def test_chat_removal_stale_preview_and_account_scope(chat_service):
    store, folder = cleanup_seed(chat_service)
    plan = material_plan(chat_service.chat_archive, [101], ["messages"])
    store.save_page(101, {"messages": [message(2)]})
    with pytest.raises(ValueError, match="изменился"):
        remove_materials(chat_service.chat_archive, chat_service.home, plan["ids"], plan["targets"], plan["token"])
    assert store.query(chat=101)["total"] == 2 and (folder / "messages/2026-09.md").exists()
    chat_service.settings.user_id = 43
    with pytest.raises(ValueError):
        material_plan(chat_service.chat_archive, [101], ["messages"])


def test_chat_interrupted_delete_recovers_without_resurrection(chat_service, monkeypatch):
    import meeting_archive.chat_cleanup as cleanup
    store, folder = cleanup_seed(chat_service)
    plan = material_plan(chat_service.chat_archive, [101], ["messages"])
    original = cleanup.remove_selection
    def fail(*args, **kwargs):
        if args[0] == folder:
            raise OSError("Synthetic interrupted deletion")
        return original(*args, **kwargs)
    monkeypatch.setattr(cleanup, "remove_selection", fail)
    with pytest.raises(OSError):
        remove_materials(chat_service.chat_archive, chat_service.home, plan["ids"], plan["targets"], plan["token"])
    assert (folder / ".delete-pending.json").exists()
    monkeypatch.setattr(cleanup, "remove_selection", original)
    # Recovery also works when the index is lost before the next start.
    store.db.execute("DELETE FROM ca_chats WHERE account=?", (store.account,))
    store.recover()
    assert not store.query(chat=101)["total"] and not (folder / ".delete-pending.json").exists()
    assert (folder / "notes/keep.md").exists()


async def test_chat_remove_api_requires_session_csrf_confirmation_and_fresh_plan(chat_service):
    store, folder = cleanup_seed(chat_service)
    app = create_app(chat_service, launch_token="synthetic-remove", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://127.0.0.1:8765") as client:
        assert (await client.post("/api/chat-archive/materials/plan", json={"ids": [101]})).status_code == 401
        await client.get("/?launch=synthetic-remove")
        csrf = (await client.get("/api/bootstrap")).json()["csrf"]
        assert (await client.post("/api/chat-archive/materials/plan", json={"ids": [101]})).status_code == 403
        headers = {"X-CSRF-Token": csrf}
        plan = (await client.post("/api/chat-archive/materials/plan", json={"ids": [101], "targets": ["notes"]}, headers=headers)).json()
        assert {c["target"] for c in plan["choices"]} >= {"messages", "notes", "attachments"}
        request = {"ids": plan["ids"], "targets": plan["targets"], "token": plan["token"]}
        assert (await client.post("/api/chat-archive/materials/remove", json=request, headers=headers)).status_code == 400
        response = await client.post("/api/chat-archive/materials/remove", json={**request, "confirm": True}, headers=headers)
        assert response.status_code == 200
    assert store.query(chat=101)["total"] == 1 and not (folder / "notes/keep.md").exists()


def test_chat_cleanup_rejects_traversal_and_running_download(chat_service):
    store, folder = cleanup_seed(chat_service)
    with pytest.raises(ValueError):
        material_plan(chat_service.chat_archive, [101], ["attachments/../notes"])
    store.file_update(101, 77, state="running")
    with pytest.raises(ValueError, match="завершения"):
        material_plan(chat_service.chat_archive, [101], ["attachments"])
    assert (folder / "notes/keep.md").exists()


def background_inventory(service, monkeypatch):
    """Run the real application/message/file lifecycle, with no UI or real I/O."""
    async def idle(*args):
        await asyncio.Future()

    for name in ("job_loop", "scheduler", "discover_resources", "refresh_identity"):
        monkeypatch.setattr(service, name, idle)
    store = seed(service)
    for key, value in {"collection_policy": 5, "inventory_ready": True,
                       "last_discovery": time.time(), "extra_recent_at": time.time(),
                       "recent_check_at": time.time(), "audit_at": time.time(),
                       "auto_since": "2026-01-01T00:00:00Z"}.items():
        store.set_state(key, value)
    return store


async def wait_background(predicate, timeout=8):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.02)


@pytest.mark.parametrize("paused", [False, True])
async def test_manual_history_runs_in_application_lifespan_without_ui(chat_service, monkeypatch, paused):
    service = chat_service
    store = background_inventory(service, monkeypatch)
    service.settings.chat_auto_save = False
    service.settings.paused = paused
    cursors = []

    async def call(method, params, **kwargs):
        assert method == "im.dialog.messages.search"
        cursor = params.get("LAST_ID", 0)
        cursors.append(cursor)
        return {"messages": [message(i) for i in (range(205, 5, -1) if not cursor else range(5, 0, -1))]}

    service.client.call = call
    service.chat_archive.request_history([101])
    app = create_app(service, manage_lifecycle=True)
    # No HTTP client, bootstrap, catalogue, chat selection or desktop-ready call.
    async with app.router.lifespan_context(app):
        await wait_background(lambda: store.chat(101).get("history_complete"))
        assert cursors == [0, 6]
        assert store.query(chat=101)["total"] == 205
        assert (store.chat_folder(101) / "messages/2026-09.md").is_file()
        assert service.chat_archive.status()["worker_running"]
        await wait_background(lambda: store.state("last_progress", {}).get("messages") == 205)
        finished = next(e for e in queue_view(service.chat_archive)["items"] if e["kind"] == "history")
        assert finished["state"] == "done" and finished["messages"] == 205 and finished["pages"] == 2
    assert not service.chat_archive.loop_running


async def test_new_message_polled_in_background_without_ui(chat_service, monkeypatch):
    service = chat_service
    store = background_inventory(service, monkeypatch)
    service.settings.chat_auto_save = True
    service.settings.chat_poll_seconds = 300
    store.update_chat(101, sync_head_id=1, source_last_message_id=1, new_checked_at=time.time())
    moment = [time.time()]
    monkeypatch.setattr("meeting_archive.chat_sync.time.time", lambda: moment[0])
    methods = []

    async def call(method, params, **kwargs):
        methods.append(method)
        if method == "im.recent.list":
            return {"items": [{"id": "chat101", "chat_id": 101, "type": "chat",
                               "message": {"id": 2, "date": "2026-12-01T00:00:00Z"}}], "hasMore": False}
        assert method == "im.dialog.messages.search"
        return {"messages": [message(2, date="2026-12-01T00:00:00Z")]}

    service.client.call = call
    app = create_app(service, manage_lifecycle=True)
    async with app.router.lifespan_context(app):
        await wait_background(lambda: getattr(service.chat_archive, "loop_running", False))
        assert not methods
        moment[0] += 301
        await wait_background(lambda: store.message(101, 2) is not None)
        assert methods == ["im.recent.list", "im.dialog.messages.search"]


async def test_history_resumes_after_process_restart_without_ui(chat_service, monkeypatch):
    service = chat_service
    store = background_inventory(service, monkeypatch)
    service.settings.chat_auto_save = False
    service.settings.save(service.home)
    page_two = asyncio.Event()
    cursors = []

    async def call(method, params, **kwargs):
        assert method == "im.dialog.messages.search"
        cursor = params.get("LAST_ID", 0)
        cursors.append(cursor)
        if cursor:
            page_two.set()
            await asyncio.Future()  # Simulate exit while a network request is pending.
        return {"messages": [message(i) for i in range(205, 5, -1)]}

    service.client.call = call
    service.chat_archive.request_history([101])
    app = create_app(service, manage_lifecycle=True)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(page_two.wait(), 8)
        assert store.query(chat=101)["total"] == 200
    client = BitrixClient(service.settings, service.vault, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    restarted = Service(service.home, vault=service.vault, client=client)
    client.settings = restarted.settings
    resumed_store = background_inventory(restarted, monkeypatch)

    async def resume(method, params, **kwargs):
        assert method == "im.dialog.messages.search"
        cursors.append(params.get("LAST_ID", 0))
        return {"messages": [message(i) for i in range(5, 0, -1)]}

    client.call = resume
    app = create_app(restarted, manage_lifecycle=True)
    async with app.router.lifespan_context(app):
        await wait_background(lambda: resumed_store.chat(101).get("history_complete"))
        assert resumed_store.query(chat=101)["total"] == 205
        assert cursors == [0, 6, 6]


async def test_manual_attachment_download_runs_without_ui(chat_service, monkeypatch):
    service = chat_service
    store = background_inventory(service, monkeypatch)
    service.settings.chat_auto_save = False
    service.settings.paused = True
    content = b"synthetic file"
    store.save_page(101, {"messages": [message(1, fileIds=[77])],
                          "files": [{"id": 77, "name": "background.txt", "size": len(content)}]})

    async def call(method, params, **kwargs):
        assert method == "im.v2.File.download"
        return {"downloadUrl": "https://synthetic.bitrix24.ru/background.txt"}

    service.client.call = call
    service.chat_archive.queue_file(store, 101, 77)
    app = create_app(service, manage_lifecycle=True)
    async with app.router.lifespan_context(app):
        await wait_background(lambda: store.file(101, 77)["state"] == "saved")
        await wait_background(lambda: store.state("last_progress", {}).get("kind") == "file")
        file = store.file(101, 77)
        assert (store.chat_folder(101) / file["path"]).read_bytes() == content
    assert not service.chat_archive.file_loop_running


async def test_shutdown_drains_inflight_chat_write_before_closing_database(chat_service, monkeypatch):
    service = chat_service
    store = background_inventory(service, monkeypatch)
    folder = store.chat_folder(101)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = ChatStore.save_page

    def slow_write(self, *args):
        started.set()
        assert release.wait(5), "Test must release the synthetic disk write"
        result = original(self, *args)
        finished.set()
        return result

    monkeypatch.setattr(ChatStore, "save_page", slow_write)
    async def call(method, params, **kwargs):
        assert method == "im.dialog.messages.search"
        return {"messages": [message(1)]}

    service.client.call = call
    service.chat_archive.request_history([101])
    await service.start()
    stopping = None
    try:
        await wait_background(started.is_set)
        stopping = asyncio.create_task(service.stop())
        await asyncio.sleep(.05)
        assert not stopping.done(), "Shutdown must wait for the native disk thread, not just cancel its await"
        assert service.db.rows("SELECT count(*) AS n FROM ca_chats")[0]["n"] == 1
    finally:
        release.set()
        await (stopping if stopping else service.stop())
    assert finished.is_set()
    assert (folder / "messages/2026-09.jsonl").is_file()


def test_queue_group_totals_include_unloaded_files_and_waiting_reason(chat_service):
    service = chat_service
    store = seed(service)
    store.update_chat(101, group="tasks", task_id=900)
    service.settings.paused = True
    store.enqueue(101, "history", {"automatic": False}, 2)
    store.enqueue(101, "new", {"automatic": True}, 0)
    store.save_page(101, {"messages": [message(1, fileIds=list(range(1, 6)))],
                          "files": [{"id": i, "name": f"demo-{i}.txt"} for i in range(1, 6)]})
    for id in range(1, 6):
        service.chat_archive.queue_file(store, 101, id)
    store.activity("audit", 101, "failed", error="Synthetic old outcome")
    view = queue_view(service.chat_archive, limit=2)
    assert len(view["items"]) == 2 and view["total"] == 8
    assert view["pending"] == 7 and view["failed"] == 1
    assert view["groups"] == [{"key": "chat:101", "title": "Синтетическая беседа", "chat": 101,
                               "group": "tasks", "task_id": 900, "total": 8, "pending": 7, "failed": 1, "running": 0}]
    entries = queue_view(service.chat_archive, all_items=True)["items"]
    new = next(e for e in entries if e["kind"] == "new")
    manual = next(e for e in entries if e["kind"] == "history")
    assert new["schedule_wait"] and new["message"] == "Автоматизация на паузе"
    assert not manual["schedule_wait"]


async def test_bootstrap_queue_names_meeting_and_does_not_export_payload(chat_service):
    service = chat_service
    row = service.db.upsert(service.settings.portal, {"callId": "synthetic-call", "overview": {"topic": "Demo meeting"}})
    service.db.enqueue("fetch", row["id"], {"automatic": False, "test_internal_field": "SYNTHETIC_PRIVATE"})
    app = create_app(service, launch_token="synthetic-queue-names", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        await client.get("/?launch=synthetic-queue-names")
        response = await client.get("/api/bootstrap")
    assert response.status_code == 200
    assert response.json()["jobs"][0]["title"] == "Demo meeting"
    assert "SYNTHETIC_PRIVATE" not in response.text and "meeting_metadata" not in response.json()["jobs"][0]


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
        if method == "im.recent.get":
            return []
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
    await service.chat_archive.step()
    store = service.chat_archive.store()
    assert store.summary()["count"] == 2 and not store.query()["total"]
    service.chat_archive.request_history([101, 102])
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
    store.enqueue(101,"history",{"cursor":0,"automatic":False},2)
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
    store.enqueue(101,"history",{"automatic":False},2)
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
        return {"downloadUrl":"https://synthetic.bitrix24.ru/file"} if method=="im.v2.File.download" else {"messages":[message(2,date="2099-01-01T00:00:00Z")]}
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
    assert not any(method.startswith("im.v2.Event") for method in calls) and not service.db.get_state("chat_events:pending_consent")


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


async def test_inventory_baseline_new_chats_and_existing_cutoff(chat_service):
    service = chat_service
    service.settings.chat_auto_save = True
    service.settings.chat_history_since = "2099-01-01"  # Legacy hidden date cannot clip the new policy.
    round = 0
    calls = []
    future = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
    async def call(method, params, **kwargs):
        calls.append((method, dict(params)))
        if method == "im.recent.get":
            return []
        if method == "im.recent.list":
            ids = [101] if round == 0 else [101, 102]
            return {"items": [{"id": id, "chat_id": id, "type": "chat"} for id in ids], "hasMore": False}
        if method == "im.dialog.messages.search":
            id = params["CHAT_ID"]
            return {"messages": [message(3, id, date=future), message(2, id), message(1, id)]}
        return {"id": 101} if method == "im.dialog.get" else []
    service.client.call = call
    await service.chat_archive.step()
    store = service.chat_archive.store()
    assert store.state("inventory_ready") and not store.chat(101).get("auto_history")
    assert store.query(chat=101)["total"] == 1 and store.message(101, 3)
    round = 1
    service.chat_archive.request_discovery()
    for _ in range(8):
        await service.chat_archive.step()
    assert not store.chat(102).get("auto_history") and not store.chat(102)["history_complete"]
    assert store.query(chat=102)["total"] == 1 and store.query(chat=101)["total"] == 1
    assert all(params.get("DATE_FROM") for method, params in calls if method == "im.dialog.messages.search")
    assert not service.db.rows("SELECT kind FROM ca_work WHERE account=? AND kind='history'", (store.account,))
    # A later explicit full-history request grants access to the old messages.
    service.chat_archive.request_history([102])
    for _ in range(3):
        await service.chat_archive.step()
    assert store.chat(102)["history_complete"] and store.chat(102)["manual_history_requested"]
    assert store.query(chat=102)["total"] == 3 and store.query(chat=101)["total"] == 1


def test_legacy_auto_history_flag_cannot_unbound_new_message_job(chat_service):
    store = seed(chat_service)
    store.set_state("auto_since", "2026-10-01T00:00:00Z")
    store.update_chat(101, auto_history=True)
    chat_service.chat_archive.schedule_chat(store, store.chat(101), True)
    rows = store.db.rows("SELECT kind,data FROM ca_work WHERE account=? AND chat=?", (store.account, 101))
    assert [r["kind"] for r in rows] == ["new"]
    assert json.loads(rows[0]["data"])["date_from"] == "2026-10-01T00:00:00Z"


async def test_short_inventory_pages_restart_and_no_implicit_history(chat_service):
    service = chat_service
    offsets = []
    async def call(method, params, **kwargs):
        if method == "im.recent.get":
            return []
        if method == "im.recent.list":
            offsets.append(params["OFFSET"])
            return {"items": [{"id": 101, "chat_id": 101, "type": "chat"}], "hasMore": params["OFFSET"] == 0}
        return {}  # No content calls are permitted while collection is disabled.
    service.client.call = call
    await service.chat_archive.step()
    service.chat_archive.request_discovery()  # UI refresh must not reset page1.
    await service.chat_archive.step()
    store = service.chat_archive.store()
    assert offsets == [0, 200] and store.summary()["count"] == 1 and not store.query()["total"]
    assert not service.db.rows("SELECT * FROM ca_work WHERE kind IN ('new','history')")


async def test_upgrade_keeps_manual_history_cancels_old_automatic(chat_service):
    service = chat_service
    store = seed(service)
    store.upsert_chat(102, "chat102", participants_at=time.time())
    store.enqueue(101, "history", {"automatic": True})
    store.enqueue(102, "history", {"automatic": False}, 2)
    async def call(method, params, **kwargs):
        if method == "im.recent.get":
            return []
        return {"items": [], "hasMore": False} if method == "im.recent.list" else {"messages": [message(1, params["CHAT_ID"])]}
    service.client.call = call
    for _ in range(4):
        await service.chat_archive.step()
    assert not store.query(chat=101)["total"] and store.query(chat=102)["total"] == 1
    assert store.state("collection_policy") == 5


async def test_manual_history_download_types_and_explicit_size_limit(chat_service):
    service = chat_service
    store = seed(service)
    service.settings.chat_download_documents = True
    service.settings.chat_max_file_mb = 1
    service.settings.chat_excluded_ids = [101]  # Manual choices are independent.
    async def call(method, params, **kwargs):
        if method == "im.recent.get":
            return []
        if method == "im.recent.list":
            return {"items": [], "hasMore": False}
        return {"messages": [message(1, fileIds=[77, 78])], "files": [
            {"id": 77, "name": "small.txt", "size": 9}, {"id": 78, "name": "large.pdf", "size": 2 * 1024**2}]}
    service.client.call = call
    service.chat_archive.request_history([101])
    await service.chat_archive.step()
    assert store.message(101, 1) and store.file(101, 77)["state"] == "queued"
    assert store.file(101, 77)["download_origin"] == "manual_collection"
    assert store.file(101, 78)["state"] == "size_limited" and "Текст сохранён" in store.file(101, 78)["error"]
    service.chat_archive.queue_file(store, 101, 78)
    assert store.file(101, 78)["state"] == "queued" and not store.file(101, 78)["automatic"]


async def test_audit_only_existing_messages_and_multiple_people_filters(chat_service):
    service = chat_service
    store = seed(service)
    store.update_chat(101, participants=[{"id": 42, "name": "Участник"}, {"id": 7, "name": "Другой"}])
    store.save_page(101, {"messages": [message(2, author_id=7)]})
    store.enqueue(101, "audit", {"cursor": 0})
    async def call(method, params, **kwargs):
        return {"items": [], "hasMore": False} if method == "im.recent.list" else {"messages": [message(2, author_id=7, text="Правка"), message(1)]}
    service.client.call = call
    for _ in range(3):
        await service.chat_archive.step()
    assert store.query()["total"] == 1 and len(store.versions(101, 2)) == 1
    assert store.query(author="7,42", participant="7,42")["total"] == 1
    assert store.query(author="8,9", participant="7,42")["total"] == 0


def test_chat_settings_disjoint_selection_and_default_poll(chat_service):
    from meeting_archive.chat_sync import validate_chat_settings
    from meeting_archive.settings import Settings
    assert Settings().chat_poll_seconds == 300
    with pytest.raises(ValueError, match="одновременно"):
        validate_chat_settings({"chat_selected_ids": [101], "chat_excluded_ids": [101]}, chat_service.settings, chat_service.home)


async def test_event_new_chat_history_and_disabled_collection_no_content(chat_service):
    service = chat_service
    store = seed(service)
    store.set_state("inventory_ready", True)
    event = {"eventId": 1, "type": "ONIMV2MESSAGEADD", "data": {"chat": {"id": 102}, "message": message(1, 102, text="Synthetic unsaved")}}
    async def call(method, params, **kwargs):
        return {"events": [event], "nextOffset": event["eventId"] + 1} if method == "im.v2.Event.get" else True
    service.client.call = call
    await service.chat_archive.events(store)
    assert not store.query(chat=102)["total"] and "Synthetic unsaved" not in (store.folder / "events.jsonl").read_text("utf-8")
    service.settings.chat_auto_save = True
    event.update(eventId=2, data={"chat": {"id": 103}, "message": message(1, 103, text="Old unseen event")})
    await service.chat_archive.events(store)
    assert not store.query(chat=103)["total"]
    assert "Old unseen event" not in (store.folder / "events.jsonl").read_text("utf-8")
    event.update(eventId=3, data={"chat": {"id": 103}, "message": message(2, 103, date="2099-01-01T00:00:00Z")})
    await service.chat_archive.events(store)
    assert not store.chat(103).get("auto_history") and store.query(chat=103)["total"] == 1
    assert not service.db.rows("SELECT kind FROM ca_work WHERE account=? AND chat=? AND kind='history'", (store.account, 103))


async def test_v2_policy_upgrade_preserves_manual_jobs_and_files(chat_service):
    service = chat_service
    store = seed(service)
    service.settings.chat_download_documents = True
    store.set_state("collection_policy", 2)
    cutoff = "2026-10-01T00:00:00Z"
    store.set_state("auto_since", cutoff)
    store.update_chat(101, auto_history=True, coverage="backfilling")
    store.upsert_chat(102, "chat102", participants_at=time.time())
    store.enqueue(101, "history", {"automatic": True, "cursor": 200}, 10)
    store.enqueue(101, "new", {"automatic": True, "cursor": 200, "date_from": "", "pages": 2, "messages": 400}, 0)
    store.enqueue(102, "history", {"automatic": False, "cursor": 61}, 2)
    store.upsert_chat(104, "chat104", participants_at=time.time())
    store.enqueue(104, "new", {"automatic": True, "cursor": 55, "date_from": cutoff}, 0)
    store.save_page(101, {"messages": [message(1, fileIds=[77, 79, 80, 81, 82]),
                                      message(2, date="2026-10-02T00:00:00Z", fileIds=[78])],
                          "files": [{"id": i, "name": f"demo-{i}.txt"} for i in range(77, 83)]})
    for id in (77, 78, 82):
        service.chat_archive.queue_file(store, 101, id, automatic=True)
    service.chat_archive.queue_file(store, 101, 79, automatic=True, manual_collection=True, collection_kind="period:2026-09-01:2026-09-30")
    service.chat_archive.queue_file(store, 101, 80, automatic=True, history_opt_in=True)
    service.chat_archive.queue_file(store, 101, 81)
    store.file_update(101, 82, state="saved")
    note = store.chat_folder(101) / "notes/synthetic.md"
    note.write_text("Synthetic note must remain", "utf-8")
    store.flush(101)
    other = ChatStore(service.db, service.settings.chat_archive_root, service.settings.portal, 43)
    other.upsert_chat(201, "chat201", auto_history=True)
    other.enqueue(201, "history", {"automatic": True})

    await service.chat_archive.upgrade_collection_policy(store)
    assert store.state("collection_policy") == 5 and store.state("auto_since") == cutoff
    work = service.db.rows("SELECT chat,kind,data FROM ca_work WHERE account=?", (store.account,))
    assert not any(r["chat"] == 101 and r["kind"] == "history" for r in work)
    manual = next(r for r in work if r["chat"] == 102)
    assert json.loads(manual["data"])["cursor"] == 61 and store.chat(102)["manual_history_requested"]
    new = next(r for r in work if r["chat"] == 101)
    assert json.loads(new["data"])["date_from"] == cutoff and json.loads(new["data"])["cursor"] == 0
    assert json.loads(new["data"])["messages"] == 0 and json.loads(new["data"])["pages"] == 0
    assert json.loads(next(r for r in work if r["chat"] == 104)["data"])["cursor"] == 55
    assert store.file(101, 77)["state"] == "not_saved"
    service.chat_archive.queue_file(store, 101, 77, automatic=True)
    assert store.file(101, 77)["state"] == "not_saved"  # A later audit cannot requeue the accidental old file.
    assert all(store.file(101, id)["state"] == "queued" for id in (78, 79, 80, 81))
    assert store.file(101, 82)["state"] == "saved" and store.query(chat=101)["total"] == 2
    assert note.read_text("utf-8") == "Synthetic note must remain"
    manifest = json.loads((store.chat_folder(101) / "chat.json").read_text("utf-8"))
    assert not any(w["kind"] == "history" for w in manifest["work"])
    assert other.chat(201)["auto_history"] and other.db.rows("SELECT kind FROM ca_work WHERE account=?", (other.account,))


@pytest.mark.parametrize("policy", [2, 3])
async def test_legacy_completeness_does_not_invent_manual_consent(chat_service, policy):
    store = seed(chat_service)
    store.set_state("collection_policy", policy)
    store.set_state("auto_since", "2026-10-01T00:00:00Z")
    store.update_chat(101, history_complete=True)
    store.upsert_chat(102, "chat102", auto_history=False, history_complete=True)
    store.upsert_chat(103, "chat103", auto_history=True, history_complete=True)
    store.activity("history", 103, "done", automatic=False, priority=2)
    if policy == 3:
        # Earlier migration guesses carry no reliable origin; do not trust them.
        store.update_chat(101, auto_history=False, manual_history_requested=True)
        store.update_chat(102, manual_history_requested=True)
    await chat_service.chat_archive.upgrade_collection_policy(store)
    assert not store.chat(101).get("manual_history_requested")
    assert not store.chat(101).get("manual_history_origin")
    assert bool(store.chat(102).get("manual_history_requested")) == (policy == 2)
    assert store.chat(103)["manual_history_requested"] and store.chat(103)["manual_history_origin"] == "manual_job_v2"
    for id in (101, 102, 103):
        store.save_page(id, {"messages": [message(1, id)]})
        chat_service.chat_archive.schedule_chat(store, store.chat(id), True)
        data = json.loads(store.db.rows("SELECT data FROM ca_work WHERE account=? AND chat=? AND kind='new'", (store.account, id))[0]["data"])
        assert bool(data["date_from"]) == (id == 101 or id == 102 and policy == 3)


@pytest.mark.parametrize("policy", [2, 3, 4])
async def test_first_release_catalogue_jobs_are_not_manual_history(chat_service, policy):
    service = chat_service
    store = seed(service)
    store.set_state("collection_policy", policy)
    store.set_state("auto_since", "2026-10-01T00:00:00Z")
    store.set_state("last_discovery", time.time())
    service.settings.chat_download_documents = True
    # v0.2.29 queued these after a catalogue refresh, without selecting history.
    store.enqueue(101, "history", {"automatic": False, "cursor": 51}, 10)
    store.enqueue(101, "new", {"automatic": False, "date_from": ""}, 0)
    store.save_page(101, {"messages": [message(1, fileIds=[77])], "files": [{"id": 77, "name": "legacy.txt"}]})
    service.chat_archive.queue_file(store, 101, 77, automatic=True, manual_collection=True)
    if policy >= 3:
        store.update_chat(101, auto_history=False, manual_history_requested=True,
                          manual_history_origin="manual_job" if policy == 4 else "")
    store.upsert_chat(102, "chat102", participants_at=time.time())
    store.enqueue(102, "history", {"automatic": False, "cursor": 61}, 2)
    store.upsert_chat(103, "chat103", participants_at=time.time())
    store.enqueue(103, "history", {"automatic": False, "cursor": 55}, 10)
    service.chat_archive.request_history([103])  # Explicit selection resolves an old row collision.

    await service.chat_archive.upgrade_collection_policy(store)
    assert not store.chat(101).get("manual_history_requested")
    assert not store.db.rows("SELECT kind FROM ca_work WHERE account=? AND chat=101 AND kind='history'", (store.account,))
    new = json.loads(store.db.rows("SELECT data FROM ca_work WHERE account=? AND chat=101 AND kind='new'", (store.account,))[0]["data"])
    assert new["automatic"] is True and new["date_from"] == "2026-10-01T00:00:00Z"
    assert store.file(101, 77)["state"] == "not_saved" and store.query(chat=101)["total"] == 1
    assert store.chat(102)["manual_history_origin"] == "manual_job_v2"
    assert store.chat(103)["manual_history_origin"] == "request"
    service.chat_archive.recovered.add(store.account)
    seen = []
    async def call(method, params, **kwargs):
        assert method == "im.dialog.messages.search" and params["CHAT_ID"] in {102, 103}
        seen.append(params["CHAT_ID"])
        return {"messages": [message(1, params["CHAT_ID"])]}
    service.client.call = call
    for _ in range(2):
        await service.chat_archive.step()
    assert seen == [102, 103]  # Automation off cannot run the old catalogue check.


@pytest.mark.parametrize("automatic", [True, False])
async def test_stale_new_job_fallback_does_not_read_all_older_pages(chat_service, automatic):
    store = seed(chat_service)
    store.set_state("auto_since", "2026-10-01T00:00:00Z")
    store.update_chat(101, auto_history=True)  # Legacy flag grants no permission.
    store.enqueue(101, "new", {"automatic": automatic, "date_from": "", "cursor": 0})
    calls = []

    async def call(method, params, **kwargs):
        calls.append(method)
        if method == "im.dialog.messages.search":
            assert params["DATE_FROM"] == "2026-10-01T00:00:00Z"
            raise BitrixError("Synthetic fallback", code="METHOD_NOT_FOUND")
        assert method == "im.dialog.messages.get"
        items = [message(i, date="2026-10-02T00:00:00Z" if i>40 else "2020-01-01T00:00:00Z") for i in range(50, 0, -1)]
        items[0].update(fileIds=[98], reply={"id": 999, "text": "Needed old source"})
        items[-1].update(fileIds=[99], reply={"id": 998})
        return {"chat_id": 101, "messages": items,
                "additionalMessages": [message(999, text="Needed old source", fileIds=[97]), message(998, text="Unrelated old source")],
                "files": [{"id": i, "name": f"demo-{i}.txt"} for i in (97, 98, 99)]}

    chat_service.client.call = call
    await chat_service.chat_archive.process_work(store, store.db.rows("SELECT * FROM ca_work WHERE account=?", (store.account,))[0])
    assert calls == ["im.dialog.messages.search", "im.dialog.messages.get"]
    assert store.query(chat=101)["total"] == 10
    assert {f["id"] for f in store.files(101)} == {97, 98}
    context = store.db.rows("SELECT id FROM ca_context WHERE account=? AND chat=?", (store.account, 101))
    assert {m["id"] for m in context} == {999}
    assert not store.db.rows("SELECT kind FROM ca_work WHERE account=?", (store.account,))


def test_manual_full_history_is_explicit_and_survives_portable_recovery(chat_service):
    store = seed(chat_service)
    store.upsert_chat(102, "chat102", participants_at=time.time())
    store.set_state("auto_since", "2026-10-01T00:00:00Z")
    chat_service.chat_archive.request_history([101], "2026-09-01", "2026-09-30")
    assert not store.chat(101).get("manual_history_requested")
    chat_service.chat_archive.request_history([102])
    store.save_page(102, {"messages": [message(1, 102)]})
    store.update_chat(102, history_complete=True)
    store.flush(102)
    for table in ("ca_chats", "ca_work", "ca_messages"):
        store.db.execute(f"DELETE FROM {table} WHERE account=? AND {'id' if table=='ca_chats' else 'chat'}=102", (store.account,))
    store.recover()
    assert store.chat(102)["manual_history_requested"]
    chat_service.chat_archive.schedule_chat(store, store.chat(102), True)
    queued = store.db.rows("SELECT data FROM ca_work WHERE account=? AND chat=102 AND kind='new'", (store.account,))[0]
    assert not json.loads(queued["data"])["date_from"]


async def test_automatic_audit_cannot_expand_into_unrequested_old_period(chat_service):
    store = seed(chat_service)
    cutoff = "2026-10-01T00:00:00Z"
    store.set_state("auto_since", cutoff)
    store.save_page(101, {"messages": [message(1), message(2, date="2026-10-02T00:00:00Z")]})
    store.enqueue(101, "audit", {"automatic": True, "date_from": "2020-01-01T00:00:00Z"})

    async def call(method, params, **kwargs):
        assert method == "im.dialog.messages.search" and params["DATE_FROM"] == cutoff
        return {"messages": [message(1, text="Old edit must not be fetched into this audit"),
                              message(2, date="2026-10-02T00:00:00Z", text="New saved edit")]}

    chat_service.client.call = call
    await chat_service.chat_archive.process_work(store, store.db.rows("SELECT * FROM ca_work WHERE account=?", (store.account,))[0])
    assert store.message(101, 1)["text"] == "Сообщение 1" and not store.versions(101, 1)
    assert store.message(101, 2)["text"] == "New saved edit" and len(store.versions(101, 2)) == 1


async def test_metadata_limit_retains_work_and_manual_request_takes_priority(chat_service):
    service = chat_service
    store = seed(service)
    store.set_state("collection_policy", 5)
    store.set_state("last_discovery", time.time())
    store.enqueue(101, "metadata", {}, 4)
    store.upsert_chat(102, "chat102", participants_at=time.time())
    service.chat_archive.request_history([102])
    reset = time.time() + 180
    async def call(method, params, **kwargs):
        if method == "im.dialog.messages.search":
            return {"messages": [message(1, 102)]}
        raise BitrixError("Synthetic limit", code="OPERATION_TIME_LIMIT", retryable=True, retry_at=reset)
    service.client.call = call
    await service.chat_archive.step()
    assert store.query(chat=102)["total"] == 1
    await service.chat_archive.step()
    row = service.db.rows("SELECT next_at FROM ca_work WHERE account=? AND chat=? AND kind='metadata'", (store.account, 101))[0]
    assert row["next_at"] == reset and store.chat(101)["participants_at"] < reset


async def test_attachment_history_opt_in_downloads_with_text_automation_off(chat_service):
    service = chat_service
    store = seed(service)
    store.save_page(101, {"messages": [message(1, fileIds=[77])], "files": [{"id": 77, "name": "old.txt", "size": 14}]})
    old = replace(service.settings)
    service.settings.chat_download_documents = service.settings.chat_download_history = True
    await service.chat_archive.settings_changed(old)
    assert store.file(101, 77)["state"] == "queued" and store.file(101, 77)["download_origin"] == "history_opt_in"
    async def call(method, params, **kwargs):
        return {"downloadUrl": "https://synthetic.bitrix24.ru/old.txt"}
    service.client.call = call
    await service.chat_archive.file_step()
    assert store.file(101, 77)["state"] == "saved"


def test_legacy_overlap_migrates_with_exclusion_precedence(chat_service):
    from meeting_archive.settings import Settings
    chat_service.settings.chat_selected_ids = [101, 102, 101]
    chat_service.settings.chat_excluded_ids = [101, 101]
    chat_service.settings.save(chat_service.home)
    migrated = Settings.load(chat_service.home)
    assert migrated.chat_selected_ids == [102] and migrated.chat_excluded_ids == [101]
