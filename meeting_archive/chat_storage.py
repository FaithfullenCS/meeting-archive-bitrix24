"""Account-scoped SQLite index and recoverable, portable chat projections."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from .chat_model import canonical, file_record, fingerprint, message_hash, normalize, positive, sequence, stamp, text_html, text_markdown
from .settings import atomic_json


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class ChatStore:
    def __init__(self, db, root, portal, user_id):
        self.db, self.root, self.portal, self.user_id = db, Path(root).resolve(), portal, int(user_id)
        self.account = fingerprint([str(self.root).casefold(), portal, self.user_id])
        self.folder = self.root / portal / f"user-{self.user_id}"

    @staticmethod
    def initialize(db):
        with db.lock:
            db.connection.create_function("ca_casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
            db.connection.executescript("""
                CREATE TABLE IF NOT EXISTS ca_chats(account TEXT, id INTEGER, dialog TEXT, data TEXT,
                    dirty INTEGER DEFAULT 1, PRIMARY KEY(account,id));
                CREATE TABLE IF NOT EXISTS ca_messages(account TEXT, chat INTEGER, id INTEGER, date TEXT,
                    author INTEGER, text TEXT, data TEXT, hash TEXT, observed TEXT,
                    PRIMARY KEY(account,chat,id));
                CREATE INDEX IF NOT EXISTS ca_message_dates ON ca_messages(account,chat,date,id);
                CREATE TABLE IF NOT EXISTS ca_versions(account TEXT, chat INTEGER, id INTEGER, hash TEXT,
                    data TEXT, observed TEXT, PRIMARY KEY(account,chat,id,hash));
                CREATE TABLE IF NOT EXISTS ca_context(account TEXT, chat INTEGER, source_chat INTEGER,
                    id INTEGER, hash TEXT, data TEXT, PRIMARY KEY(account,chat,source_chat,id,hash));
                CREATE TABLE IF NOT EXISTS ca_files(account TEXT, chat INTEGER, id INTEGER, data TEXT,
                    state TEXT DEFAULT 'not_saved', automatic INTEGER DEFAULT 0, next_at REAL DEFAULT 0,
                    attempts INTEGER DEFAULT 0, error TEXT DEFAULT '', PRIMARY KEY(account,chat,id));
                CREATE INDEX IF NOT EXISTS ca_file_queue ON ca_files(account,state,next_at);
                CREATE TABLE IF NOT EXISTS ca_work(account TEXT, chat INTEGER, kind TEXT, data TEXT,
                    priority INTEGER, next_at REAL DEFAULT 0, touched REAL DEFAULT 0,
                    PRIMARY KEY(account,chat,kind));
                CREATE TABLE IF NOT EXISTS ca_events(account TEXT, id INTEGER, data TEXT,
                    PRIMARY KEY(account,id));
                CREATE TABLE IF NOT EXISTS ca_dirty_months(account TEXT, chat INTEGER, month TEXT,
                    PRIMARY KEY(account,chat,month));
                CREATE TABLE IF NOT EXISTS ca_activity(account TEXT,id TEXT,data TEXT,touched REAL,
                    PRIMARY KEY(account,id));
                UPDATE ca_files SET state='queued' WHERE state='running';
            """)
            db.connection.commit()

    def key(self, suffix):
        return f"chat_archive:{self.account}:{suffix}"

    def state(self, suffix, default=None):
        raw = self.db.get_state(self.key(suffix))
        return json.loads(raw) if raw else default

    def set_state(self, suffix, data):
        self.db.set_state(self.key(suffix), canonical(data))

    def chat_folder(self, chat):
        id = int(chat)
        # Keep existing folders and notebook links stable. New archives separate
        # task chats physically; collections also index every legacy folder.
        candidates = [self.folder / "chats" / f"chat-{id}", self.folder / "chats" / "tasks" / f"chat-{id}", self.folder / "chats" / "conversations" / f"chat-{id}"]
        for path in candidates:
            if path.exists():
                return path
        rows = self.db.rows("SELECT data FROM ca_chats WHERE account=? AND id=?", (self.account,id))
        group = json.loads(rows[0]["data"]).get("group", "conversations") if rows else "conversations"
        return self.folder / "chats" / ("tasks" if group == "tasks" else "conversations") / f"chat-{id}"

    def chats(self):
        rows = self.db.rows("""SELECT c.*,COALESCE(m.n,0) AS message_count,m.first,m.last,COALESCE(w.n,0) AS pending
            FROM ca_chats c LEFT JOIN (SELECT chat,count(*) AS n,min(date) AS first,max(date) AS last
            FROM ca_messages WHERE account=? GROUP BY chat) m ON m.chat=c.id
            LEFT JOIN (SELECT chat,count(*) AS n FROM ca_work WHERE account=? GROUP BY chat) w ON w.chat=c.id
            WHERE c.account=?""", (self.account, self.account, self.account))
        for row in rows:
            data = json.loads(row.pop("data"))
            row.update(data)
            row["count"] = {"n": row.pop("message_count"), "first": row.pop("first"), "last": row.pop("last")}
        return rows

    def summary(self):
        """Cheap account-wide counters, independent of the current view filters."""
        counts = self.db.rows("SELECT count(*) AS count FROM ca_chats WHERE account=?", (self.account,))[0]
        counts.update(self.db.rows("SELECT count(DISTINCT chat) AS archived,count(*) AS messages FROM ca_messages WHERE account=?", (self.account,))[0])
        counts["pending"] = self.db.rows("SELECT count(*) AS n FROM ca_work WHERE account=?", (self.account,))[0]["n"] + self.db.rows("SELECT count(*) AS n FROM ca_files WHERE account=? AND state IN ('queued','running')", (self.account,))[0]["n"]
        return counts

    def activity(self, kind, chat=0, state="done", **values):
        id = fingerprint([self.account,kind,chat,values.get("file_id",0)])[:24]
        data = {"id":id,"kind":kind,"chat":chat,"state":state,"touched":time.time(),**values}
        self.db.execute("INSERT OR REPLACE INTO ca_activity VALUES(?,?,?,?)", (self.account,id,canonical(data),data["touched"]))
        self.db.execute("DELETE FROM ca_activity WHERE account=? AND id NOT IN (SELECT id FROM ca_activity WHERE account=? ORDER BY touched DESC LIMIT 300)", (self.account,self.account))
        return data

    def collections(self):
        groups = {"tasks":[], "conversations":[]}
        for row in self.db.rows("SELECT id,data FROM ca_chats WHERE account=? ORDER BY id", (self.account,)):
            data=json.loads(row["data"])
            group="tasks" if data.get("group")=="tasks" else "conversations"
            relative = data.get("storage_path")
            if relative not in {f"chats/chat-{row['id']}",f"chats/tasks/chat-{row['id']}",f"chats/conversations/chat-{row['id']}"}:
                relative=f"chats/{group}/chat-{row['id']}"
            groups[group].append({"id":row["id"],"title":data["title"],"task_id":data.get("task_id",0),"path":relative+"/chat.json"})
        for group, entries in groups.items():
            target=self.folder / "collections" / (group+".json")
            payload=canonical({"schemaVersion":1,"group":group,"chats":entries})
            if not target.exists() or target.read_text("utf-8")!=payload:
                atomic_text(target,payload)

    def chat(self, id):
        rows = self.db.rows("SELECT * FROM ca_chats WHERE account=? AND id=?", (self.account, int(id)))
        if not rows:
            raise ValueError("Чат не сохранён для текущего аккаунта и папки")
        row = rows[0]
        row.update(json.loads(row.pop("data")))
        return row

    def upsert_chat(self, id, dialog, **values):
        id = positive(id)
        if not id:
            raise ValueError("Не указан устойчивый ID чата")
        old = self.db.rows("SELECT data FROM ca_chats WHERE account=? AND id=?", (self.account, id))
        data = json.loads(old[0]["data"]) if old else {
            "title": f"Чат {id}", "type": "chat", "group": "conversations", "participants": [], "coverage": "pending",
            "discovery": "recent_and_known_only", "last_checked": "", "limitations": [],
            "history_since": "", "history_complete": False, "meeting_ids": [], "months": []}
        data.update(values)
        if not data.get("storage_path"):
            legacy = self.folder / "chats" / f"chat-{id}"
            data["storage_path"] = f"chats/chat-{id}" if legacy.exists() else f"chats/{'tasks' if data.get('group')=='tasks' else 'conversations'}/chat-{id}"
        self.db.execute("INSERT INTO ca_chats(account,id,dialog,data) VALUES(?,?,?,?) ON CONFLICT(account,id) DO UPDATE SET dialog=excluded.dialog,data=excluded.data,dirty=1",
                        (self.account, id, str(dialog), canonical(data)))

    def update_chat(self, id, **values):
        self.chat(id)
        data = json.loads(self.db.rows("SELECT data FROM ca_chats WHERE account=? AND id=?", (self.account, id))[0]["data"])
        data.update(values)
        self.db.execute("UPDATE ca_chats SET data=?,dirty=1 WHERE account=? AND id=?", (canonical(data), self.account, id))

    def enqueue(self, chat, kind, data=None, priority=10):
        self.chat(chat)
        self.db.execute("INSERT OR IGNORE INTO ca_work(account,chat,kind,data,priority,touched) VALUES(?,?,?,?,?,?)",
                        (self.account, chat, kind, canonical(data or {"cursor": 0}), priority, time.time()))

    def save_page(self, chat_id, page):
        """Durable index first. Projection must succeed before caller advances a cursor."""
        users = {positive(u.get("id")): str(u.get("name") or " ".join(filter(None, [u.get("firstName"), u.get("lastName")])))
                 for u in sequence(page.get("users")) if isinstance(u, dict)}
        reactions = {positive(r.get("messageId")): r for r in sequence(page.get("reactions")) if isinstance(r, dict)}
        extra = sequence(page.get("additionalMessages"))
        snapshots = {}
        for raw in extra:
            if not isinstance(raw, dict):
                continue
            source_chat = positive(raw.get("chatId") or raw.get("chat_id")) or chat_id
            message = normalize(raw, source_chat, users, reactions.get(positive(raw.get("id"))))
            snapshots[(source_chat, message["id"])] = message
        messages = []
        for raw in page["messages"]:
            message = normalize(raw, chat_id, users, reactions.get(positive(raw.get("id"))))
            previous = self.db.rows("SELECT data FROM ca_messages WHERE account=? AND chat=? AND id=?", (self.account, chat_id, message["id"]))
            prior_links = json.loads(previous[0]["data"])["relations"] if previous else []
            for link in message["relations"]:
                prior = next((old for old in prior_links if (old["kind"], old["chat_id"], old["message_id"]) == (link["kind"], link["chat_id"], link["message_id"])), None)
                if prior and not link["excerpt"]:
                    link["excerpt"] = prior["excerpt"]
                source = snapshots.get((link["chat_id"], link["message_id"]))
                if source:
                    link.update(author_id=source["author_id"], author=source["author"], date=source["date"])
                    if not link["excerpt"]:
                        link["excerpt"] = source["text"]
            messages.append(message)
        observed = stamp()
        with self.db.lock:
            con = self.db.connection
            try:
                con.execute("BEGIN")
                for message in messages:
                    old = con.execute("SELECT data,hash,observed FROM ca_messages WHERE account=? AND chat=? AND id=?",
                                      (self.account, chat_id, message["id"])).fetchone()
                    digest = message_hash(message)
                    if old and old["hash"] != digest:
                        con.execute("INSERT OR IGNORE INTO ca_versions VALUES(?,?,?,?,?,?)",
                                    (self.account, chat_id, message["id"], old["hash"], old["data"], old["observed"]))
                    if old and old["hash"] == digest:
                        observed_at = old["observed"]
                    else:
                        observed_at = observed
                    message.update(hash=digest, observed_at=observed_at)
                    con.execute("INSERT OR REPLACE INTO ca_messages VALUES(?,?,?,?,?,?,?,?,?)",
                                (self.account, chat_id, message["id"], message["date"], message["author_id"],
                                 message["text"], canonical(message), digest, observed_at))
                    con.execute("INSERT OR IGNORE INTO ca_dirty_months VALUES(?,?,?)",
                                (self.account, chat_id, message["date"][:7] or "undated"))
                    con.execute("INSERT OR IGNORE INTO ca_dirty_months SELECT account,chat,COALESCE(NULLIF(substr(date,1,7),''),'undated') FROM ca_messages WHERE account=? AND chat=? AND EXISTS(SELECT 1 FROM json_each(json_extract(ca_messages.data,'$.relations')) r WHERE json_extract(r.value,'$.chat_id')=? AND json_extract(r.value,'$.message_id')=?)",
                                (self.account, chat_id, chat_id, message["id"]))
                for (source_chat, id), message in snapshots.items():
                    digest = message_hash(message)
                    message.update(hash=digest, observed_at=observed, context_only=True)
                    con.execute("INSERT OR IGNORE INTO ca_context VALUES(?,?,?,?,?,?)",
                                (self.account, chat_id, source_chat, id, digest, canonical(message)))
                for raw in sequence(page.get("files")):
                    if not isinstance(raw, dict):
                        continue
                    file = file_record(raw)
                    if not file["id"]:
                        continue
                    old = con.execute("SELECT data FROM ca_files WHERE account=? AND chat=? AND id=?",
                                      (self.account, chat_id, file["id"])).fetchone()
                    changed = False
                    if old:
                        previous = json.loads(old["data"])
                        changed = file["source"] != previous.get("source", {})
                        file.update({key: previous[key] for key in ("path", "sha256", "contents", "download_origin", "collection_kind", "locally_deleted", "auto_suppressed") if key in previous})
                    con.execute("INSERT INTO ca_files(account,chat,id,data) VALUES(?,?,?,?) ON CONFLICT(account,chat,id) DO UPDATE SET data=excluded.data",
                                (self.account, chat_id, file["id"], canonical(file)))
                    if changed:
                        con.execute("UPDATE ca_files SET state='not_saved',error='Источник сообщил изменение файла; сохранённая копия остаётся в архиве' WHERE account=? AND chat=? AND id=? AND state='saved'", (self.account, chat_id, file["id"]))
                con.execute("UPDATE ca_chats SET dirty=1 WHERE account=? AND id=?", (self.account, chat_id))
                con.commit()
            except BaseException:
                con.rollback()
                raise
        self.flush(chat_id)
        return messages

    def files(self, chat):
        return [{**json.loads(row["data"]), **{k: v for k, v in row.items() if k != "data"}} for row in
                self.db.rows("SELECT * FROM ca_files WHERE account=? AND chat=?", (self.account, chat))]

    def file(self, chat, id):
        self.chat(chat)
        matches = self.db.rows("SELECT * FROM ca_files WHERE account=? AND chat=? AND id=?", (self.account, chat, int(id)))
        if not matches:
            raise ValueError("Вложение не найдено")
        row = matches[0]
        return {**json.loads(row["data"]), **{k: v for k, v in row.items() if k != "data"}}

    def file_update(self, chat, id, **values):
        self.db.execute(f"UPDATE ca_files SET {','.join(k+'=?' for k in values)} WHERE account=? AND chat=? AND id=?",
                        (*values.values(), self.account, chat, id))
        self.db.execute("UPDATE ca_chats SET dirty=1 WHERE account=? AND id=?", (self.account, chat))
        self.db.execute("INSERT OR IGNORE INTO ca_dirty_months SELECT account,chat,COALESCE(NULLIF(substr(date,1,7),''),'undated') FROM ca_messages WHERE account=? AND chat=? AND EXISTS(SELECT 1 FROM json_each(json_extract(ca_messages.data,'$.file_ids')) WHERE value=?)", (self.account, chat, id))

    def enrich(self, chat, message):
        ids = message["file_ids"]
        rows = self.db.rows("SELECT * FROM ca_files WHERE account=? AND chat=? AND id IN (" + ",".join("?" for _ in ids) + ")", (self.account, chat, *ids)) if ids else []
        files = {r["id"]: {**json.loads(r["data"]), **{k: v for k, v in r.items() if k != "data"}} for r in rows}
        item = dict(message)
        item["files"] = [files.get(id, {"id": id, "name": f"Файл {id}", "category": "other", "state": "unavailable",
                                             "error": "Источник не предоставил сведения о файле"}) for id in item["file_ids"]]
        item["version_count"] = self.db.rows("SELECT count(*) AS n FROM ca_versions WHERE account=? AND chat=? AND id=?",
                                             (self.account, chat, item["id"]))[0]["n"]
        return item

    def message(self, chat, id):
        self.chat(chat)
        rows = self.db.rows("SELECT data FROM ca_messages WHERE account=? AND chat=? AND id=?", (self.account, chat, int(id)))
        return self.enrich(chat, json.loads(rows[0]["data"])) if rows else None

    def versions(self, chat, id):
        self.chat(chat)
        return [json.loads(r["data"]) for r in self.db.rows("SELECT data FROM ca_versions WHERE account=? AND chat=? AND id=? ORDER BY observed",
                                                          (self.account, chat, int(id)))]

    def context(self, chat, source_chat, id):
        self.chat(chat)
        rows = self.db.rows("SELECT data FROM ca_context WHERE account=? AND chat=? AND source_chat=? AND id=?",
                            (self.account, chat, source_chat, id))
        return [json.loads(row["data"]) for row in rows]

    def query(self, *, chat=0, q="", date_from="", date_to="", author=0, direction="", kind="", attachment="",
              file_state="", system="", type="", participant=0, coverage="", offset=0, limit=50):
        conditions, args = ["account=?"], [self.account]
        for column, value in (("chat", positive(chat)),):
            if value:
                conditions.append(column + "=?")
                args.append(value)
        authors = {positive(i) for i in str(author).split(",")} - {0}
        if authors:
            conditions.append("author IN (" + ",".join("?" for _ in authors) + ")")
            args.extend(sorted(authors))
        if q:
            conditions.append("instr(ca_casefold(text),ca_casefold(?))>0")
            args.append(q)
        if date_from:
            conditions.append("substr(date,1,10)>=?")
            args.append(date_from)
        if date_to:
            conditions.append("substr(date,1,10)<=?")
            args.append(date_to)
        if direction in {"incoming", "outgoing"}:
            conditions.append("author" + ("=" if direction == "outgoing" else "!=") + "?")
            args.append(self.user_id)
        if type or participant or coverage:
            participants = {positive(i) for i in str(participant).split(",")} - {0}
            allowed = [c["id"] for c in self.chats() if (not type or (c.get("group","conversations")==type if type in {"tasks","conversations"} else c["type"]==type and (type!="chat" or c.get("group")!="tasks"))) and
                       (not participants or participants.issubset({p["id"] for p in c.get("participants", [])})) and
                       (not coverage or c["coverage"] == coverage)]
            if not allowed:
                return {"items": [], "total": 0, "offset": 0, "limit": limit}
            conditions.append("chat IN (" + ",".join("?" for _ in allowed) + ")")
            args += allowed
        if system in {"hide", "only"}:
            conditions.append("json_extract(data,'$.system')=?")
            args.append(int(system == "only"))
        if kind in {"reply", "forward"}:
            conditions.append("EXISTS(SELECT 1 FROM json_each(json_extract(ca_messages.data,'$.relations')) WHERE json_extract(value,'$.kind')=?)")
            args.append(kind)
        if kind in {"quote", "deleted"}:
            conditions.append(f"json_extract(data,'$.{kind}')=1")
        if kind == "edited":
            conditions.append("EXISTS(SELECT 1 FROM ca_versions v WHERE v.account=ca_messages.account AND v.chat=ca_messages.chat AND v.id=ca_messages.id)")
        if attachment or file_state:
            extra = ""
            if attachment and attachment != "any":
                extra += " AND json_extract(f.data,'$.category')=?"
                args.append(attachment)
            if file_state:
                extra += " AND f.state=?"
                args.append(file_state)
            conditions.append("EXISTS(SELECT 1 FROM json_each(json_extract(ca_messages.data,'$.file_ids')) ids JOIN ca_files f ON f.account=ca_messages.account AND f.chat=ca_messages.chat AND f.id=ids.value WHERE 1=1" + extra + ")")
        offset, limit = max(0, int(offset)), max(1, min(200, int(limit)))
        where = " AND ".join(conditions)
        total = self.db.rows("SELECT count(*) AS n FROM ca_messages WHERE " + where, args)[0]["n"]
        rows = self.db.rows("SELECT data,chat FROM ca_messages WHERE " + where + " ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
        result = []
        for row in rows:
            item = json.loads(row["data"])
            enriched = self.enrich(row["chat"], item)
            enriched["html"] = text_html(item["text"])
            result.append(enriched)
        return {"items": result, "total": total, "offset": offset, "limit": limit}

    def flush(self, chat):
        folder = self.chat_folder(chat)
        if (folder / ".delete-pending.json").exists():
            raise ValueError("Удаление материалов ещё не завершено; запись архива временно остановлена")
        months = {}
        dirty = [r["month"] for r in self.db.rows("SELECT month FROM ca_dirty_months WHERE account=? AND chat=?", (self.account, chat))]
        manifest_path = folder / "chat.json"
        previous = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else {}
        if not previous:
            dirty = [r["month"] for r in self.db.rows("SELECT DISTINCT COALESCE(NULLIF(substr(date,1,7),''),'undated') AS month FROM ca_messages WHERE account=? AND chat=?", (self.account, chat))]
        files = {f["id"]: f for f in self.files(chat)}
        for month in dirty:
            rows = self.db.rows("SELECT data FROM ca_messages WHERE account=? AND chat=? AND COALESCE(NULLIF(substr(date,1,7),''),'undated')=? ORDER BY date,id", (self.account, chat, month))
            months[month] = [json.loads(row["data"]) for row in rows]
        reading = {r["month"]: r for r in previous.get("reading", [])}
        for month, messages in months.items():
            jsonl = "".join(canonical(m) + "\n" for m in messages)
            atomic_text(folder / "messages" / f"{month}.jsonl", jsonl)
            md = f"# {self.chat(chat)['title']} — {month}\n\n"
            for m in messages:
                md += f'<a id="message-{m["id"]}"></a>\n\n## {m["author"] or "Автор ID " + str(m["author_id"])} · {m["date"] or "Дата не предоставлена"}\n\n'
                md += f'ID: {m["id"]} · SHA-256: {m["hash"]} · Получено: {m["observed_at"]}\n\n'
                if m["deleted"]:
                    md += "**Удаление подтверждено событием источника. Ниже сохранённый текст.**\n\n"
                for link in m["relations"]:
                    md += f'**{"Пересылка" if link["kind"] == "forward" else "Ответ" if link["kind"] == "reply" else "Цитата"}**: чат {link["chat_id"]}, сообщение {link["message_id"]}, автор {link.get("author") or link["author_id"] or "не предоставлен"}.\n\n'
                    if link["excerpt"]:
                        md += "\n".join("> " + s for s in text_markdown(link["excerpt"]).splitlines()) + "\n\n"
                    if link["chat_id"] == chat:
                        target = self.message(chat, link["message_id"])
                        if target:
                            md += f'[К исходному]({target["date"][:7] or "undated"}.md#message-{target["id"]})\n\n'
                        else:
                            md += "Оригинал недоступен; полученный фрагмент сохранён.\n\n"
                md += text_markdown(m["text"]) + "\n\n"
                if m["reactions"]:
                    md += "Реакции: " + canonical(m["reactions"]) + "\n\n"
                for id in m["file_ids"]:
                    file = files.get(id, {"name": f"Файл {id}", "state": "unavailable"})
                    if file.get("path"):
                        md += f'[Вложение ID {id}](../{file["path"].replace(" ", "%20")}) · {file["name"]}\n\n'
                    else:
                        md += f'Вложение ID {id}: {file["name"]} · {file["state"]}\n\n'
            atomic_text(folder / "messages" / f"{month}.md", md)
            reading[month] = {"month": month, "markdown": f"messages/{month}.md", "jsonl": f"messages/{month}.jsonl",
                             "sha256": hashlib.sha256(jsonl.encode()).hexdigest()}
        versions = self.db.rows("SELECT data,observed FROM ca_versions WHERE account=? AND chat=? ORDER BY observed,id", (self.account, chat))
        grouped = {}
        for row in versions:
            grouped.setdefault(json.loads(row["data"])["date"][:7] or "undated", []).append(json.loads(row["data"]))
        for month, entries in grouped.items():
            atomic_text(folder / "versions" / f"{month}.jsonl", "".join(canonical(m) + "\n" for m in entries))
        contexts = self.db.rows("SELECT data FROM ca_context WHERE account=? AND chat=?", (self.account, chat))
        atomic_text(folder / "context/excerpts.jsonl", "".join(row["data"] + "\n" for row in contexts))
        self.update_chat(chat, storage_path=folder.relative_to(self.folder).as_posix())
        data = self.chat(chat)
        count = self.db.rows("SELECT count(*) AS messages,min(date) AS first,max(date) AS last FROM ca_messages WHERE account=? AND chat=?", (self.account, chat))[0]
        work = self.db.rows("SELECT kind,data,priority FROM ca_work WHERE account=? AND chat=?", (self.account, chat))
        atomic_json(folder / "chat.json", {"schemaVersion": 1, "portal": self.portal, "user_id": self.user_id,
                    **{k: v for k, v in data.items() if k not in {"account", "dirty"}}, "reading": sorted(reading.values(), key=lambda r: r["month"]),
                    "range": count, "files": [{k: v for k, v in file.items() if k not in {"account", "chat"}} for file in files.values()],
                    "work": [{**r, "data": json.loads(r["data"])} for r in work]})
        (folder / "notes").mkdir(parents=True, exist_ok=True)
        self.folder.mkdir(parents=True, exist_ok=True)
        # Never overwrite an assistant/user notebook.
        (self.folder / "_notebook").mkdir(exist_ok=True)
        atomic_json(self.folder / "account.json", {"schemaVersion": 1, "portal": self.portal, "user_id": self.user_id,
                    "chats": "chats/", "collections":{"tasks":"collections/tasks.json","conversations":"collections/conversations.json"}, "notebook": "_notebook/", "discovery": "recent_and_known_only"})
        self.collections()
        atomic_json(self.root / "archive.json", {"schemaVersion": 1, "kind": "chat-archive", "accounts": "<portal>/user-<ID>/account.json"})
        self.db.execute("UPDATE ca_chats SET dirty=0 WHERE account=? AND id=?", (self.account, chat))
        self.db.execute("DELETE FROM ca_dirty_months WHERE account=? AND chat=?", (self.account, chat))

    def recover(self):
        from .chat_cleanup import recover_pending
        recover_pending(self)
        """Restore a lost index from portable files and replay unfinished projections."""
        if self.folder.exists():
            paths = list((self.folder / "chats").glob("chat-*/chat.json")) + list((self.folder / "chats/tasks").glob("chat-*/chat.json")) + list((self.folder / "chats/conversations").glob("chat-*/chat.json"))
            for path in paths:
                if path.is_symlink() or not path.resolve().is_relative_to((self.folder / "chats").resolve()):
                    continue
                manifest = json.loads(path.read_text("utf-8"))
                if manifest.get("portal") != self.portal or manifest.get("user_id") != self.user_id:
                    continue
                id = positive(manifest.get("id"))
                if path.parent != self.chat_folder(id):
                    raise ValueError("Идентичность папки архива не совпадает с chat.json")
                if self.db.rows("SELECT id FROM ca_chats WHERE account=? AND id=?", (self.account, id)):
                    continue
                # Validate a complete chat before insertion. Incomplete recovery must
                # never project over the portable source or suppress the next attempt.
                messages, versions, contexts, files = [], [], [], []
                declared = set()
                for entry in manifest.get("reading", []):
                    relative = entry.get("jsonl", "")
                    source = (path.parent / relative).resolve()
                    if not source.is_relative_to((path.parent / "messages").resolve()) or not source.is_file() or source.is_symlink():
                        raise ValueError("Не хватает файла месяца архива; восстановление остановлено без изменения источника")
                    if hashlib.sha256(source.read_bytes()).hexdigest() != entry.get("sha256"):
                        raise ValueError("Контрольная сумма месяца архива не совпадает; источник оставлен на месте")
                    declared.add(source)
                actual = {file.resolve() for file in (path.parent / "messages").glob("*.jsonl")}
                if actual != declared:
                    raise ValueError("Состав месяцев не совпадает с chat.json; источник оставлен на месте")
                for file in (path.parent / "messages").glob("*.jsonl"):
                    for line in file.read_text("utf-8").splitlines():
                        m = json.loads(line)
                        if m["chat_id"] != id or message_hash(m) != m["hash"]:
                            raise ValueError("Повреждён JSONL архива; файл оставлен для восстановления")
                        messages.append((self.account, id, m["id"], m["date"], m["author_id"], m["text"], canonical(m), m["hash"], m["observed_at"]))
                if len(messages) != manifest.get("range", {}).get("messages") or len({m[2] for m in messages}) != len(messages):
                    raise ValueError("Количество сообщений не совпадает с chat.json; неполный архив не объявлен восстановленным")
                for file in (path.parent / "versions").glob("*.jsonl"):
                    for line in file.read_text("utf-8").splitlines():
                        m = json.loads(line)
                        if m["chat_id"] != id or message_hash(m) != m["hash"]:
                            raise ValueError("Повреждён файл редакций; источник оставлен на месте")
                        versions.append((self.account, id, m["id"], m["hash"], canonical(m), m["observed_at"]))
                context = path.parent / "context/excerpts.jsonl"
                if context.exists():
                    for line in context.read_text("utf-8").splitlines():
                        m = json.loads(line)
                        contexts.append((self.account, id, m["chat_id"], m["id"], m["hash"], canonical(m)))
                for f in manifest.get("files", []):
                    state = "queued" if f["state"] == "running" else f["state"]
                    if f.get("path"):
                        target = (path.parent / f["path"]).resolve()
                        if not target.is_relative_to(path.parent.resolve()) or not target.is_file():
                            state = "missing"
                    files.append((self.account, id, f["id"], canonical({k: v for k, v in f.items() if k not in {"state", "automatic", "next_at", "attempts", "error"}}), state, int(bool(f.get("automatic"))), f.get("next_at", 0), f.get("attempts", 0), f.get("error", "")))
                data = {k: v for k, v in manifest.items() if k not in {"schemaVersion", "portal", "user_id", "id", "dialog", "files", "range", "reading", "work"}}
                work = [(self.account, id, w["kind"], canonical(w["data"]), w["priority"]) for w in manifest.get("work", [])]
                with self.db.lock:
                    con = self.db.connection
                    try:
                        con.execute("BEGIN")
                        con.executemany("INSERT OR IGNORE INTO ca_messages VALUES(?,?,?,?,?,?,?,?,?)", messages)
                        con.executemany("INSERT OR IGNORE INTO ca_versions VALUES(?,?,?,?,?,?)", versions)
                        con.executemany("INSERT OR IGNORE INTO ca_context VALUES(?,?,?,?,?,?)", contexts)
                        con.executemany("INSERT OR IGNORE INTO ca_files(account,chat,id,data,state,automatic,next_at,attempts,error) VALUES(?,?,?,?,?,?,?,?,?)", files)
                        con.executemany("INSERT OR IGNORE INTO ca_work(account,chat,kind,data,priority) VALUES(?,?,?,?,?)", work)
                        con.execute("INSERT INTO ca_chats(account,id,dialog,data,dirty) VALUES(?,?,?,?,0)", (self.account,id,manifest["dialog"],canonical(data)))
                        con.commit()
                    except BaseException:
                        con.rollback()
                        raise
        for row in self.db.rows("SELECT id FROM ca_chats WHERE account=? AND dirty=1", (self.account,)):
            self.flush(row["id"])
