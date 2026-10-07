"""Account-confined chat material removal with preview and restart recovery."""

from __future__ import annotations

import copy
import hashlib
import json

from .chat_model import canonical, positive, stamp
from .chat_storage import atomic_text
from .cleanup import remove_selection, selection_plan
from .settings import atomic_json

GROUPS = {
    "messages": "Переписка, редакции и контекст",
    "attachments": "Все скачанные вложения",
    "notes": "Заметки и результаты AI",
}
INTENT = ".delete-pending.json"


def ids_for(store, ids):
    ids = [r["id"] for r in store.chats()] if ids is None else ids
    if not isinstance(ids, list) or not ids or len(ids) > 10000 or any(type(i) is not int or i < 1 for i in ids):
        raise ValueError("Выберите чаты для удаления материалов")
    ids = sorted(set(ids))
    for id in ids:
        store.chat(id)
    return ids


def paths_for(store, chat, targets):
    paths = []
    for target in targets:
        if target == "messages":
            paths.extend(["messages", "versions", "context"])
        elif target in {"attachments", "notes"}:
            paths.append(target)
        elif (
            isinstance(target, str)
            and target.startswith("attachments/file-")
            and target.removeprefix("attachments/file-").isdigit()
        ):
            id = positive(target.removeprefix("attachments/file-"))
            store.file(chat, id)
            paths.append(f"attachments/file-{id}")
        else:
            raise ValueError("Выберите материалы из списка чата")
    return paths


def material_plan(engine, ids=None, targets=None, *, choices=False):
    store = engine.store()
    if ids is None and not store.chats():
        return {"account": store.account, "ids": [], "targets": [], "token": "", "plans": [],
                "chats": 0, "files": 0, "bytes": 0, "messages": 0, "choices": []}
    ids = ids_for(store, ids)
    targets = ["messages", "attachments"] if targets is None else targets
    if not isinstance(targets, list) or not targets:
        raise ValueError("Выберите материалы для удаления")
    plans = []
    for id in ids:
        if store.db.rows("SELECT id FROM ca_files WHERE account=? AND chat=? AND state='running'", (store.account, id)):
            raise ValueError("Дождитесь завершения скачивания вложения выбранного чата")
        folder = store.chat_folder(id)
        if (folder / INTENT).exists():
            raise ValueError("Удаление этого чата ещё не завершено. Дождитесь восстановления операции")
        paths = paths_for(store, id, targets)
        plan = selection_plan(folder, paths)
        signature = (
            store.db.rows("SELECT id,hash FROM ca_messages WHERE account=? AND chat=? ORDER BY id", (store.account, id))
            if "messages" in targets
            else []
        )
        files = store.db.rows(
            "SELECT id,data,state FROM ca_files WHERE account=? AND chat=? ORDER BY id", (store.account, id)
        )
        plan["index_token"] = hashlib.sha256(
            canonical([signature, files, store.chat(id).get("message_delete_after", "")]).encode()
        ).hexdigest()
        plans.append({"id": id, **plan})
    token = hashlib.sha256(
        canonical([store.account, targets, [(p["id"], p["token"], p["index_token"]) for p in plans]]).encode()
    ).hexdigest()
    result = {
        "account": store.account,
        "ids": ids,
        "targets": targets,
        "token": token,
        "plans": plans,
        "chats": len(ids),
        "files": sum(p["files"] for p in plans),
        "bytes": sum(p["bytes"] for p in plans),
        "messages": sum(
            store.db.rows("SELECT count(*) AS n FROM ca_messages WHERE account=? AND chat=?", (store.account, id))[0][
                "n"
            ]
            for id in ids
        )
        if "messages" in targets
        else 0,
    }
    if choices:
        values = list(GROUPS)
        if len(ids) == 1:
            values += [f"attachments/file-{f['id']}" for f in store.files(ids[0]) if f.get("path") or f.get("contents")]
        result["choices"] = []
        for target in values:
            part = material_plan(engine, ids, [target])
            if part["files"] or part["messages"]:
                label = (
                    GROUPS.get(target)
                    or "Вложение · " + store.file(ids[0], positive(target.removeprefix("attachments/file-")))["name"]
                )
                result["choices"].append(
                    {
                        "target": target,
                        "label": label,
                        "files": part["files"],
                        "bytes": part["bytes"],
                        "messages": part["messages"],
                    }
                )
    return result


def final_manifest(store, id, targets, at):
    path = store.chat_folder(id) / "chat.json"
    store.flush(id)
    value = json.loads(path.read_text("utf-8"))
    if "messages" in targets:
        value.update(
            message_delete_after=at,
            manual_history_requested=False,
            manual_history_origin="",
            history_complete=False,
            history_paused=True,
            coverage="pending",
            history_since="",
            months=[],
            reading=[],
            range={"messages": 0, "first": None, "last": None},
            work=[],
            error="",
        )
    selected = {positive(t.removeprefix("attachments/file-")) for t in targets if t.startswith("attachments/file-")}
    for file in value.get("files", []):
        if "messages" in targets and file["state"] in {"queued", "error", "size_limited"}:
            file.update(
                state="not_saved",
                automatic=False,
                next_at=0,
                auto_suppressed=True,
                error="Загрузка остановлена после удаления переписки; скачайте вручную",
            )
        if "attachments" in targets or file["id"] in selected:
            for key in ("path", "sha256", "contents", "downloaded_bytes"):
                file.pop(key, None)
            file.update(
                state="not_saved",
                automatic=False,
                next_at=0,
                locally_deleted=True,
                download_origin="",
                error="Удалено с компьютера; для восстановления скачайте вручную",
            )
    return value


def apply_index(store, id, manifest, targets):
    db = store.db
    with db.lock:
        con = db.connection
        try:
            con.execute("BEGIN")
            if "messages" in targets:
                for table in ("ca_messages", "ca_versions", "ca_context", "ca_dirty_months"):
                    con.execute(f"DELETE FROM {table} WHERE account=? AND chat=?", (store.account, id))
                con.execute("DELETE FROM ca_work WHERE account=? AND chat=? AND kind!='metadata'", (store.account, id))
                con.execute(
                    "DELETE FROM ca_activity WHERE account=? AND json_extract(data,'$.chat')=?", (store.account, id)
                )
                con.execute(
                    "DELETE FROM ca_events WHERE account=? AND COALESCE(json_extract(data,'$.data.chat.id'),json_extract(data,'$.data.message.chatId'),json_extract(data,'$.chatId'))=?",
                    (store.account, id),
                )
            for file in manifest.get("files", []):
                if file.get("locally_deleted") or file.get("auto_suppressed"):
                    data = {
                        k: v for k, v in file.items() if k not in {"state", "automatic", "next_at", "attempts", "error"}
                    }
                    con.execute(
                        "UPDATE ca_files SET data=?,state='not_saved',automatic=0,next_at=0,error=? WHERE account=? AND chat=? AND id=?",
                        (canonical(data), file["error"], store.account, id, file["id"]),
                    )
                    con.execute(
                        "DELETE FROM ca_activity WHERE account=? AND json_extract(data,'$.chat')=? AND json_extract(data,'$.file_id')=?",
                        (store.account, id, file["id"]),
                    )
            if con.execute("SELECT 1 FROM ca_chats WHERE account=? AND id=?", (store.account, id)).fetchone():
                data = {
                    k: v
                    for k, v in manifest.items()
                    if k
                    not in {"schemaVersion", "portal", "user_id", "id", "dialog", "files", "range", "reading", "work"}
                }
                con.execute(
                    "UPDATE ca_chats SET data=?,dirty=1 WHERE account=? AND id=?", (canonical(data), store.account, id)
                )
            con.commit()
        except BaseException:
            con.rollback()
            raise


def finish_intent(store, folder, intent):
    manifest = intent.get("manifest", {})
    id = positive(manifest.get("id"))
    if (
        intent.get("schemaVersion") != 1
        or manifest.get("portal") != store.portal
        or manifest.get("user_id") != store.user_id
        or folder != store.chat_folder(id)
    ):
        raise ValueError("Неверная идентичность операции удаления чата")
    targets = intent.get("targets", [])
    # Whitelist paths even when resuming a locally stored operation after exit.
    paths = []
    for target in targets:
        if target == "messages":
            paths.extend(["messages", "versions", "context"])
        elif target in {"attachments", "notes"}:
            paths.append(target)
        elif (
            isinstance(target, str)
            and target.startswith("attachments/file-")
            and target.removeprefix("attachments/file-").isdigit()
        ):
            paths.append("attachments/file-" + str(positive(target.removeprefix("attachments/file-"))))
        else:
            raise ValueError("Неверная сохранённая операция удаления")
    if not paths:
        raise ValueError("Пустая операция удаления")
    plan = selection_plan(folder, paths)
    remove_selection(folder, paths, plan["token"])
    apply_index(store, id, manifest, targets)
    atomic_json(folder / "chat.json", manifest)
    if "messages" in targets:
        events = store.db.rows("SELECT data FROM ca_events WHERE account=? ORDER BY id", (store.account,))
        atomic_text(store.folder / "events.jsonl", "".join(r["data"] + "\n" for r in events))
    (folder / INTENT).unlink()
    if store.db.rows("SELECT id FROM ca_chats WHERE account=? AND id=?", (store.account, id)):
        store.flush(id)


def recover_pending(store):
    paths = set()
    for pattern in ("chats/chat-*/" + INTENT, "chats/tasks/chat-*/" + INTENT, "chats/conversations/chat-*/" + INTENT):
        paths.update(store.folder.glob(pattern))
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(store.folder.resolve()):
            raise ValueError("Небезопасная операция восстановления удаления")
        finish_intent(store, path.parent, json.loads(path.read_text("utf-8")))
    store.set_state("cleanup_pending", False)


def remove_materials(engine, home, ids, targets, token):
    store = engine.store()
    plan = material_plan(engine, ids, targets)
    if token != plan["token"]:
        raise ValueError("Состав материалов изменился. Откройте удаление заново")
    # Invalidate the cached export; separate user-downloaded exports are untouched.
    cached = ["exports/" + store.account + ".zip"]
    cache_plan = selection_plan(home, cached)
    remove_selection(home, cached, cache_plan["token"])
    at = stamp()
    for id in plan["ids"]:
        folder = store.chat_folder(id)
        manifest = final_manifest(store, id, targets, at)
        intent = {"schemaVersion": 1, "targets": targets, "manifest": copy.deepcopy(manifest), "approved_at": at}
        store.set_state("cleanup_pending", True)
        atomic_json(folder / INTENT, intent)
        finish_intent(store, folder, intent)
    store.set_state("cleanup_pending", False)
    return {k: v for k, v in plan.items() if k != "plans"}
