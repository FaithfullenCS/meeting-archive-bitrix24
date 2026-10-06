"""Local chat endpoints inherit the application's session and CSRF middleware."""
from __future__ import annotations

import asyncio
import json
import zipfile

from fastapi import Request
from fastapi.responses import FileResponse

from .chat_model import positive, text_html


def register_chat_api(app, service):
    engine = service.chat_archive

    @app.get("/api/chat-archive")
    async def catalogue(q: str = "", type: str = "", participant: str = "", coverage: str = ""):
        store = engine.store()
        if service.connected() and store.account not in engine.recovered:
            async with engine.lock:
                await asyncio.to_thread(store.recover)
                engine.recovered.add(store.account)
        if service.connected() and not store.state("last_discovery"):
            engine.request_discovery(False)
        participants = {positive(i) for i in participant.split(",")} - {0}
        items = store.chats()
        items = [c for c in items if (not q or q.casefold() in c["title"].casefold() or q == str(c["id"])) and
                 (not type or c["type"] == type) and (not coverage or c["coverage"] == coverage) and
                 (not participants or participants.issubset({p["id"] for p in c.get("participants", [])}))]
        previews = {r["id"]: r["preview"] for r in service.db.rows("SELECT c.id,(SELECT text FROM ca_messages m WHERE m.account=c.account AND m.chat=c.id ORDER BY date DESC,id DESC LIMIT 1) AS preview FROM ca_chats c WHERE c.account=?", (store.account,))}
        for item in items:
            item["preview"] = (previews.get(item["id"]) or "Сообщения ещё не сохранены")[:160]
        return {"items": sorted(items, key=lambda c: c["count"]["last"] or "", reverse=True), "account": store.account,
                **store.summary(), "total": len(items), "poll_seconds": service.settings.chat_poll_seconds,
                "connected": service.connected(), "error": engine.error, "active": engine.active,
                "auto_save": service.settings.chat_auto_save, "events_error": store.state("event_error", ""),
                "events_status": ("Отключено" if not service.settings.chat_events else "Ожидается подключение аккаунта" if not service.connected() else "Быстрое получение правок и удалений разрешено для этого аккаунта" if service.db.get_state(f"chat_events:{store.portal}:{store.user_id}:consent") or service.db.get_state("chat_events:pending_consent") else "Для этого аккаунта режим ещё не разрешён. Выключите и включите сохранение правок, затем сохраните настройки."),
                "discovery": "Последние диалоги и известные приложению чаты. Скрытые ветки могут отсутствовать."}

    @app.get("/api/chat-archive/messages")
    async def messages(chat: int = 0, q: str = "", date_from: str = "", date_to: str = "", author: str = "",
                       direction: str = "", kind: str = "", attachment: str = "", file_state: str = "",
                       system: str = "", type: str = "", participant: str = "", coverage: str = "", around: int = 0, offset: int = 0, limit: int = 50):
        store = engine.store()
        if chat:
            store.chat(chat)
        if around and chat:
            offset = max(0, service.db.rows("SELECT count(*) AS n FROM ca_messages WHERE account=? AND chat=? AND id>?", (store.account, chat, around))[0]["n"] - 25)
        return await asyncio.to_thread(store.query, chat=chat, q=q, date_from=date_from, date_to=date_to,
                                       author=author, direction=direction, kind=kind, attachment=attachment,
                                       file_state=file_state, system=system, type=type, participant=participant,
                                       coverage=coverage, offset=offset, limit=limit)

    @app.get("/api/chat-archive/filters")
    async def filters():
        store = engine.store()
        people = {}
        for row in service.db.rows("SELECT author,data FROM ca_messages WHERE account=? GROUP BY author HAVING id=max(id)", (store.account,)):
            data = json.loads(row["data"])
            people[row["author"]] = data["author"] or f"Автор ID {row['author']}"
        for chat in store.chats():
            for person in chat.get("participants", []):
                people.setdefault(person["id"], person["name"] or f"Участник ID {person['id']}")
        return {"authors": [{"id": id, "name": name} for id, name in people.items()],
                "types": sorted({c["type"] for c in store.chats()}),
                "coverage": sorted({c["coverage"] for c in store.chats()})}

    @app.get("/api/chat-archive/chats/{chat}/messages/{id}")
    async def message(chat: int, id: int):
        store = engine.store()
        item = store.message(chat, id)
        if not item:
            raise ValueError("Оригинал недоступен в этом архиве")
        item["html"] = text_html(item["text"])
        return {"message": item}

    @app.get("/api/chat-archive/chats/{chat}/messages/{id}/versions")
    async def versions(chat: int, id: int):
        return {"items": [{**item, "html": text_html(item["text"])} for item in engine.store().versions(chat, id)]}

    @app.get("/api/chat-archive/chats/{chat}/context/{source_chat}/{id}")
    async def context(chat: int, source_chat: int, id: int):
        return {"items": engine.store().context(chat, source_chat, id)}

    @app.post("/api/chat-archive/sync")
    async def sync():
        async with engine.lock:
            engine.request_discovery(False)
        return {"queued": True}

    @app.post("/api/chat-archive/backfill")
    async def backfill(request: Request):
        data = await request.json()
        ids = data.get("ids")
        if ids is not None and (not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids)):
            raise ValueError("Выберите ID чатов")
        async with engine.lock:
            count = engine.request_history(ids, data.get("date_from", ""), data.get("date_to", ""))
        return {"queued": count}

    @app.post("/api/chat-archive/chats")
    async def add_chat(request: Request):
        data = await request.json()
        return {"chat": await engine.add(data.get("dialog", ""))}

    @app.post("/api/chat-archive/chats/{chat}/files/{id}/download")
    async def download(chat: int, id: int):
        async with engine.lock:
            store = engine.store()
            engine.queue_file(store, chat, id)
            await asyncio.to_thread(store.flush, chat)
        return {"queued": True}

    @app.get("/api/chat-archive/chats/{chat}/files/{id}")
    async def file(chat: int, id: int):
        store = engine.store()
        item = store.file(chat, id)
        folder = store.chat_folder(chat).resolve()
        path = (folder / item.get("path", "")).resolve()
        if not path.is_relative_to(folder / "attachments") or not path.is_file():
            raise ValueError("Вложение ещё не скачано или файл перемещён")
        # Download arbitrary source files, never execute HTML/SVG on the local origin.
        return FileResponse(path, media_type="application/octet-stream", filename=item["name"], content_disposition_type="attachment")

    @app.post("/api/chat-archive/export")
    async def export(request: Request):
        data = await request.json()
        store = engine.store()
        all_chats = not data.get("ids")
        ids = data.get("ids") or [c["id"] for c in store.chats()]
        ids = [positive(id) for id in ids]
        for id in ids:
            store.chat(id)
        async with engine.lock:
            for id in ids:
                await asyncio.to_thread(store.flush, id)
            target = service.home / "exports" / (store.account + ".zip")
            target.parent.mkdir(parents=True, exist_ok=True)
            def write_zip():
                temporary = target.with_suffix(".tmp")
                with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                    for id in ids:
                        folder = store.chat_folder(id)
                        for path in folder.rglob("*"):
                            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(folder.resolve()) and not any(part.startswith(".") for part in path.relative_to(folder).parts):
                                archive.write(path, path.relative_to(store.root).as_posix())
                    for path in (store.root / "archive.json", store.folder / "account.json"):
                        if path.is_file():
                            archive.write(path, path.relative_to(store.root).as_posix())
                    if all_chats:
                        notebook = store.folder / "_notebook"
                        for path in notebook.rglob("*"):
                            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(notebook.resolve()) and not any(part.startswith(".") for part in path.relative_to(notebook).parts):
                                archive.write(path, path.relative_to(store.root).as_posix())
                temporary.replace(target)
            await asyncio.to_thread(write_zip)
        return {"url": "/api/chat-archive/export/download"}

    @app.get("/api/chat-archive/export/download")
    async def export_file():
        path = service.home / "exports" / (engine.store().account + ".zip")
        if not path.is_file():
            raise ValueError("Сначала создайте экспорт")
        return FileResponse(path, filename="Архив чатов.zip", media_type="application/zip")
