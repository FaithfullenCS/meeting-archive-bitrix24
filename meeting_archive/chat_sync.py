"""Bounded, fair personal chat synchronization and a separate single-file queue."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone, time as day_time
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from .bitrix import BitrixError
from .chat_model import canonical, chat_classification, normalize, positive, safe_metadata, safe_name, sequence, stamp
from .chat_storage import ChatStore
from .scheduling import window_status

ACCESS_CODES = {"ACCESS_ERROR", "ACCESS_DENIED", "CHAT_NOT_FOUND", "DIALOG_ID_INVALID"}
UNSUPPORTED_CODES = {"METHOD_NOT_FOUND", "ERROR_METHOD_NOT_FOUND", "NOT_IMPLEMENTED", "UNKNOWN_METHOD"}
COLLECTION_POLICY = 5
MANUAL_HISTORY_ORIGINS = {"request", "manual_job_v2", "baseline_v2"}


async def durable_io(function, *args):
    """Cancellation must drain disk work before Service.stop closes SQLite.

    Cancelling to_thread's await does not stop its native thread. Keep the chat
    lock and let an already started local write/recovery finish on shutdown.
    Network waits remain immediately cancellable.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def iso_day(value, end=False):
    if not value:
        return ""
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError) as exc:
        raise ValueError("Укажите дату в формате ГГГГ-ММ-ДД") from exc
    return datetime.combine(day, day_time.max if end else day_time.min).astimezone().isoformat()


def instant(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def manual_history_complete(chat):
    return bool(chat.get("manual_history_requested") is True and chat.get("history_complete") is True and
                chat.get("manual_history_origin") in MANUAL_HISTORY_ORIGINS)


def auto_since_for(store, chat):
    return max((store.state("auto_since", stamp()), chat.get("message_delete_after") or ""), key=lambda v: instant(v) or 0)


def related_materials(page, chat):
    """Retain only files and source excerpts related to the selected messages."""
    messages = [normalize(raw, chat) for raw in page["messages"]]
    wanted = {(link["chat_id"], link["message_id"]) for m in messages for link in m["relations"]}
    files = {id for m in messages for id in m["file_ids"]}
    pending = [(raw, normalize(raw, positive(raw.get("chatId") or raw.get("chat_id")) or chat))
               for raw in sequence(page.get("additionalMessages")) if isinstance(raw, dict)]
    kept = set()
    while True:
        found = False
        for _, m in pending:
            key = (m["chat_id"], m["id"])
            if key in wanted and key not in kept:
                kept.add(key)
                wanted.update((link["chat_id"], link["message_id"]) for link in m["relations"])
                files.update(m["file_ids"])
                found = True
        if not found:
            break
    return {**page, "additionalMessages": [raw for raw, m in pending if (m["chat_id"], m["id"]) in kept],
            "files": [raw for raw in sequence(page.get("files")) if isinstance(raw, dict) and positive(raw.get("id") or raw.get("fileId")) in files]}


def validate_chat_settings(values, settings, home):
    if values.get("chat_scope", settings.chat_scope) not in {"all", "selected"}:
        raise ValueError("Выберите все доступные чаты или выбранные чаты")
    for key in ("chat_selected_ids", "chat_excluded_ids"):
        if key in values and (len(values[key]) > 10000 or any(type(i) is not int or i <= 0 for i in values[key])):
            raise ValueError("Список чатов должен содержать положительные ID")
    selected = values.get("chat_selected_ids", settings.chat_selected_ids)
    excluded = values.get("chat_excluded_ids", settings.chat_excluded_ids)
    if set(selected) & set(excluded):
        raise ValueError("Чат не может быть одновременно выбранным и исключённым")
    for key in ("chat_selected_ids", "chat_excluded_ids"):
        if key in values:
            values[key] = sorted(set(values[key]))
    if values.get("chat_poll_seconds", settings.chat_poll_seconds) not in {60, 300, 900}:
        raise ValueError("Интервал проверки: 1, 5 или 15 минут")
    iso_day(values.get("chat_history_since", settings.chat_history_since))
    if "chat_archive_root" in values:
        path = Path(values["chat_archive_root"]).expanduser()
        root, private = path.resolve(), home.resolve()
        meetings = Path(values.get("archive_root", settings.archive_root)).resolve()
        if not path.is_absolute() or root.is_relative_to(private) or private.is_relative_to(root) or root.is_relative_to(meetings) or meetings.is_relative_to(root):
            raise ValueError("Папка чатов должна находиться отдельно от служебной папки и архива совещаний")
        values["chat_archive_root"] = str(root)


class ChatArchive:
    def __init__(self, service):
        self.service = service
        self.lock = asyncio.Lock()
        self.file_lock = asyncio.Lock()
        self.error = ""
        self.active = ""
        self.recovered = set()
        ChatStore.initialize(service.db)

    def store(self):
        s = self.service.settings
        return ChatStore(self.service.db, s.chat_archive_root, s.portal, s.user_id)

    def selected(self, id):
        s = self.service.settings
        return id not in s.chat_excluded_ids and (s.chat_scope == "all" or id in s.chat_selected_ids)

    def has_current_messages(self, store, chat):
        return bool(self.service.db.rows("SELECT 1 FROM ca_messages WHERE account=? AND chat=? AND julianday(date)>=julianday(?) LIMIT 1",
                                        (store.account, chat, store.state("auto_since", stamp()))))

    @staticmethod
    def recent_metadata(item):
        message = item.get("message") if isinstance(item.get("message"), dict) else {}
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        values = {}
        values.update(chat_classification(item))
        moment = instant(message.get("date") or item.get("date_last_activity") or item.get("date_update"))
        if moment is not None:
            values["source_last_message_at"] = datetime.fromtimestamp(moment, timezone.utc).isoformat()
            values["source_last_message_id"] = positive(message.get("id"))
        if item.get("type") == "user" and type(user.get("active")) is bool:
            values["peer_active"] = user["active"]
        return values

    async def extra_recent(self, store):
        """Read a second documented recent list; never enumerate staff or save text."""
        if time.time() < store.state("extra_recent_at", 0) + 86400:
            return
        try:
            page = await self.service.client.call("im.recent.get", {"SKIP_OPENLINES": "N", "SKIP_CHAT": "N", "SKIP_DIALOG": "N"}, v3=False)
            if not isinstance(page, list):
                raise ValueError("Дополнительный список чатов вернул неизвестный формат")
            for item in page:
                if not isinstance(item, dict):
                    continue
                id = positive(item.get("chat_id") or (item.get("chat") or {}).get("id"))
                if not id:
                    continue
                existing = self.service.db.rows("SELECT id FROM ca_chats WHERE account=? AND id=?", (store.account, id))
                values = self.recent_metadata(item)
                # The paginated current list has authority if both lists expose it.
                current = store.chat(id) if existing else {}
                primary_fresh = current.get("source_recent_seen_at", 0) >= time.time() - self.service.settings.chat_poll_seconds
                if primary_fresh:
                    values.pop("source_last_message_at", None)
                    values.pop("source_last_message_id", None)
                if not existing:
                    values["auto_history"] = False
                store.upsert_chat(id, current["dialog"] if primary_fresh else str(item["id"]) if item.get("type") == "user" else f"chat{id}",
                                  title=str(current["title"] if primary_fresh else item.get("title") or current.get("title") or f"Чат {id}"),
                                  type=str(current["type"] if primary_fresh else item.get("type") or "chat"), **values)
            store.set_state("extra_recent_warning", "")
        except BitrixError as exc:
            if exc.retryable:
                raise
            store.set_state("extra_recent_warning", "Дополнительный список недоступен; показаны последние и ранее известные чаты")
        except ValueError:
            store.set_state("extra_recent_warning", "Дополнительный список не предоставлен; полнота обнаружения не подтверждена")
        store.set_state("extra_recent_at", time.time())

    def request_discovery(self, automatic=False):
        if not self.service.connected():
            raise ValueError("Сначала подключите Bitrix24 с правом im")
        store = self.store()
        # Keep the initial inventory a baseline across all pages and restarts.
        if store.state("discover_requested"):
            return
        store.set_state("discover_requested", {"automatic": automatic, "baseline": not store.state("inventory_ready", False)})
        store.set_state("discovery_offset", 0)

    def request_history(self, ids=None, date_from="", date_to=""):
        start, end = iso_day(date_from), iso_day(date_to, True)
        if start and end and start > end:
            raise ValueError("Начало периода должно быть не позже окончания")
        store = self.store()
        ids = [c["id"] for c in store.chats() if self.selected(c["id"])] if ids is None else ids
        for id in ids:
            store.chat(positive(id))
        for id in ids:
            store.update_chat(positive(id), history_paused=False,
                              **({"manual_history_requested": True, "manual_history_origin": "request", "message_delete_after": ""} if not start and not end else {}))
            kind = "period:" + date_from + ":" + date_to if start or end else "history"
            current = self.service.db.rows("SELECT data FROM ca_work WHERE account=? AND chat=? AND kind=?", (store.account, positive(id), kind))
            if current and json.loads(current[0]["data"]).get("automatic"):
                self.service.db.execute("DELETE FROM ca_work WHERE account=? AND chat=? AND kind=?", (store.account, positive(id), kind))
            store.enqueue(positive(id), kind, {"cursor": 0, "date_from": start, "date_to": end, "automatic": False}, 2)
        return len(ids)

    def schedule_chat(self, store, chat, automatic):
        if time.time() - chat.get("participants_at", 0) > 86400:
            store.enqueue(chat["id"], "metadata", {"automatic": False}, 4)
        if not automatic or not self.selected(chat["id"]):
            return
        newest = self.service.db.rows("SELECT max(id) AS id FROM ca_messages WHERE account=? AND chat=?", (store.account, chat["id"]))[0]["id"] or 0
        since = "" if manual_history_complete(chat) and newest else auto_since_for(store, chat)
        head = positive(chat.get("source_last_message_id"))
        unchanged = head and head == chat.get("sync_head_id")
        recently_checked = time.time() - chat.get("new_checked_at", 0) < max(900, self.service.settings.chat_poll_seconds * 5)
        if not unchanged or not recently_checked:
            urgent = head and head != chat.get("sync_head_id") and (instant(chat.get("source_last_message_at")) or 0) >= (instant(since) or 0)
            store.enqueue(chat["id"], "new", {"cursor": 0, "stop_id": newest, "source_head_id": head, "date_from": since, "automatic": True}, -1 if urgent else 0)
            if urgent:
                self.service.db.execute("UPDATE ca_work SET priority=-1 WHERE account=? AND chat=? AND kind='new'", (store.account, chat["id"]))

    async def add_dialog(self, store, dialog, hint=None):
        result = await self.service.client.call("im.dialog.get", {"DIALOG_ID": str(dialog)}, v3=False)
        id = positive(result.get("id")) if isinstance(result, dict) else 0
        if not id:
            raise ValueError("Источник не подтвердил ID доступного чата")
        hint = hint or {}
        title = str(result.get("name") or hint.get("title") or hint.get("name") or f"Чат {id}")
        members = sequence(result.get("users"))
        participants = [{"id": positive(u.get("id")), "name": str(u.get("name") or "")} if isinstance(u, dict)
                        else {"id": positive(u), "name": ""} for u in members]
        store.upsert_chat(id, dialog, title=title, type="user" if hint.get("type") == "user" or str(dialog).isdigit() else str(result.get("type") or "chat"),
                          participants=participants, parent_chat_id=positive(result.get("parent_chat_id")),
                          parent_message_id=positive(result.get("parent_message_id")))
        return store.chat(id)

    async def add(self, value):
        value = str(value).strip()
        if "://" in value:
            parsed = urlsplit(value)
            if parsed.hostname != self.service.settings.portal:
                raise ValueError("Ссылка должна относиться к подключённому порталу")
            query = parse_qs(parsed.query)
            value = (query.get("IM_DIALOG") or query.get("dialog") or [""])[0]
        if not re.fullmatch(r"(?:chat|sg)?[1-9]\d*", value):
            raise ValueError("Укажите chat123, ID собеседника или ссылку Bitrix24 с IM_DIALOG")
        async with self.service.auth_lock, self.lock:
            store = self.store()
            chat = await self.add_dialog(store, value)
            self.schedule_chat(store, chat, self.service.settings.chat_auto_save and not self.service.settings.paused)
            store.flush(chat["id"])
            return chat

    async def discover(self, store, automatic):
        offset = store.state("discovery_offset", 0)
        automatic = self.service.settings.chat_auto_save and not self.service.settings.paused
        existing = {chat["id"] for chat in store.chats()}
        page = await self.service.client.call("im.recent.list", {"OFFSET": offset, "LIMIT": 200,
                            "SKIP_OPENLINES": "N", "PARSE_TEXT": "N", "GET_ORIGINAL_TEXT": "Y"}, v3=False)
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise ValueError("Неизвестный формат списка чатов; обнаружение не завершено")
        for item in page["items"]:
            id = positive(item.get("chat_id") or item.get("chatId") or (item.get("chat") or {}).get("id"))
            dialog = str(item.get("id")) if item.get("type") == "user" else f"chat{id}" if id else str(item.get("id") or "")
            if id:
                hint = item.get("user") if item.get("type") == "user" else item.get("chat")
                hint = hint if isinstance(hint, dict) else {}
                values = self.recent_metadata(item)
                values["source_recent_seen_at"] = time.time()
                if item.get("type") == "user" and positive(item.get("id")) == store.user_id:
                    values.update(is_self=True, aliases=list(dict.fromkeys(["Мои заметки", "Избранное", str(item.get("title") or ""), str(hint.get("name") or ""), self.service.db.get_state(self.service.identity_key()) or ""])))
                if id not in existing:
                    values["auto_history"] = False
                store.upsert_chat(id, dialog, title=str(item.get("title") or hint.get("name") or f"Чат {id}"), type=str(item.get("type") or "chat"), **values)
            elif re.fullmatch(r"(?:chat|sg)?[1-9]\d*", dialog):
                chat = await self.add_dialog(store, dialog, item)
                id = chat["id"]
                if id not in existing:
                    store.update_chat(id, auto_history=False)
                store.update_chat(id, **self.recent_metadata(item))
            if id:
                self.schedule_chat(store, store.chat(id), automatic)
        # Only meetings whose participant list contains this account may seed chat IDs.
        known = [json.loads(r["metadata"]) for r in self.service.db.rows("SELECT metadata FROM meetings WHERE portal=? AND source='bitrix'", (store.portal,))]
        peers = self.service.personal_chat_peers(known)
        existing = {chat["id"] for chat in store.chats()}
        for metadata in known:
            id = positive(metadata.get("chatId"))
            participant_ids = {positive(p.get("userId") or p.get("user_id") or p.get("id")) if isinstance(p, dict) else positive(p) for p in metadata.get("participants", [])}
            if id and id not in existing and store.user_id in participant_ids:
                dialog = str(peers.get(id) or f"chat{id}")
                store.upsert_chat(id, dialog, title=str(metadata.get("chatTitle") or f"Чат {id}"), auto_history=False)
                existing.add(id)
        if not (page.get("hasMore") or page.get("hasMorePages")):
            await self.extra_recent(store)
        for chat in store.chats():
            self.schedule_chat(store, chat, automatic)
        if page.get("hasMore") or page.get("hasMorePages"):
            if not page["items"]:
                raise ValueError("Пустая страница при незавершённом списке чатов")
            # The source de-duplicates internal rows: a short page can still have
            # more pages. Its documented offset advances by LIMIT, not items.
            store.set_state("discovery_offset", offset + 200)
        else:
            store.set_state("discover_requested", None)
            store.set_state("discovery_offset", 0)
            store.set_state("last_discovery", time.time())
            store.set_state("inventory_ready", True)

    async def fetch_page(self, chat, work):
        client = self.service.client
        params = {"CHAT_ID": chat["id"], "ORDER": {"ID": "DESC"}, "LIMIT": 200}
        if work.get("cursor"):
            params["LAST_ID"] = work["cursor"]
        for field, key in (("DATE_FROM", "date_from"), ("DATE_TO", "date_to")):
            if work.get(key):
                params[field] = work[key]
        fallback = chat.get("method") == "get"
        if not fallback:
            try:
                page = await client.call("im.dialog.messages.search", params, v3=False)
            except BitrixError as exc:
                if exc.code not in UNSUPPORTED_CODES:
                    raise
                fallback = True
        if fallback:
            args = {"DIALOG_ID": chat["dialog"], "LIMIT": 50}
            if work.get("cursor"):
                args["LAST_ID"] = work["cursor"]
            page = await client.call("im.dialog.messages.get", args, v3=False)
            if positive(page.get("chat_id") or page.get("chatId")) != chat["id"]:
                raise ValueError("Источник вернул историю другого чата")
        if not isinstance(page, dict) or not isinstance(page.get("messages"), list):
            raise ValueError("Неизвестный формат истории; страница не считается пустой")
        return page, "get" if fallback else "search"

    async def process_work(self, store, row):
        chat = store.chat(row["chat"])
        work = json.loads(row["data"])
        if row["kind"] == "metadata":
            try:
                details = await self.service.client.call("im.dialog.get", {"DIALOG_ID": chat["dialog"]}, v3=False)
                if not isinstance(details, dict) or positive(details.get("id")) != chat["id"]:
                    raise ValueError("Источник не подтвердил идентичность чата")
                members = await self.service.client.call("im.chat.user.list", {"CHAT_ID": chat["id"]}, v3=False)
                if not isinstance(members, list) or any(not positive(id) for id in members):
                    raise ValueError("Источник не предоставил состав участников")
                names, activity = {}, {}
                for offset in range(0, len(members), 100):
                    users = await self.service.client.call("im.user.list.get", {"ID": members[offset:offset+100], "RESULT_TYPE": "array"}, v3=False)
                    names.update({positive(u.get("id")): str(u.get("name") or "") for u in sequence(users) if isinstance(u, dict)})
                    activity.update({positive(u.get("id")): u["active"] for u in sequence(users) if isinstance(u, dict) and type(u.get("active")) is bool})
                peer = positive(chat["dialog"]) if chat["type"] == "user" else 0
                peer_state = {"peer_active": activity[peer]} if peer in activity else {}
                store.update_chat(chat["id"], title=str(details.get("name") or names.get(peer) or chat["title"]), participants=[{"id":positive(id),"name":names.get(positive(id),next((p.get("name", "") for p in chat.get("participants",[]) if p["id"]==positive(id)),"")), **({"active":activity[positive(id)]} if positive(id) in activity else {})} for id in members], **peer_state, **chat_classification(details),
                                  participants_at=time.time(), participants_warning="", parent_chat_id=positive(details.get("parent_chat_id")), parent_message_id=positive(details.get("parent_message_id")))
            except (BitrixError, httpx.HTTPError, ValueError, OSError) as exc:
                if isinstance(exc, (httpx.HTTPError, OSError)) or isinstance(exc, BitrixError) and exc.retryable:
                    raise
                store.update_chat(chat["id"], participants_at=time.time(), participants_warning=self.service.vault.redact(str(exc)))
            self.service.db.execute("DELETE FROM ca_work WHERE account=? AND chat=? AND kind=?", (store.account, chat["id"], row["kind"]))
            await durable_io(store.flush, chat["id"])
            return
        if not manual_history_complete(chat) and (row["kind"] == "new" or
                row["kind"] in {"recent_check", "audit"} and work.get("automatic")):
            # A stale queue/portable manifest must not bypass the current policy.
            cutoff = auto_since_for(store, chat)
            if not work.get("date_from") or (instant(work["date_from"]) or 0) < (instant(cutoff) or 0):
                work.update(date_from=cutoff, cursor=0, pages=0, messages=0)
        page, method = await self.fetch_page(chat, work)
        raw_messages = page["messages"]
        # get treats a missing/deleted anchor as an empty result. Restart a bounded
        # traversal from the newest page; only a verified lower boundary may finish it.
        if not raw_messages and work.get("cursor") and method == "get":
            if not work.get("anchor_restart"):
                work.update(restart_below=work["cursor"], cursor=0, anchor_restart=True)
                self.service.db.execute("UPDATE ca_work SET data=?,touched=? WHERE account=? AND chat=? AND kind=?",
                                        (canonical(work), time.time(), store.account, chat["id"], row["kind"]))
                store.update_chat(chat["id"], coverage="verifying_boundary", limitations=["Проверяется граница истории: курсор мог быть удалён"])
                store.flush(chat["id"])
                return
        # Fallback has no date filter. Traverse source pages, but only persist the
        # requested period; outside-period related originals remain context.
        selected_page = dict(page)
        if work.get("date_from") or work.get("date_to"):
            def in_period(raw):
                date = raw.get("date") or ""
                moment = instant(date)
                return moment is not None and (not work.get("date_from") or moment >= instant(work["date_from"])) and (not work.get("date_to") or moment <= instant(work["date_to"]))
            selected_page["messages"] = [m for m in raw_messages if in_period(m)]
        if row["kind"] in {"recent_check", "audit"}:
            page_ids = [positive(m.get("id")) for m in selected_page["messages"]]
            archived = {r["id"] for r in self.service.db.rows("SELECT id FROM ca_messages WHERE account=? AND chat=? AND id IN (" + ",".join("?" for _ in page_ids) + ")", (store.account, chat["id"], *page_ids))} if page_ids else set()
            selected_page["messages"] = [m for m in selected_page["messages"] if positive(m.get("id")) in archived]
        if work.get("date_from") or work.get("date_to") or row["kind"] in {"recent_check", "audit"}:
            selected_page = related_materials(selected_page, chat["id"])
        messages = await durable_io(store.save_page, chat["id"], selected_page)
        work.pop("error",None)
        work["pages"]=work.get("pages",0)+1
        work["messages"]=work.get("messages",0)+len(messages)
        ids = [positive(m.get("id")) for m in raw_messages]
        if any(not id for id in ids):
            raise ValueError("Источник вернул сообщение без ID; курсор не продвинут")
        cursor = min(ids) if ids else work.get("cursor", 0)
        if ids and work.get("cursor") and cursor >= work["cursor"]:
            raise ValueError("Источник повторил курсор истории; граница не подтверждена")
        limit = 50 if method == "get" else 200
        stop = bool(work.get("stop_id") and any(id <= work["stop_id"] for id in ids))
        below_date = bool(work.get("date_from") and any(instant(m.get("date")) is not None and instant(m["date"]) < instant(work["date_from"]) for m in raw_messages))
        finished = not ids or len(ids) < limit or stop or (method == "get" and below_date)
        if method == "get" and work.get("anchor_restart") and not ids and work.get("cursor"):
            # Repeated empty cursor does not prove completeness.
            raise ValueError("Граница истории не подтверждена после повторного прохода; повторите сверку")
        if method == "get" and work.get("restart_below") and ids and cursor >= work["restart_below"]:
            finished = False
        tariff = bool((page.get("tariffRestrictions") or {}).get("isHistoryLimitExceeded"))
        limitations = list(dict.fromkeys(chat.get("limitations", []) + (["История ограничена тарифом Bitrix24"] if tariff else []) +
                     (["Метод поиска недоступен; связи и граница тарифа могут отсутствовать"] if method == "get" else [])))
        update = {"last_checked": stamp(), "method": method, "limitations": limitations, "error": ""}
        if row["kind"] == "new" and finished:
            update.update(sync_head_id=positive(work.get("source_head_id")) or max(ids, default=0), new_checked_at=time.time())
        from .call_discovery import structured_calls
        call_ids = structured_calls(raw_messages)
        meeting_ids = set(chat.get("meeting_ids", []))
        for call_id in call_ids:
            for meeting in self.service.db.rows("SELECT id,metadata FROM meetings WHERE portal=? AND call_id=? AND source='bitrix'", (store.portal, str(call_id))):
                metadata = json.loads(meeting["metadata"])
                people = {positive(p.get("userId") or p.get("user_id") or p.get("id")) if isinstance(p, dict) else positive(p) for p in metadata.get("participants", [])}
                if positive(metadata.get("chatId")) == chat["id"] and store.user_id in people:
                    meeting_ids.add(meeting["id"])
        update["meeting_ids"] = sorted(meeting_ids)
        if row["kind"] == "history":
            update.update(history_since=work.get("date_from", "")[:10], history_complete=finished,
                          coverage="tariff_limited" if tariff else "source_boundary" if finished and method == "search" else "boundary_unverified" if finished else "backfilling")
        elif chat["coverage"] in {"access_lost", "error"}:
            update["coverage"] = "backfilling" if not chat.get("history_complete") else "source_boundary" if method == "search" else "boundary_unverified"
        if tariff:
            update["coverage"] = "tariff_limited"
        store.update_chat(chat["id"], **update)
        # Enabled types apply whenever metadata is collected, including manual
        # history. The history checkbox separately requeues already known files.
        manual_collection = not work.get("automatic", False)
        for message in messages:
            for file_id in message["file_ids"]:
                self.queue_file(store, chat["id"], file_id, automatic=True, missing_ok=True, manual_collection=manual_collection, collection_kind=row["kind"])
        if finished:
            self.service.db.execute("DELETE FROM ca_work WHERE account=? AND chat=? AND kind=?", (store.account, chat["id"], row["kind"]))
        else:
            work["cursor"] = cursor
            self.service.db.execute("UPDATE ca_work SET data=?,touched=?,next_at=0 WHERE account=? AND chat=? AND kind=?",
                                    (canonical(work), time.time(), store.account, chat["id"], row["kind"]))
        await durable_io(store.flush, chat["id"])
        store.set_state("last_progress", {"at": time.time(), "chat": chat["id"], "kind": row["kind"],
                                          "pages": work["pages"], "messages": work["messages"]})
        return {"pages": work["pages"], "messages": work["messages"], "automatic": bool(work.get("automatic")),
                "priority": row["priority"], "manual_history_origin": chat.get("manual_history_origin", "")}

    def status(self):
        store = self.store()
        work = getattr(self, "current_work", None)
        current = work if work and work[0] == store.account else None
        last = store.state("last_discovery", 0)
        running = bool(self.service.alive and getattr(self, "loop_running", False))
        automatic = self.service.settings.chat_auto_save and not self.service.settings.paused
        return {"discovering": bool(getattr(self, "discovering", False) or store.state("discover_requested")),
                "last_catalogue_at": last,
                "next_catalogue_at": max(last + self.service.settings.chat_poll_seconds, store.state("discovery_retry_at", 0)),
                "worker_running": running, "file_worker_running": bool(getattr(self, "file_loop_running", False)),
                "last_cycle_at": getattr(self, "last_cycle_at", 0), "last_progress": store.state("last_progress", {}),
                "active_chat": current[1] if current else 0, "active_kind": current[2] if current else "",
                "connected": self.service.connected(), "automatic": automatic,
                "paused": self.service.settings.paused, "error": self.error}

    def automatic_file_allowed(self, store, chat, id, origin, collection_kind=""):
        details = store.chat(chat)
        requested = details.get("manual_history_requested") is True and details.get("manual_history_origin") in MANUAL_HISTORY_ORIGINS
        if origin in {"history_opt_in", "manual_file"} or origin == "manual_collection" and (requested or collection_kind.startswith("period:")) or manual_history_complete(details):
            return True
        return bool(self.service.db.rows("""SELECT 1 FROM ca_messages m,json_each(m.data,'$.file_ids') f
            WHERE m.account=? AND m.chat=? AND f.value=? AND julianday(m.date)>=julianday(?) LIMIT 1""",
            (store.account, chat, id, store.state("auto_since", stamp()))))

    def queue_file(self, store, chat, id, *, automatic=False, missing_ok=False, manual_collection=False, history_opt_in=False, collection_kind=""):
        try:
            file = store.file(chat, id)
        except ValueError:
            if missing_ok:
                return
            raise
        s = self.service.settings
        restore_collection = manual_collection and (collection_kind == "history" or collection_kind.startswith("period:"))
        if (file.get("locally_deleted") or file.get("auto_suppressed")) and automatic and not restore_collection:
            return
        manual_collection = manual_collection or file.get("download_origin") == "manual_collection" and file["state"] == "queued"
        if automatic and (not manual_collection and not self.selected(chat) or not getattr(s, "chat_download_" + file["category"])):
            return
        if file["state"] == "running" or automatic and file["state"] == "saved":
            return
        origin = "manual_collection" if manual_collection else "history_opt_in" if history_opt_in else "automatic"
        collection_kind = collection_kind or file.get("collection_kind", "")
        if automatic and store.state("collection_policy", 0) >= COLLECTION_POLICY and not self.automatic_file_allowed(store, chat, id, origin, collection_kind):
            return
        if automatic and s.chat_max_file_mb and file["size"] > s.chat_max_file_mb * 1024**2:
            store.file_update(chat, id, state="size_limited", error=f"Размер файла превышает лимит {s.chat_max_file_mb} МБ. Текст сохранён; файл можно скачать вручную.")
            return
        data = json.loads(self.service.db.rows("SELECT data FROM ca_files WHERE account=? AND chat=? AND id=?", (store.account, chat, id))[0]["data"])
        data["download_origin"] = "manual_collection" if manual_collection else "history_opt_in" if history_opt_in else "automatic" if automatic else "manual_file"
        if not automatic or restore_collection:
            data.pop("locally_deleted", None)
            data.pop("auto_suppressed", None)
        if manual_collection:
            data["collection_kind"] = collection_kind
        data.update(queued_at=time.time(),downloaded_bytes=0)
        # Manual request promotes a queued automatic job and bypasses all gates.
        store.file_update(chat, id, state="queued", automatic=int(automatic and (file["state"] != "queued" or file["automatic"])), data=canonical(data), error="", next_at=0)

    async def settings_changed(self, old):
        store = self.store()
        s = self.service.settings
        if self.service.connected():
            if s.chat_auto_save and not old.chat_auto_save:
                store.set_state("auto_since", stamp())
                # Queued checks from the previous enabled period must be rebased.
                self.service.db.execute("DELETE FROM ca_work WHERE account=? AND kind='new' AND json_extract(data,'$.automatic')=1", (store.account,))
            if s.chat_auto_save and (not old.chat_auto_save or old.chat_archive_root != s.chat_archive_root or
                                    old.chat_scope != s.chat_scope or old.chat_selected_ids != s.chat_selected_ids or
                                    old.chat_excluded_ids != s.chat_excluded_ids or old.chat_history_since != s.chat_history_since):
                self.request_discovery(True)
            for chat in store.chats():
                for file in store.files(chat["id"]):
                    manual_collection = file.get("download_origin") == "manual_collection"
                    enabled = (manual_collection or self.selected(chat["id"])) and getattr(s, "chat_download_" + file["category"])
                    if file["state"] == "queued" and file["automatic"] and (not enabled or not s.chat_auto_save and file.get("download_origin") not in {"manual_collection", "history_opt_in"}):
                        store.file_update(chat["id"], file["id"], state="not_saved", automatic=0)
                    elif s.chat_download_history and enabled:
                        self.queue_file(store, chat["id"], file["id"], automatic=True, history_opt_in=True)
        # Event consent belongs to a portal/user, independently of the storage root.
        if not s.chat_events and old.chat_events and self.service.connected():
            self.service.db.set_state(f"chat_events:{s.portal}:{s.user_id}:unsubscribe", "1")
        if s.chat_events and not old.chat_events:
            key = f"chat_events:{s.portal}:{s.user_id}:consent" if self.service.connected() else "chat_events:pending_consent"
            self.service.db.set_state(key, "1")
        if not s.chat_events:
            self.service.db.set_state("chat_events:pending_consent", "")

    async def events(self, store):
        db, client = self.service.db, self.service.client
        if self.service.settings.chat_auto_save and not store.state("auto_since"):
            store.set_state("auto_since", stamp())
        prefix = f"chat_events:{store.portal}:{store.user_id}:"
        if not db.get_state(prefix + "subscribed"):
            await client.call("im.v2.Event.subscribe", {}, v3=False)
            db.set_state(prefix + "subscribed", "1")
        offset = int(db.get_state(prefix + "offset", "0"))
        last = float(db.get_state(prefix + "last", "0"))
        if last and time.time() - last > 86400:
            for chat in store.chats():
                store.update_chat(chat["id"], event_gap=True)
                self.schedule_chat(store, chat, True)
        page = await client.call("im.v2.Event.get", {"offset": offset, "limit": 100}, v3=False)
        if not isinstance(page, dict) or not isinstance(page.get("events"), list) or type(page.get("nextOffset")) is not int:
            raise ValueError("Неизвестный формат событий; подтверждение не отправлено")
        for event in page["events"]:
            if not isinstance(event, dict) or not isinstance(event.get("data") or {}, dict):
                raise ValueError("Неизвестный формат события; подтверждение не отправлено")
            id = positive(event.get("eventId"))
            if not id:
                raise ValueError("Событие без ID; подтверждение не отправлено")
            data = event.get("data") or {}
            chat_raw = data.get("chat") or {}
            raw = data.get("message") or {}
            if not isinstance(chat_raw, dict) or not isinstance(raw, dict):
                raise ValueError("Неизвестный формат сообщения события; подтверждение не отправлено")
            chat_id = positive(chat_raw.get("id") or raw.get("chatId") or data.get("chatId"))
            # Excluded/unknown chats retain only an acknowledgment marker, never their content.
            message_id = positive(raw.get("id") or data.get("messageId"))
            known = db.rows("SELECT data FROM ca_chats WHERE account=? AND id=?", (store.account, chat_id))
            previous = store.message(chat_id, message_id) if known else None
            full_history = bool(known and manual_history_complete(json.loads(known[0]["data"])))
            cutoff = auto_since_for(store, json.loads(known[0]["data"]) if known else {})
            collect_new = self.service.settings.chat_auto_save and (full_history or
                          (instant(raw.get("date")) or 0) >= (instant(cutoff) or time.time()))
            journal = safe_metadata(event) if chat_id and self.selected(chat_id) and (collect_new or previous) else {"eventId": id, "type": str(event.get("type") or ""), "chatId": chat_id, "skipped": True}
            db.execute("INSERT OR IGNORE INTO ca_events VALUES(?,?,?)", (store.account, id, canonical(journal)))
            if chat_id and self.selected(chat_id):
                if not db.rows("SELECT id FROM ca_chats WHERE account=? AND id=?", (store.account, chat_id)):
                    store.upsert_chat(chat_id, f"chat{chat_id}", title=str(chat_raw.get("name") or f"Чат {chat_id}"),
                                      auto_history=False)
                deleted = event.get("type") == "ONIMV2MESSAGEDELETE"
                if message_id and (previous or collect_new) and (deleted or "text" in raw or event.get("type") in {"ONIMV2REACTIONCHANGE", "ONIMV2MESSAGEREACTIONCHANGE"}):
                    # Reduced event payloads must not erase fields omitted by the source.
                    merged = {"id": message_id, "chatId": chat_id}
                    if previous:
                        merged.update(authorId=previous["author_id"], date=previous["date"], text=previous["text"],
                                      params=previous["source"].get("params", {}), reactions=previous["reactions"],
                                      fileIds=previous["file_ids"], isSystem=previous["system"], deleted=previous["deleted"],
                                      forward=[{"id": r["message_id"], "chatId": r["chat_id"], "userId": r["author_id"],
                                                "date": r["date"], "text": r["excerpt"]} for r in previous["relations"] if r["kind"] == "forward"])
                        for field, value in previous["source"].items():
                            merged.setdefault(field, value)
                        for link in previous["relations"]:
                            if link["kind"] in {"reply", "quote"}:
                                merged[link["kind"]] = {"id": link["message_id"], "chatId": link["chat_id"], "userId": link["author_id"], "text": link["excerpt"], "date": link["date"]}
                    merged.update(raw)
                    if event.get("type") in {"ONIMV2REACTIONCHANGE", "ONIMV2MESSAGEREACTIONCHANGE"}:
                        reaction = data.get("reaction")
                        if isinstance(reaction, str):
                            reactions = copy.deepcopy(previous["reactions"]) if previous and isinstance(previous["reactions"], dict) else {}
                            users_by_reaction = reactions.setdefault("reactionUsers", {})
                            counters = reactions.setdefault("reactionCounters", {})
                            person = positive((data.get("user") or {}).get("id"))
                            people = users_by_reaction.setdefault(reaction, [])
                            if data.get("action") == "add" and person and person not in people:
                                people.append(person)
                                counters[reaction] = max(positive(counters.get(reaction)), len(people) - 1) + 1
                            elif data.get("action") == "delete" and person in people:
                                people.remove(person)
                                counters[reaction] = max(0, positive(counters.get(reaction)) - 1)
                            merged["reactions"] = reactions
                        else:
                            merged["reactions"] = data.get("reactions") or raw.get("reactions") or merged.get("reactions", [])
                    if deleted:
                        merged["deleted"] = True
                    user = data.get("user") or {}
                    users = [user] if isinstance(user, dict) else []
                    if previous and previous["author"]:
                        users.append({"id": previous["author_id"], "name": previous["author"]})
                    store.save_page(chat_id, {"messages": [merged], "users": users, "files": data.get("files", [])})
                self.schedule_chat(store, store.chat(chat_id), self.service.settings.chat_auto_save and not self.service.settings.paused)
                store.flush(chat_id)
        # Journal is portable and durable before advancing the ACK offset.
        from .chat_storage import atomic_text
        entries = db.rows("SELECT data FROM ca_events WHERE account=? ORDER BY id", (store.account,))
        atomic_text(store.folder / "events.jsonl", "".join(r["data"] + "\n" for r in entries))
        db.set_state(prefix + "offset", str(page["nextOffset"]))
        db.set_state(prefix + "last", str(time.time()))

    async def upgrade_collection_policy(self, store):
        """Cancel implicit backfills while keeping explicit work and saved data."""
        policy = store.state("collection_policy", 0)
        if policy >= COLLECTION_POLICY:
            return
        db = self.service.db
        if policy < 2 or not store.state("auto_since"):
            store.set_state("auto_since", stamp())
        cutoff = store.state("auto_since")
        chats = {r["id"]: json.loads(r["data"]) for r in db.rows("SELECT id,data FROM ca_chats WHERE account=?", (store.account,))}
        work = db.rows("SELECT chat,kind,data,priority FROM ca_work WHERE account=?", (store.account,))
        # v0.2.29 catalogue refresh also produced automatic=False, priority=10.
        # Only modern manual priority=2 or explicit origin proves the request.
        manual = {r["chat"] for r in work if r["kind"] == "history" and json.loads(r["data"]).get("automatic") is False and r["priority"] == 2}
        outcomes = db.rows("""SELECT data FROM ca_activity
            WHERE account=? AND json_extract(data,'$.kind')='history' AND json_extract(data,'$.state')='done'
            AND json_type(data,'$.automatic')='false'""", (store.account,))
        for row in outcomes:
            data = json.loads(row["data"])
            prior = chats.get(data["chat"], {})
            if data.get("priority") == 2 or data.get("manual_history_origin") in MANUAL_HISTORY_ORIGINS or policy == 2 and type(prior.get("auto_history")) is bool:
                manual.add(data["chat"])
        dirty = set()
        for id, chat in chats.items():
            origin = chat.get("manual_history_origin", "")
            if chat.get("manual_history_requested") is True and origin in MANUAL_HISTORY_ORIGINS:
                confirmed = True
            elif id in manual:
                confirmed, origin = True, "manual_job_v2"
            elif policy == 2 and "manual_history_requested" not in chat and chat.get("history_complete") is True and chat.get("auto_history") is False:
                # In policy 2 an explicitly false flag never scheduled automatic
                # history. Absence of the flag (older formats) proves nothing.
                confirmed, origin = True, "baseline_v2"
            else:
                confirmed, origin = False, ""
            if chat.get("auto_history") or chat.get("manual_history_requested", False) != confirmed or chat.get("manual_history_origin", "") != origin:
                store.update_chat(id, auto_history=False, manual_history_requested=confirmed, manual_history_origin=origin)
                dirty.add(id)
            chat.update(manual_history_requested=confirmed, manual_history_origin=origin)
        for row in work:
            data = json.loads(row["data"])
            if row["kind"] == "history" and not (chats[row["chat"]].get("manual_history_requested") and chats[row["chat"]].get("manual_history_origin") in MANUAL_HISTORY_ORIGINS):
                db.execute("DELETE FROM ca_work WHERE account=? AND chat=? AND kind='history'", (store.account, row["chat"]))
                store.activity("history", row["chat"], "cancelled", message="Подтвердите старую историю кнопкой «Сохранить выбранные»")
                if not chats[row["chat"]].get("history_complete"):
                    store.update_chat(row["chat"], coverage="pending")
                dirty.add(row["chat"])
                continue
            if row["kind"] == "new" and data.get("automatic") is False and row["priority"] != 2:
                data["automatic"] = True  # Old catalogue checks are not manual downloads.
                db.execute("UPDATE ca_work SET data=? WHERE account=? AND chat=? AND kind='new'", (canonical(data), store.account, row["chat"]))
                dirty.add(row["chat"])
            if not data.get("automatic"):
                continue
            if row["kind"] == "history":
                db.execute("DELETE FROM ca_work WHERE account=? AND chat=? AND kind='history'", (store.account, row["chat"]))
                store.activity("history", row["chat"], "cancelled", message="Старая история сохраняется только по ручному выбору")
                if not chats[row["chat"]].get("history_complete"):
                    store.update_chat(row["chat"], coverage="pending")
                dirty.add(row["chat"])
            elif row["kind"] in {"new", "recent_check", "audit"} and not manual_history_complete(chats[row["chat"]]):
                if row["kind"] != "new" and not self.has_current_messages(store, row["chat"]):
                    db.execute("DELETE FROM ca_work WHERE account=? AND chat=? AND kind=?", (store.account, row["chat"], row["kind"]))
                    store.activity(row["kind"], row["chat"], "cancelled", message="Проверяются только сохранённые новые сообщения")
                    dirty.add(row["chat"])
                    continue
                if policy >= 2 and data.get("date_from") and (instant(data["date_from"]) or 0) >= (instant(cutoff) or 0):
                    continue  # Already bounded: retain the page cursor and retry state.
                data.update(cursor=0, date_from=cutoff, pages=0, messages=0)
                data.pop("error", None)
                db.execute("UPDATE ca_work SET data=?,next_at=0 WHERE account=? AND chat=? AND kind=?",
                           (canonical(data), store.account, row["chat"], row["kind"]))
                dirty.add(row["chat"])
        # Files already explicitly requested (including the history checkbox)
        # remain queued. Only accidental older automatic files are withdrawn.
        newer_files = {(r["chat"], r["id"]) for r in db.rows("""SELECT DISTINCT m.chat,CAST(f.value AS INTEGER) AS id
            FROM ca_messages m,json_each(m.data,'$.file_ids') f
            WHERE m.account=? AND julianday(m.date)>=julianday(?)""", (store.account, cutoff))}
        for row in db.rows("SELECT chat,id,data FROM ca_files WHERE account=? AND automatic=1 AND state IN ('queued','error','size_limited')", (store.account,)):
            file = json.loads(row["data"])
            origin = file.get("download_origin", "automatic")
            requested = chats[row["chat"]].get("manual_history_requested") and chats[row["chat"]].get("manual_history_origin") in MANUAL_HISTORY_ORIGINS
            explicit = origin in {"manual_file", "history_opt_in"} or origin == "manual_collection" and (requested or file.get("collection_kind", "").startswith("period:"))
            if not explicit and not manual_history_complete(chats[row["chat"]]) and (row["chat"], row["id"]) not in newer_files:
                store.file_update(row["chat"], row["id"], state="not_saved", automatic=0, error="", next_at=0)
                dirty.add(row["chat"])
        for id in dirty:
            if (store.chat_folder(id) / "chat.json").exists():
                await durable_io(store.flush, id)
        if policy < 2:
            store.set_state("inventory_ready", False)
            store.set_state("discover_requested", None)
            store.set_state("last_discovery", 0)
        store.set_state("collection_policy", COLLECTION_POLICY)

    async def step(self):
        if not self.service.connected():
            return
        async with self.service.auth_lock, self.lock:
            store = self.store()
            s = self.service.settings
            if store.account not in self.recovered:
                await durable_io(store.recover)
                self.recovered.add(store.account)
            elif store.state("cleanup_pending", False):
                from .chat_cleanup import recover_pending
                await durable_io(recover_pending, store)
            await self.upgrade_collection_policy(store)
            automatic = s.chat_auto_save and not s.paused
            prefix = f"chat_events:{store.portal}:{store.user_id}:"
            if automatic and not store.state("auto_since"):
                store.set_state("auto_since", stamp())
            if s.chat_events and self.service.db.get_state("chat_events:pending_consent"):
                self.service.db.set_state(prefix + "consent", "1")
                self.service.db.set_state("chat_events:pending_consent", "")
            if self.service.db.get_state(prefix + "unsubscribe"):
                await self.service.client.call("im.v2.Event.unsubscribe", {}, v3=False)
                self.service.db.set_state(prefix + "subscribed", "")
                self.service.db.set_state(prefix + "consent", "")
                self.service.db.set_state(prefix + "unsubscribe", "")
            requested = store.state("discover_requested")
            if not requested and time.time() - store.state("last_discovery", 0) >= s.chat_poll_seconds:
                self.request_discovery(False)
                requested = store.state("discover_requested")
            if requested and time.time() >= store.state("discovery_retry_at", 0) and (not requested.get("automatic") or automatic):
                self.discovering=True
                try:
                    await self.discover(store, requested.get("automatic", False))
                    store.set_state("discovery_retry_at", 0)
                    if not store.state("discover_requested"):
                        store.activity("discovery", state="done")
                except (BitrixError, httpx.HTTPError, ValueError, OSError) as exc:
                    # An inventory failure must not block already known chats.
                    self.error = self.service.vault.redact(str(exc))
                    store.set_state("discovery_retry_at", max(time.time() + 30, getattr(exc, "retry_at", 0)))
                finally:
                    self.discovering=False
            if automatic:
                for kind, seconds, days, priority in (("recent_check", 86400, 7, 3), ("audit", 604800, 0, 15)):
                    if time.time() - store.state(kind + "_at", 0) >= seconds:
                        for chat in store.chats():
                            if self.selected(chat["id"]):
                                first = chat["count"]["first"]
                                if not first:
                                    continue
                                recent = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else first
                                bounds = [first, recent]
                                if not manual_history_complete(chat):
                                    if not self.has_current_messages(store, chat["id"]):
                                        continue
                                    bounds.append(auto_since_for(store, chat))
                                start = max(bounds, key=lambda value: instant(value) or 0)
                                store.enqueue(chat["id"], kind, {"cursor": 0, "date_from": start, "automatic": True}, priority)
                        store.set_state(kind + "_at", time.time())
            if s.chat_events and self.service.db.get_state(prefix + "consent") and not s.paused and time.time() - store.state("events_at", 0) >= 15:
                try:
                    await self.events(store)
                    store.set_state("event_error", "")
                except (BitrixError, httpx.HTTPError, ValueError, OSError) as exc:
                    store.set_state("event_error", self.service.vault.redact(str(exc)))
                store.set_state("events_at", time.time())
            # Reserve regular turns for explicitly requested history and metadata;
            # a continuous stream of new-message checks must not starve them.
            self.work_turn = getattr(self, "work_turn", 0) + 1
            background_turn = self.work_turn % 5 == 0
            order = "CASE WHEN priority>0 THEN 0 ELSE 1 END," if background_turn else ""
            rows = self.service.db.rows(f"SELECT * FROM ca_work WHERE account=? AND next_at<=? ORDER BY {order}priority,touched,chat", (store.account, time.time()))
            for row in rows:
                work = json.loads(row["data"])
                if work.get("automatic") and (not automatic or not self.selected(row["chat"])):
                    continue
                self.active = f"Чат {row['chat']}"
                self.current_work=(store.account,row["chat"],row["kind"])
                try:
                    result = await self.process_work(store, row)
                    left=self.service.db.rows("SELECT data FROM ca_work WHERE account=? AND chat=? AND kind=?",(store.account,row["chat"],row["kind"]))
                    if not left:
                        store.activity(row["kind"], row["chat"], "done", **(result or {}))
                    self.error = ""
                except (BitrixError, httpx.HTTPError, ValueError, OSError) as exc:
                    message = self.service.vault.redact(str(exc))
                    work.update(error=message)
                    self.service.db.execute("UPDATE ca_work SET data=?,next_at=?,touched=? WHERE account=? AND chat=? AND kind=?",
                                            (canonical(work),max(time.time() + 30, getattr(exc, "retry_at", 0)), time.time(), store.account, row["chat"], row["kind"]))
                    store.update_chat(row["chat"], error=message, coverage="access_lost" if isinstance(exc, BitrixError) and exc.code in ACCESS_CODES else "error")
                    self.error = message
                finally:
                    self.active = ""
                    self.current_work=None
                break  # One page per turn: fair across chats and cancellable.

    async def loop(self):
        self.loop_running = True
        try:
            while self.service.alive:
                self.last_cycle_at = time.time()
                try:
                    await self.step()
                except Exception as exc:
                    self.error = self.service.vault.redact(str(exc))
                    await asyncio.sleep(10)
                await asyncio.sleep(1)
        finally:
            self.loop_running = False
            self.active = ""
            self.current_work = None

    async def download_file(self, store, file, client=None):
        chat = store.chat(file["chat"])
        client = client or self.service.client
        result = {"downloadUrl": file["download_url"]} if file.get("download_url") else await client.call("im.v2.File.download", {"dialogId": chat["dialog"], "fileId": file["id"]}, v3=False)
        url = client.safe_download_url(result.get("downloadUrl") or "")
        if not result.get("downloadUrl"):
            raise ValueError("Источник не предоставил ссылку скачивания")
        folder = store.chat_folder(file["chat"]) / "attachments" / f"file-{file['id']}"
        folder.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(folder).free < file["size"] + 1024**3:
            raise OSError("Недостаточно места: требуется размер файла и резерв 1 ГБ")
        fd, name = tempfile.mkstemp(prefix=".download-", dir=folder)
        os.close(fd)
        temp = Path(name)
        digest, size = hashlib.sha256(), 0
        progress_at=0
        try:
            for redirect in range(6):
                async with client.http.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = client.safe_download_url(urljoin(url, response.headers.get("location") or ""))
                        continue
                    response.raise_for_status()
                    declared = positive(response.headers.get("content-length"))
                    maximum = file.get("maximum", self.service.settings.chat_max_file_mb * 1024**2) if file["automatic"] else 0
                    if maximum and max(file["size"], declared) > maximum:
                        raise ValueError("Размер превышает лимит автоматической загрузки; доступно ручное скачивание")
                    with temp.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if time.monotonic()-progress_at>=1:
                                progress_at=time.monotonic()
                                progress=json.loads(self.service.db.rows("SELECT data FROM ca_files WHERE account=? AND chat=? AND id=?",(store.account,file["chat"],file["id"]))[0]["data"])
                                progress["downloaded_bytes"]=size
                                store.file_update(file["chat"],file["id"],data=canonical(progress))
                            if maximum and size > maximum:
                                raise ValueError("Размер превышает лимит автоматической загрузки")
                            if shutil.disk_usage(folder).free < len(chunk) + 1024**3:
                                raise OSError("Недостаточно места: резерв 1 ГБ")
                            output.write(chunk)
                            digest.update(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    expected = file.get("source_size", file["size"])
                    if expected and expected != size or declared and declared != size:
                        raise OSError("Размер скачанного файла не совпадает; повторите загрузку")
                    break
            else:
                raise ValueError("Слишком много перенаправлений файла")
            sha = digest.hexdigest()
            with temp.open("rb") as check:
                verified = hashlib.file_digest(check, "sha256").hexdigest()
            if verified != sha:
                raise OSError("Проверка SHA-256 файла не пройдена")
            target = folder / (sha + "_" + safe_name(file["name"]))
            os.replace(temp, target)
            async with self.lock:
                data = json.loads(self.service.db.rows("SELECT data FROM ca_files WHERE account=? AND chat=? AND id=?", (store.account, file["chat"], file["id"]))[0]["data"])
                content = {"path": target.relative_to(store.chat_folder(file["chat"])).as_posix(), "sha256": sha, "size": size, "observed_at": stamp()}
                contents = data.get("contents", [])
                if not any(c["sha256"] == sha for c in contents):
                    contents.append(content)
                data.update(contents=contents, path=content["path"], sha256=sha, size=size)
                store.file_update(file["chat"], file["id"], data=canonical(data), state="saved", error="")
                store.activity("file",file["chat"],"done",file_id=file["id"],filename=file["name"],downloaded_bytes=size,total_bytes=size)
                await durable_io(store.flush, file["chat"])
                store.set_state("last_progress", {"at": time.time(), "chat": file["chat"], "kind": "file"})
        finally:
            temp.unlink(missing_ok=True)

    async def file_step(self):
        if not self.service.connected():
            return
        async with self.file_lock:
            store = self.store()
            if store.state("cleanup_pending", False):
                return
            s = self.service.settings
            rows = self.service.db.rows("SELECT * FROM ca_files WHERE account=? AND state='queued' AND next_at<=? ORDER BY automatic,id", (store.account, time.time()))
            for row in rows:
                file = {**json.loads(row["data"]), **{k: v for k, v in row.items() if k != "data"}}
                if file["automatic"]:
                    if store.state("collection_policy", 0) < COLLECTION_POLICY and file.get("download_origin") != "history_opt_in":
                        continue  # Wait for the message loop's durable upgrade.
                    if not self.automatic_file_allowed(store, file["chat"], file["id"], file.get("download_origin", "automatic"), file.get("collection_kind", "")):
                        store.file_update(file["chat"], file["id"], state="not_saved", automatic=0, error="", next_at=0)
                        continue
                    manual_collection = file.get("download_origin") == "manual_collection"
                    independent = file.get("download_origin") in {"manual_collection", "history_opt_in"}
                    if (not independent and not s.chat_auto_save) or (not manual_collection and not self.selected(file["chat"])) or not getattr(s, "chat_download_" + file["category"]):
                        store.file_update(file["chat"], file["id"], state="not_saved")
                        continue
                    if s.paused or not window_status(s.chat_attachment_schedule)["allowed"]:
                        continue
                try:
                    async with self.service.auth_lock:
                        # Capture identity and resolve the one-time link while auth is
                        # stable. Streaming uses no bearer token and holds no sync lock.
                        if store.account != self.store().account:
                            return
                        fresh = self.service.db.rows("SELECT * FROM ca_files WHERE account=? AND chat=? AND id=?", (store.account, file["chat"], file["id"]))
                        if not fresh or fresh[0]["state"] != "queued":
                            continue
                        row = fresh[0]
                        file = {**json.loads(row["data"]), **{k: v for k, v in row.items() if k != "data"}}
                        client = copy.copy(self.service.client)
                        client.settings = replace(self.service.settings)
                        file["maximum"] = s.chat_max_file_mb * 1024**2
                        result = await client.call("im.v2.File.download", {"dialogId": store.chat(file["chat"])["dialog"], "fileId": file["id"]}, v3=False)
                        file["download_url"] = result.get("downloadUrl") or ""
                        if not file["download_url"]:
                            raise ValueError("Источник не предоставил ссылку скачивания")
                        store.file_update(file["chat"], file["id"], state="running", attempts=file["attempts"] + 1)
                    await self.download_file(store, file, client)
                except (BitrixError, httpx.HTTPError, ValueError, OSError) as exc:
                    permanent = isinstance(exc, BitrixError) and exc.code in ACCESS_CODES or isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {403, 404}
                    limited = isinstance(exc, ValueError) and "лимит автоматической загрузки" in str(exc)
                    store.file_update(file["chat"], file["id"], state="unavailable" if permanent else "size_limited" if limited else "error", error=self.service.vault.redact(str(exc)), next_at=max(time.time() + 30, getattr(exc, "retry_at", 0)))
                    if not permanent and not isinstance(exc, ValueError):
                        store.file_update(file["chat"], file["id"], state="queued")
                break

    async def file_loop(self):
        self.file_loop_running = True
        try:
            while self.service.alive:
                try:
                    await self.file_step()
                except Exception as exc:
                    self.error = self.service.vault.redact(str(exc))
                await asyncio.sleep(1)
        finally:
            self.file_loop_running = False
