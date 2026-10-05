"""Supplement Follow-up with structured call IDs; never persist chat messages."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

import httpx

from .archive import clean_metadata
from .bitrix import BitrixError


def positive_id(value):
    if isinstance(value, bool):
        return 0
    try:
        return int(value) if str(value).isdigit() and int(value) > 0 else 0
    except (ValueError, TypeError):
        return 0


def structured_calls(messages):
    calls = set()
    for message in messages:
        params = message.get("params") or {}
        component = params.get("COMPONENT_PARAMS") if isinstance(params, dict) else None
        # User text and attachments are not discovery evidence.
        if not isinstance(component, dict) or not (
            message.get("author_id") == 0 or params.get("COMPONENT_ID") == "CallMessage"
        ):
            continue
        call_id = positive_id(component.get("CALL_ID"))
        if call_id:
            calls.add(call_id)
    return sorted(calls)


async def discover(service, start, end):
    db, client = service.db, service.client
    portal, user_id = service.settings.portal, service.settings.user_id
    prefix = f"call_discovery:{portal}:{user_id}:"
    if float(db.get_state(prefix + "retry_at", "0")) > time.time():
        return
    dialogs = json.loads(db.get_state(prefix + "dialogs", "{}"))
    known = [json.loads(row["metadata"]) for row in db.rows(
        "SELECT metadata FROM meetings WHERE portal=? AND source='bitrix'", (service.settings.portal,))]
    peers = service.personal_chat_peers(known)
    for item in known:
        chat_id = positive_id(item.get("chatId"))
        if chat_id:
            dialogs.setdefault(str(chat_id), str(peers.get(chat_id) or f"chat{chat_id}"))
    try:
        # Always check the first recent page, then continue older pages on later scans.
        offset = int(db.get_state(prefix + "recent_offset", "0"))
        for current in dict.fromkeys((0, offset)):
            result = await client.recent_dialogs(current)
            if (service.settings.portal, service.settings.user_id) != (portal, user_id):
                return
            for item in result["items"]:
                chat_id = positive_id(item.get("chat_id"))
                peer = positive_id(item.get("id"))
                if chat_id and item.get("type") in {"user", "chat"}:
                    dialogs[str(chat_id)] = str(peer) if item["type"] == "user" and peer else f"chat{chat_id}"
            more = result.get("hasMore") or result.get("hasMorePages")
            db.set_state(prefix + "recent_offset", str(current + len(result["items"]) if more and result["items"] else 0))
            await asyncio.sleep(.2)
    except (BitrixError, httpx.HTTPError, ValueError, OSError) as exc:
        db.set_state(prefix + "error", service.vault.redact(str(exc)))
        if not dialogs:
            db.set_state(prefix + "retry_at", str(time.time() + 300))
            return
    db.set_state(prefix + "dialogs", json.dumps(dialogs, sort_keys=True))
    lower, upper = datetime.fromisoformat(start.replace("Z", "+00:00")), datetime.fromisoformat(end.replace("Z", "+00:00"))
    # Least recently visited chats first: a busy chat cannot starve another one.
    chats = sorted(dialogs, key=lambda chat: float(db.get_state(prefix + chat + ":at", "0")))[:20]
    for chat in chats:
        key = prefix + chat + ":cursor"
        state = json.loads(db.get_state(key, "{}"))
        try:
            # Latest page catches new calls independently of an older backfill.
            modes = ["latest"]
            for mode in modes:
                before = state.get("catchup", 0) if mode == "catchup" else state.get("before", 0) if mode == "history" else 0
                messages = await client.dialog_messages(dialogs[chat], int(chat), before)
                if (service.settings.portal, service.settings.user_id) != (portal, user_id):
                    return
                ids = [positive_id(message.get("id")) for message in messages]
                ids = [value for value in ids if value]
                if ids and before and min(ids) >= before:
                    raise BitrixError("Bitrix24 вернул повторяющийся курсор сообщений")
                items = []
                pending = state.setdefault("pending", {})
                candidates = set(structured_calls(messages))
                candidates.update(int(call_id) for call_id, retry_at in pending.items() if retry_at <= time.time())
                for call_id in sorted(candidates):
                    if pending.get(str(call_id), 0) > time.time():
                        continue
                    if db.rows("SELECT id FROM meetings WHERE portal=? AND call_id=?",
                               (service.settings.portal, str(call_id))):
                        pending.pop(str(call_id), None)
                        continue
                    try:
                        item = await client.followup_metadata(str(call_id))
                        if (service.settings.portal, service.settings.user_id) != (portal, user_id):
                            return
                        if str(item.get("callId")) != str(call_id) or str(item.get("chatId")) != chat or not item.get("uuid"):
                            raise BitrixError("Ответ Bitrix24 относится к другому звонку или чату")
                        if any(positive_id(p.get("userId")) == service.settings.user_id for p in item.get("participants", [])):
                            date = datetime.fromisoformat(str(item.get("startDate", "")).replace("Z", "+00:00"))
                            if lower <= date <= upper:
                                items.append(clean_metadata(item))
                        pending.pop(str(call_id), None)
                    except (BitrixError, httpx.HTTPError, ValueError, OSError) as exc:
                        # An unfinished/unavailable call must not hide later calls in this page.
                        pending[str(call_id)] = time.time() + 300
                        db.set_state(prefix + chat + ":error", service.vault.redact(str(exc)))
                    await asyncio.sleep(.2)
                if items:
                    yield items
                # Commit only after the consumer has stored this page successfully.
                oldest, newest = (min(ids), max(ids)) if ids else (0, 0)
                dates = [datetime.fromisoformat(message["date"].replace("Z", "+00:00")) for message in messages if message.get("date")]
                exhausted = not ids or (dates and min(dates) < lower)
                if mode == "latest":
                    if state.get("catchup"):
                        state["pending_head"] = max(state.get("pending_head", 0), newest)
                    if not state.get("head"):
                        state.update(head=newest, before=0 if exhausted else oldest)
                    elif not exhausted and oldest > state["head"]:
                        state.setdefault("catchup", oldest)
                        state["pending_head"] = max(state.get("pending_head", 0), newest)
                    elif not state.get("catchup"):
                        state["head"] = max(state["head"], newest)
                    if state.get("catchup"):
                        modes.append("catchup")
                    if state.get("before"):
                        modes.append("history")
                elif mode == "catchup":
                    if exhausted or oldest <= state["head"]:
                        state["head"] = max(state["head"], state.pop("pending_head", 0))
                        state.pop("catchup", None)
                    else:
                        state["catchup"] = oldest
                else:
                    state["before"] = 0 if exhausted else oldest
                db.set_state(key, json.dumps(state))
                await asyncio.sleep(.2)
            if not state.get("pending"):
                db.set_state(prefix + chat + ":error", "")
        except (BitrixError, httpx.HTTPError, ValueError, OSError) as exc:
            db.set_state(prefix + chat + ":error", service.vault.redact(str(exc)))
            if isinstance(exc, BitrixError) and "INSUFFICIENTSCOPE" in exc.code.replace("_", ""):
                db.set_state(prefix + "retry_at", str(time.time() + 300))
                return
        finally:
            db.set_state(prefix + chat + ":at", str(time.time()))
