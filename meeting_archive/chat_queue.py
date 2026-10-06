"""One account-scoped view of durable chat work, files and recent outcomes."""

import json
import time

from .chat_model import fingerprint
from .scheduling import window_status
from .chat_sync import iso_day


LABELS = {
    "new": "Новые сообщения",
    "history": "История чата",
    "metadata": "Участники и сведения о чате",
    "recent_check": "Проверка сохранённых сообщений",
    "audit": "Сверка истории",
    "file": "Вложение",
    "discovery": "Каталог чатов",
}


def queue_view(engine, offset=0, limit=100, *, all_items=False):
    store = engine.store()
    chats = {
        r["id"]: json.loads(r["data"])
        for r in store.db.rows("SELECT id,data FROM ca_chats WHERE account=?", (store.account,))
    }
    entries = {}
    now = time.time()

    def item(kind, chat, state, **values):
        id = fingerprint([store.account, kind, chat, values.get("file_id", 0)])[:24]
        return {
            "id": id,
            "kind": kind,
            "chat": chat,
            "state": state,
            "title": chats.get(chat, {}).get("title", "Каталог чатов"),
            "label": LABELS.get(kind, "Выбранный период"),
            **values,
        }

    for row in store.db.rows("SELECT data FROM ca_activity WHERE account=? ORDER BY touched DESC", (store.account,)):
        data = json.loads(row["data"])
        entries[data["id"]] = item(
            data["kind"],
            data["chat"],
            data["state"],
            **{k: v for k, v in data.items() if k not in {"id", "kind", "chat", "state"}},
        )
    for row in store.db.rows("SELECT * FROM ca_work WHERE account=? ORDER BY priority,touched,chat", (store.account,)):
        data = json.loads(row["data"])
        running = getattr(engine, "current_work", None) == (store.account, row["chat"], row["kind"])
        state = "running" if running else "failed" if data.get("error") else "queued"
        value = item(
            row["kind"],
            row["chat"],
            state,
            touched=row["touched"],
            next_at=row["next_at"],
            automatic=bool(data.get("automatic")),
            error=data.get("error", ""),
            pages=data.get("pages", 0),
            messages=data.get("messages", 0),
        )
        entries[value["id"]] = value
    for row in store.db.rows(
        "SELECT * FROM ca_files WHERE account=? AND state IN ('queued','running','error','unavailable','size_limited')",
        (store.account,),
    ):
        data = json.loads(row["data"])
        state = (
            "running"
            if row["state"] == "running"
            else "failed"
            if row["state"] in {"error", "unavailable", "size_limited"} or row["error"]
            else "queued"
        )
        window = window_status(engine.service.settings.chat_attachment_schedule)
        waiting = bool(row["automatic"] and (engine.service.settings.paused or not window["allowed"]))
        value = item(
            "file",
            row["chat"],
            state,
            file_id=row["id"],
            filename=data["name"],
            error=row["error"],
            touched=data.get("queued_at", 0),
            next_at=row["next_at"],
            automatic=bool(row["automatic"]),
            schedule_wait=waiting,
            message="Автоматизация на паузе"
            if engine.service.settings.paused and row["automatic"]
            else window["label"]
            if waiting
            else "",
            downloaded_bytes=data.get("downloaded_bytes", 0),
            total_bytes=data.get("size", 0),
        )
        entries[value["id"]] = value
    if store.state("discover_requested"):
        value = item(
            "discovery",
            0,
            "running"
            if getattr(engine, "discovering", False)
            else "failed"
            if store.state("discovery_retry_at", 0) > now
            else "queued",
            touched=now,
            error=engine.error if store.state("discovery_retry_at", 0) > now else "",
            next_at=store.state("discovery_retry_at", 0),
            message="Обновляем доступные чаты",
        )
        entries[value["id"]] = value
    values = sorted(
        entries.values(),
        key=lambda e: (e["state"] == "running", e["state"] in {"queued", "failed"}, e.get("touched", 0)),
        reverse=True,
    )
    offset = max(0, offset)
    limit = max(1, min(200, limit))
    return {
        "items": values if all_items else values[offset : offset + limit],
        "account": store.account,
        "total": len(values),
        "offset": offset,
        "pending": sum(e["state"] in {"queued", "running", "failed"} for e in values),
    }


def queue_action(engine, id, action):
    store = engine.store()
    # Lookup is scoped to this archive/account. Never accept arbitrary SQL keys.
    selected = next((e for e in queue_view(engine, all_items=True)["items"] if e["id"] == id), None)
    if not selected:
        raise ValueError("Задание недоступно для текущего аккаунта")
    kind, chat = selected["kind"], selected["chat"]
    if kind == "discovery":
        if action == "retry":
            store.set_state("discovery_retry_at", 0)
            engine.request_discovery(False)
        else:
            store.set_state("discover_requested", None)
        return
    if kind == "file":
        if action == "retry":
            engine.queue_file(store, chat, selected["file_id"])
        else:
            store.file_update(chat, selected["file_id"], state="not_saved", automatic=0)
    elif action == "retry":
        rows = store.db.rows(
            "SELECT data FROM ca_work WHERE account=? AND chat=? AND kind=?", (store.account, chat, kind)
        )
        payload = json.loads(rows[0]["data"]) if rows else {}
        payload.update(cursor=0, automatic=False)
        payload.pop("error", None)
        if kind == "new":
            payload.setdefault("date_from", store.state("auto_since", ""))
            payload["stop_id"] = (
                store.db.rows("SELECT max(id) AS n FROM ca_messages WHERE account=? AND chat=?", (store.account, chat))[
                    0
                ]["n"]
                or 0
            )
        if kind.startswith("period:"):
            _, start, end = kind.split(":", 2)
            payload.update(date_from=iso_day(start), date_to=iso_day(end, True))
        store.db.execute("DELETE FROM ca_work WHERE account=? AND chat=? AND kind=?", (store.account, chat, kind))
        store.enqueue(chat, kind, payload, 2)
        if kind == "history":
            store.update_chat(chat, history_paused=False)
    else:
        store.db.execute("DELETE FROM ca_work WHERE account=? AND chat=? AND kind=?", (store.account, chat, kind))
        if kind == "history":
            store.update_chat(chat, history_paused=True)
        store.activity(kind, chat, "cancelled")
