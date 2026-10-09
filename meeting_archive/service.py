from __future__ import annotations

import asyncio
import copy
import ctypes
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from dataclasses import asdict, replace
from pathlib import Path

import httpx

from .archive import Archive, clean_metadata, followup_state
from .bitrix import BitrixClient, BitrixError
from .participants import meeting_participants
from .db import Database
from .modules import ModuleManager
from .settings import Settings, Vault
from .scheduling import window_status

MEDIA_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".mp4", ".mkv", ".webm", ".aac", ".opus", ".mov", ".wma"}
CATALOGUE_POLL_SECONDS = 60
QUEUE_POLL_SECONDS = .5


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def retry_delay(created: float) -> int:
    age = time.time() - created
    return 60 if age < 15 * 60 else 300 if age < 24 * 3600 else 86400


class Service:
    def __init__(self, home: Path, *, vault=None, client=None):
        self.home = home.resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        from .settings import atomic_json
        marker = self.home / ".meeting-archive-profile.json"
        if not marker.exists():
            atomic_json(marker, {"schemaVersion": 1, "product": "MeetingArchive"})
        self.settings = Settings.load(home)
        self.vault = vault or Vault(home)
        self.db = Database(home / "archive.sqlite")
        self.client = client or BitrixClient(self.settings, self.vault)
        self.module = ModuleManager(home, self.vault)
        self.tasks: list[asyncio.Task] = []
        self.scan_lock = asyncio.Lock()
        self.auth_lock = asyncio.Lock()
        self.catalogue = {"running": False, "count": 0, "error": ""}
        self.auth_error = ""
        self.chat_warning = ""
        self.chat_lookup_disabled = False
        self.alive = False
        self.scan_retry_at = 0
        self.active_tasks: dict[int, asyncio.Task] = {}
        self.event_loop: asyncio.AbstractEventLoop | None = None
        self.material_maintenance: set[int] = set()
        self.module_maintenance = False
        from .updates import UpdateManager
        from .notifications import Notifications
        self.updates = UpdateManager(self)
        self.notifications = Notifications(self)
        from .chat_sync import ChatArchive
        self.chat_archive = ChatArchive(self)
        self.recovery_status = {"running": False, "found": 0, "restored": 0, "errors": 0, "issues": []}
        self.recovery_lock = asyncio.Lock()

    async def restore_local_archive(self):
        from .recovery import restore_local
        from .chat_sync import durable_io
        async with self.auth_lock, self.scan_lock, self.chat_archive.lock, self.recovery_lock:
            return await durable_io(restore_local, self)

    @property
    def archive(self):
        return Archive(Path(self.settings.archive_root))

    def connected(self) -> bool:
        return bool(self.settings.portal and self.settings.user_id)

    async def start(self):
        await self.restore_local_archive()
        self.reconcile_short_calls()
        self.alive = True
        self.event_loop = asyncio.get_running_loop()
        self.tasks = [asyncio.create_task(self.job_loop(("fetch", "import"))),
                      asyncio.create_task(self.job_loop(("transcribe", "install"))),
                      asyncio.create_task(self.scheduler()), asyncio.create_task(self.discover_resources()),
                      asyncio.create_task(self.refresh_identity()), asyncio.create_task(self.updates.loop()),
                      asyncio.create_task(self.chat_archive.loop()), asyncio.create_task(self.chat_archive.file_loop())]

    def identity_key(self):
        return f"account_name:{self.settings.portal}:{self.settings.user_id}"

    async def discover_resources(self):
        await self.module.discover()
        self.schedule_local_pending()

    def remember_identity(self, profile):
        if int(profile.get("ID", 0)) != self.settings.user_id:
            return
        name = " ".join(str(profile.get(key) or "").strip() for key in ("NAME", "LAST_NAME")).strip()
        self.db.set_state(self.identity_key(), name)

    async def refresh_identity(self):
        if not self.connected():
            return
        try:
            async with self.auth_lock:
                self.remember_identity(await self.client.profile())
        except (BitrixError, httpx.HTTPError, ValueError, OSError):
            pass  # An unavailable profile name must not stop catalogue/jobs.

    async def hydrate_chat(self, metadata):
        chat_id = metadata.get("chatId")
        if not chat_id:
            return metadata
        key = f"chat_title:{self.settings.portal}:{int(chat_id)}"
        cached = json.loads(self.db.get_state(key) or "{}")
        title = cached.get("title", "")
        if not self.chat_lookup_disabled and ((not title and cached.get("resolver") != 2) or time.time() - cached.get("at", 0) > (86400 if title else 300)):
            try:
                title = await self.client.chat_title(int(chat_id))
                self.db.set_state(key, json.dumps({"title": title, "at": time.time()}, ensure_ascii=False))
                self.chat_warning = ""
                await asyncio.sleep(.2)
            except (BitrixError, httpx.HTTPError, ValueError, OSError) as exc:
                if not isinstance(exc, BitrixError) or exc.auth:
                    self.chat_lookup_disabled = True
                self.chat_warning = "Названия некоторых бесед недоступны. Проверьте подключение и право im в приложении Bitrix24."
            if not title and not self.chat_lookup_disabled:
                try:
                    titles = await self.client.personal_chat_titles(self.personal_chat_peers([metadata]), self.personal_chat_names([metadata]))
                    title = titles.get(int(chat_id), "")
                    self.db.set_state(key, json.dumps({"title": title, "at": time.time(), "resolver": 2}, ensure_ascii=False))
                except (BitrixError, httpx.HTTPError, ValueError, OSError):
                    pass
        if title:
            metadata["chatTitle"] = title
        return metadata

    def personal_chat_peers(self, items):
        candidates = {}
        for item in items:
            if not item.get("chatId"):
                continue
            people = {int(p["user_id"]) for p in meeting_participants(self.settings.portal, item)}
            chat_id = int(item["chatId"])
            peer = next(iter(people - {self.settings.user_id})) if len(people) == 2 and self.settings.user_id in people else None
            candidates.setdefault(chat_id, set()).add(peer)
        return {chat_id: next(iter(peers)) for chat_id, peers in candidates.items()
                if len(peers) == 1 and None not in peers}

    def personal_chat_names(self, items):
        names = {}
        for item in items:
            for person in meeting_participants(self.settings.portal, item):
                if person["named"]:
                    names.setdefault(int(person["user_id"]), person["label"])
        return names

    async def hydrate_chats(self, items):
        missing = set()
        for metadata in items:
            chat_id = metadata.get("chatId")
            if chat_id:
                cached = json.loads(self.db.get_state(f"chat_title:{self.settings.portal}:{int(chat_id)}") or "{}")
                if (not cached.get("title") and cached.get("resolver") != 2) or time.time() - cached.get("at", 0) > (86400 if cached.get("title") else 300):
                    missing.add(int(chat_id))
        ids = sorted(missing)
        for offset in range(0, len(ids), 50):
            if self.chat_lookup_disabled:
                break
            group = ids[offset:offset + 50]
            try:
                titles, errors = await self.client.chat_titles(group)
                denied_scope = isinstance(errors, dict) and any(str(e.get("error", "")).lower() == "insufficient_scope" for e in errors.values())
                if not denied_scope:
                    peers = self.personal_chat_peers(items)
                    candidates = {chat_id: peers[chat_id] for chat_id in group if chat_id not in titles and chat_id in peers}
                    if candidates:
                        try:
                            titles.update(await self.client.personal_chat_titles(candidates, self.personal_chat_names(items)))
                        except (BitrixError, httpx.HTTPError, ValueError, OSError):
                            pass  # Do not replace an unavailable name with an unverified person.
                for chat_id in group:
                    key = f"chat_title:{self.settings.portal}:{chat_id}"
                    old = json.loads(self.db.get_state(key) or "{}")
                    self.db.set_state(key, json.dumps({"title": titles.get(chat_id, old.get("title", "")), "at": time.time(), "resolver": 2}, ensure_ascii=False))
                self.chat_warning = "Некоторые названия бесед недоступны текущему пользователю." if errors and any(chat_id not in titles for chat_id in group) else ""
                if denied_scope:
                    self.chat_lookup_disabled = True
                    self.chat_warning = "Для названий чатов добавьте право im и повторите вход."
                self.db.set_state("chat_revision", str(time.time_ns()))
            except (BitrixError, httpx.HTTPError, ValueError, OSError):
                self.chat_lookup_disabled = True
                self.chat_warning = "Названия чатов пока недоступны. Проверьте подключение и право im."
            if offset + 50 < len(ids):
                await asyncio.sleep(.2)
        for metadata in items:
            if metadata.get("chatId"):
                cached = json.loads(self.db.get_state(f"chat_title:{self.settings.portal}:{int(metadata['chatId'])}") or "{}")
                if cached.get("title"):
                    metadata["chatTitle"] = cached["title"]
        return items

    async def stop(self):
        self.alive = False
        await self.updates.stop()
        await self.notifications.stop()
        await self.module.stop_install()
        for job_id in list(self.module.processes):
            self.module.cancel_job(job_id)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.close()
        self.db.close()

    async def verify_connection(self):
        profile = await self.client.profile()
        self.settings.user_id = int(profile["ID"])
        # A real metadata call is required before reporting connected.
        end = now_iso()
        start = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        async for _ in self.client.catalogue(start, end):
            break
        self.auth_error = ""
        self.remember_identity(profile)
        self.settings.save(self.home)

    async def activate_connection(self, settings, values):
        """Verify a candidate without replacing a working connection on failure."""
        class CandidateVault:
            def __init__(self, data):
                self.data = data

            def read(self):
                return dict(self.data)

            def update(self, **updates):
                self.data.update(updates)

        async with self.auth_lock:
            candidate_settings = replace(settings)
            candidate_vault = CandidateVault({**self.vault.read(), **values})
            candidate = copy.copy(self.client)
            candidate.settings, candidate.vault = candidate_settings, candidate_vault
            candidate.refresh_lock = asyncio.Lock()
            profile = await candidate.profile()
            candidate_settings.user_id = int(profile["ID"])
            if candidate_settings.user_id <= 0:
                raise ValueError("Bitrix24 не вернул текущего пользователя")
            start = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            async for _ in candidate.catalogue(start, now_iso()):
                break
            async with self.scan_lock, self.client.refresh_lock:
                old_settings, old_secrets = asdict(self.settings), self.vault.read()
                try:
                    verified = candidate_vault.read()
                    auth_keys = {"client_id", "client_secret", "access_token", "refresh_token", "expires_at", "webhook"}
                    self.vault.write({**old_secrets, **{key: verified[key] for key in auth_keys if key in verified}})
                    for key in ("portal", "auth_mode", "user_id", "member_id", "oauth_relay", "oauth_flow"):
                        setattr(self.settings, key, getattr(candidate_settings, key))
                    self.settings.save(self.home)
                except Exception:
                    self.vault.write(old_secrets)
                    for key, value in old_settings.items():
                        setattr(self.settings, key, value)
                    raise
                self.client.settings = self.settings
                self.auth_error = ""
                self.remember_identity(profile)

    def owned(self, meeting: dict):
        if meeting["source"] == "bitrix" and meeting["portal"] != self.settings.portal:
            raise ValueError("Эта запись относится к другому порталу. Подключите соответствующий портал")

    async def scan(self, full=False):
        self.chat_lookup_disabled = False
        full = full or bool(self.db.rows("SELECT id FROM meetings WHERE source='bitrix' AND json_type(metadata,'$.chatId') IS NULL LIMIT 1"))
        if not self.connected() or self.scan_lock.locked() or self.updates.waiting:
            return
        async with self.scan_lock:
            self.catalogue.update(running=True, count=0, error="")
            end = now_iso()
            previous = self.db.get_state("last_scan:" + self.settings.portal)
            start = "2000-01-01T00:00:00Z" if full or not previous else (datetime.fromisoformat(previous) - timedelta(days=7)).isoformat()
            try:
                known = self.db.rows("SELECT id,metadata FROM meetings WHERE portal=?", (self.settings.portal,))
                enriched = await self.hydrate_chats([json.loads(row["metadata"]) for row in known])
                for row, metadata in zip(known, enriched):
                    if metadata.get("chatTitle"):
                        self.db.update_meeting(row["id"], metadata=json.dumps(metadata, ensure_ascii=False))
                async for page in self.catalogue_pages(start, end):
                    page = [item for item in page if any(int(p.get("userId", 0)) == self.settings.user_id for p in item.get("participants", []))]
                    cleaned = await self.hydrate_chats([clean_metadata(item) for item in page])
                    for item, metadata in zip(page, cleaned):
                        participants = item.get("participants", [])
                        if not any(int(p.get("userId", 0)) == self.settings.user_id for p in participants):
                            continue  # Never widen an administrator's catalogue to other people's calls.
                        previous_rows = self.db.rows("SELECT * FROM meetings WHERE portal=? AND call_id=?", (self.settings.portal, str(item["callId"])))
                        changed = bool(previous_rows and json.loads(previous_rows[0]["metadata"]) != metadata)
                        meeting = self.db.upsert(self.settings.portal, metadata)
                        state = followup_state(metadata, saved=meeting["bitrix"] == "saved")
                        if state == "short_call" or meeting["bitrix"] == "short_call":
                            self.db.update_meeting(meeting["id"], bitrix=state if state == "short_call" else "not_saved")
                            meeting = self.db.meeting(meeting["id"])
                        self.catalogue["count"] += 1
                        if meeting["requested"] and (changed or full) and not self.settings.paused:
                            self.request_download(meeting["id"], automatic=True)
                        if self.settings.auto_download and not self.settings.paused and item.get("endDate") and self.settings.auto_since:
                            completed = datetime.fromisoformat(item["endDate"].replace("Z", "+00:00"))
                            if completed >= datetime.fromisoformat(self.settings.auto_since) and not meeting["requested"]:
                                self.request_download(meeting["id"], automatic=True)
                    await asyncio.sleep(0)
                self.db.set_state("last_scan:" + self.settings.portal, end)
                if full:
                    self.db.set_state("last_full:" + self.settings.portal, end)
                self.auth_error = ""
                self.scan_retry_at = 0
                self.notifications.auth_seen = False
                self.db.set_state("scan_failures:" + self.settings.portal, "0")
            except (BitrixError, httpx.HTTPError, ValueError, OSError) as exc:
                self.catalogue["error"] = self.vault.redact(str(exc))
                if isinstance(exc, BitrixError) and exc.auth:
                    self.auth_error = self.catalogue["error"]
                    await self.notifications.failed({"id": 0}, self.auth_error, True)
                failure_key = "scan_failures:" + self.settings.portal
                failures = int(self.db.get_state(failure_key, "0")) + 1
                self.db.set_state(failure_key, str(failures))
                self.scan_retry_at = time.time() + min(3600, 60 * 2**min(failures, 6))
            finally:
                self.catalogue["running"] = False

    async def catalogue_pages(self, start, end):
        async for page in self.client.catalogue(start, end):
            yield page
        from .call_discovery import discover
        # Backfill independently of the normal seven-day Follow-up overlap.
        async for page in discover(self, "2000-01-01T00:00:00Z", end):
            yield page

    async def scheduler(self):
        while self.alive:
            if self.updates.waiting:
                await asyncio.sleep(.5)
                continue
            try:
                if self.connected() and time.time() >= self.scan_retry_at:
                    previous = self.db.get_state("last_full:" + self.settings.portal)
                    full = not previous or datetime.now(timezone.utc) - datetime.fromisoformat(previous) > timedelta(days=1)
                    await self.scan(full=full)
                if self.settings.watch_enabled and not self.settings.paused:
                    await asyncio.to_thread(self.watch_once)
                if self.settings.auto_local and not self.settings.paused:
                    self.schedule_local_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.db.set_state("watch_error", self.vault.redact(str(exc)))
            await asyncio.sleep(CATALOGUE_POLL_SECONDS)

    def request_download(self, meeting_id: int, automatic=False, *, audio_only=False):
        self.check_material_maintenance(meeting_id)
        meeting = self.db.meeting(meeting_id)
        self.owned(meeting)
        if meeting["source"] != "bitrix":
            return None
        self.db.update_meeting(meeting_id, requested=1)
        payload = {"automatic": automatic}
        if not automatic:
            payload["download_audio"] = bool(self.settings.auto_download_audio or self.settings.auto_local)
        if audio_only:
            payload.update(audio_only=True, audio_requested=True, download_audio=True)
        return self.db.enqueue("fetch", meeting_id, payload)

    def audio_requested(self, payload):
        return bool(payload.get("audio_only") or payload.get("audio_requested") or payload.get(
            "download_audio", self.settings.auto_download_audio or self.settings.auto_local))

    def reconcile_short_calls(self):
        for meeting in self.db.rows("SELECT * FROM meetings WHERE source='bitrix' AND portal=?", (self.settings.portal,)):
            metadata = json.loads(meeting["metadata"])
            if not any(int(p.get("userId", 0)) == self.settings.user_id for p in metadata.get("participants", [])):
                continue
            state = followup_state(metadata, saved=meeting["bitrix"] == "saved")
            if state != "short_call":
                if meeting["bitrix"] == "short_call":
                    self.db.update_meeting(meeting["id"], bitrix="waiting")
                continue
            self.db.update_meeting(meeting["id"], bitrix=state)
            if meeting["folder"]:
                self.archive.manifest(self.db.meeting(meeting["id"]))
            for job in self.db.rows("SELECT * FROM jobs WHERE kind='fetch' AND meeting_id=? AND state='queued'", (meeting["id"],)):
                if not self.audio_requested(json.loads(job["payload"])) or meeting["audio"] == "saved":
                    self.db.job_update(job["id"], state="done", progress=1, error="",
                                       message=self.fetch_completion_message(self.db.meeting(meeting["id"])))
                else:
                    self.db.job_update(job["id"], message="Ожидаем аудиозапись Bitrix24")

    @staticmethod
    def fetch_completion_message(meeting):
        if meeting["bitrix"] == "short_call":
            return ("Аудиозапись сохранена. Короткий звонок: текст Bitrix24 не предоставлен"
                    if meeting["audio"] == "saved" else "Короткий звонок: текст Bitrix24 не предоставлен")
        return "Материалы Bitrix24 сохранены"

    async def fetch(self, job: dict, progress) -> bool:
        meeting = self.db.meeting(job["meeting_id"])
        self.owned(meeting)
        item = await self.client.followup(meeting["call_id"])
        if str(item.get("callId")) != meeting["call_id"] or (meeting["uuid"] and item.get("uuid") != meeting["uuid"]):
            raise ValueError("Ответ Bitrix24 относится к другой сессии. Запись не сохранена")
        if not any(int(p.get("userId", 0)) == self.settings.user_id for p in item.get("participants", [])):
            raise ValueError("Текущий пользователь отсутствует среди участников. Скачивание остановлено")
        self.db.update_meeting(meeting["id"], metadata=json.dumps(await self.hydrate_chat(clean_metadata(item)), ensure_ascii=False))
        meeting = self.db.meeting(meeting["id"])
        folder = self.archive.ensure(meeting)
        self.db.update_meeting(meeting["id"], folder=str(folder))
        # A manual audio request can promote this job while the portal responds.
        current = self.db.rows("SELECT payload FROM jobs WHERE id=?", (job["id"],))
        payload = json.loads(current[0]["payload"] if current else job.get("payload") or "{}")
        audio_only = bool(payload.get("audio_only"))
        state = followup_state(item, saved=meeting["bitrix"] == "saved")
        if state == "short_call":
            self.db.update_meeting(meeting["id"], bitrix=state)
        elif meeting["bitrix"] == "short_call":
            self.db.update_meeting(meeting["id"], bitrix="waiting")
        if not audio_only:
            if self.archive.transcript(folder, item.get("transcription") or {}):
                self.db.update_meeting(meeting["id"], bitrix="saved")
            elif state != "short_call" and meeting["bitrix"] != "saved":
                self.db.update_meeting(meeting["id"], bitrix="waiting")
        tracks = item.get("tracks") or []
        download_audio = self.audio_requested(payload)
        new_audio = []
        self.archive.manifest(self.db.meeting(meeting["id"]))
        for track in tracks if download_audio else []:
            path, fresh = await self.archive.download(self.client, folder, track, progress)
            if fresh:
                new_audio.append(path.name)
        if download_audio and tracks:
            self.db.update_meeting(meeting["id"], audio="saved")
        elif meeting["audio"] != "saved":
            self.db.update_meeting(meeting["id"], audio="waiting" if download_audio else "not_saved")
        if new_audio and self.settings.auto_local:
            self.defer_local(meeting["id"], new_audio)
        self.archive.manifest(self.db.meeting(meeting["id"]))
        # Keep a requested source under observation, including tracks/AI blocks arriving later.
        saved = self.db.meeting(meeting["id"])
        return (audio_only or saved["bitrix"] in {"saved", "short_call"}) and (
            not download_audio or bool(tracks) or (saved["bitrix"] == "short_call" and saved["audio"] == "saved"))

    def request_import(self, path: Path, meeting_id: int | None = None, automatic=False) -> int:
        if meeting_id is not None:
            self.check_material_maintenance(meeting_id)
        path = path.resolve()
        if path.suffix.lower() not in MEDIA_EXTENSIONS or not path.is_file():
            raise ValueError("Выберите существующий аудио- или видеофайл")
        if path.is_relative_to(Path(self.settings.archive_root).resolve()) or path.is_relative_to(self.module.root.resolve()):
            raise ValueError("Не импортируйте файл из самого архива или папки модуля")
        if meeting_id is None:
            digest = hashlib.sha256(str(path).encode()).hexdigest()[:24]
            metadata = {"callId": "import-" + digest, "startDate": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                        "endDate": None, "durationSeconds": 0, "participants": [], "overview": {"topic": path.stem}}
            meeting_id = self.db.upsert("local-import", metadata, "import")["id"]
        else:
            self.db.meeting(meeting_id)
        return self.db.enqueue("import", meeting_id, {"path": str(path), "automatic": automatic})

    async def import_job(self, job: dict, progress):
        payload = json.loads(job["payload"])
        meeting = self.db.meeting(job["meeting_id"])
        source = Path(payload["path"])
        before = source.stat()
        await asyncio.sleep(2)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or not exclusive_readable(source):
            raise ValueError("Запись ещё изменяется или открыта для записи. Повторите импорт после завершения")
        path, fresh = await asyncio.to_thread(self.archive.import_file, meeting, source)
        self.db.update_meeting(meeting["id"], audio="saved", folder=str(path.parent.parent))
        self.archive.manifest(self.db.meeting(meeting["id"]))
        if fresh and self.settings.auto_local:
            self.defer_local(meeting["id"], [path.name])
        if payload.get("upload"):
            source = Path(payload["path"]).resolve()
            if source.is_relative_to(self.home / "uploads"):
                source.unlink(missing_ok=True)
        progress(1, "Запись добавлена в архив")

    def defer_local(self, meeting_id: int, names: list[str]):
        key = "auto_local_pending:" + str(meeting_id)
        previous = json.loads(self.db.get_state(key, "[]"))
        self.db.set_state(key, json.dumps(sorted(set(previous + names))))
        self.db.update_meeting(meeting_id, local="waiting")
        self.schedule_local_pending()

    def schedule_local_pending(self):
        if not self.settings.auto_local or self.settings.paused:
            return
        readiness = self.transcription_status()
        if not readiness["ready"]:
            self.db.set_state("auto_local_error", self.vault.redact(readiness["reason"]))
            return
        self.db.set_state("auto_local_error", "")
        for record in self.db.rows("SELECT key,value FROM state WHERE key LIKE 'auto_local_pending:%' AND value!='[]'"):
            meeting_id = int(record["key"].split(":")[1])
            try:
                self.request_transcribe(meeting_id, file_names=json.loads(record["value"]), automatic=True)
            except ValueError as exc:
                self.db.set_state("auto_local_error", self.vault.redact(str(exc)))
                continue
            self.db.set_state(record["key"], "[]")

    def transcription_status(self):
        try:
            if self.settings.diarization and not (self.vault.read().get("hf_token") and self.db.get_state("hf_verified")):
                raise ValueError("Сохраните токен Hugging Face и проверьте доступ перед включением диаризации")
            if self.settings.device == "cpu" and not self.settings.cpu_confirmed:
                raise ValueError("Подтвердите обработку на CPU в профиле")
            self.module.require_engine(self.settings)
            return {"ready": True, "reason": ""}
        except (ValueError, OSError) as exc:
            return {"ready": False, "reason": str(exc)}

    def automation_status(self):
        pending = self.db.rows("SELECT key,value FROM state WHERE key LIKE 'auto_local_pending:%' AND value!='[]'")
        readiness = self.transcription_status()
        window = window_status(self.settings.local_schedule)
        reason = ("Локальная авторасшифровка выключена" if not self.settings.auto_local else
                  "Автоматизация приостановлена" if self.settings.paused else
                  readiness["reason"] if not readiness["ready"] else
                  window["label"] if not window["allowed"] else "Готова: новые записи будут обработаны автоматически")
        return {"pending": len(pending), "ready": bool(self.settings.auto_local and not self.settings.paused and readiness["ready"] and window["allowed"]),
                "reason": reason}

    def automation_timing(self):
        return {"catalogue_poll_seconds": CATALOGUE_POLL_SECONDS, "queue_poll_seconds": QUEUE_POLL_SECONDS,
                "material_retry_seconds": [retry_delay(time.time() - age) for age in (0, 900, 86400)],
                "last_scan": self.db.get_state("last_scan:" + self.settings.portal),
                "catalogue_running": bool(self.catalogue["running"])}

    def check_material_maintenance(self, meeting_id):
        if meeting_id in self.material_maintenance:
            raise ValueError("Сначала дождитесь удаления материалов этой встречи")

    def material_plan(self, ids=None, targets=None):
        from .cleanup import selection_plan
        targets = targets if targets is not None else ["audio", "bitrix", "local", "notes"]
        if not isinstance(targets, list) or not targets:
            raise ValueError("Выберите материалы для удаления")
        for target in targets:
            parts = target.split("/") if isinstance(target, str) else []
            if not parts or parts[0] not in {"audio", "bitrix", "local", "notes"} or len(parts) > 2:
                raise ValueError("Выберите материалы из списка встречи")
        if ids is None:
            records = self.db.rows("SELECT * FROM meetings WHERE folder!='' ORDER BY id")
        else:
            if not isinstance(ids, list) or not ids or len(ids) > 10000 or any(type(i) is not int or i < 1 for i in ids):
                raise ValueError("Выберите совещания для удаления материалов")
            records = [self.db.meeting(i) for i in sorted(set(ids))]
        plans = []
        for record in records:
            if self.material_maintenance and record["id"] in self.material_maintenance:
                raise ValueError("Удаление материалов уже выполняется")
            if self.db.rows("SELECT id FROM jobs WHERE meeting_id=? AND state IN ('queued','running')", (record["id"],)):
                raise ValueError("Сначала завершите или отмените задачи выбранных совещаний")
            if record["folder"]:
                plans.append({"id": record["id"], **selection_plan(self.archive.folder(record), targets)})
        signature = hashlib.sha256(json.dumps([(p["id"], p["token"]) for p in plans]).encode()).hexdigest()
        return {"token": signature, "ids": [r["id"] for r in records], "targets": targets,
                "meetings": sum(bool(p["files"]) for p in plans), "files": sum(p["files"] for p in plans),
                "bytes": sum(p["bytes"] for p in plans), "plans": plans}

    def material_choices(self, ids=None):
        ids = ids if ids is not None else [r["id"] for r in self.db.rows("SELECT id FROM meetings WHERE folder!=''")]
        labels = {"audio": "Аудио и видео", "bitrix": "Текст Bitrix24 · все версии", "local": "Локальные расшифровки", "notes": "Заметки и результаты AI"}
        targets = list(labels)
        if len(ids) == 1:
            meeting = self.db.meeting(ids[0])
            if meeting["folder"]:
                folder = self.archive.folder(meeting)
                for group in ("audio", "local"):
                    base = self.archive.contained(folder / group, within=folder)
                    if not base.is_dir() or not any(path.name.startswith(".") for path in base.iterdir()):
                        targets = [t for t in targets if t != group]
                    if base.is_dir():
                        targets.extend(path.relative_to(folder).as_posix() for path in sorted(base.iterdir()) if not path.name.startswith("."))
        choices = []
        for target in targets:
            plan = self.material_plan(ids or None, [target])
            if plan["files"]:
                label = labels[target] if target in labels else ("Запись · " if target.startswith("audio/") else "Расшифровка · ") + target.split("/")[-1]
                choices.append({"target": target, "label": label,
                                "group": target.split("/")[0], "files": plan["files"], "bytes": plan["bytes"]})
        return choices

    async def remove_materials(self, ids, targets, token):
        from .cleanup import remove_selection
        plan = self.material_plan(ids, targets)
        if token != plan["token"]:
            raise ValueError("Состав файлов изменился. Откройте удаление заново")
        self.material_maintenance.update(plan["ids"])
        try:
            for entry in plan["plans"]:
                meeting = self.db.meeting(entry["id"])
                folder = self.archive.folder(meeting)
                try:
                    await asyncio.to_thread(remove_selection, folder, entry["targets"], entry["token"])
                finally:
                    audio = any(p.is_file() for p in (folder / "audio").glob("*"))
                    bitrix = any(p.is_file() for p in (folder / "bitrix").rglob("*"))
                    from .engine_catalog import read_marker
                    runs = [p.parent for p in (folder / "local").glob("*/run.json") if (p.parent / "transcript.txt").is_file()]
                    full = any(not read_marker(p / "run.json").get("sample") for p in runs)
                    self.db.update_meeting(entry["id"], audio="saved" if audio else "not_saved", bitrix="saved" if bitrix else "not_saved",
                        local="saved" if full else "tested" if runs else "not_saved", requested=0)
                    self.db.set_state("auto_local_pending:" + str(entry["id"]), "[]")
                    self.archive.manifest(self.db.meeting(entry["id"]))
            if any(t.split("/")[0] == "local" for t in targets):
                test = json.loads(self.db.get_state("model_test", "{}") or "{}")
                if not test.get("meeting_id") or test["meeting_id"] in plan["ids"]:
                    self.db.set_state("model_test", "")
            return {k: v for k, v in plan.items() if k != "plans"}
        finally:
            self.material_maintenance.difference_update(plan["ids"])

    def transcription_files(self, meeting, file_names=None):
        folder = self.archive.folder(meeting)
        audio = self.archive.contained(folder / "audio", within=folder)
        files = sorted(p for p in audio.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS) if audio.is_dir() else []
        for path in files:
            self.archive.contained(path, within=audio)
        if file_names is not None:
            if not isinstance(file_names, list) or not file_names or len(file_names) > 100:
                raise ValueError("Выберите хотя бы одну запись для обработки")
            selected = set()
            for name in file_names:
                if not isinstance(name, str):
                    raise ValueError("Некорректное имя записи")
                normalized = name.replace("\\", "/")
                if normalized.startswith("audio/"):
                    normalized = normalized[6:]
                if not normalized or "/" in normalized or normalized in {".", ".."} or ":" in normalized:
                    raise ValueError("Выберите запись из папки audio этого совещания")
                selected.add(normalized)
            available = {p.name for p in files}
            if not selected <= available:
                raise ValueError("Выбранная запись отсутствует. Обновите карточку или повторно скачайте материалы")
            files = [p for p in files if p.name in selected]
        if not files:
            raise ValueError("Сохранённая запись не найдена. Повторно скачайте материалы или добавьте файл")
        return files

    def request_transcribe(self, meeting_id: int, *, mode="combined", sample=False, file_names=None, automatic=False):
        self.check_material_maintenance(meeting_id)
        if self.module_maintenance:
            raise ValueError("Дождитесь удаления выбранных моделей")
        if mode not in {"combined", "merged_wav"}:
            raise ValueError("Неизвестный режим обработки")
        if self.settings.device == "cpu" and not self.settings.cpu_confirmed:
            raise ValueError("Подтвердите обработку на CPU в настройках")
        if self.settings.diarization and not (self.vault.read().get("hf_token") and self.db.get_state("hf_verified")):
            raise ValueError("Сначала сохраните токен и проверьте доступ к моделям Hugging Face")
        self.module.require_engine(self.settings)
        meeting = self.db.meeting(meeting_id)
        if not meeting["folder"] or meeting["audio"] != "saved":
            raise ValueError("Сначала скачайте или импортируйте аудио")
        files = self.transcription_files(meeting, file_names)
        file_names = [p.name for p in files]
        self.db.update_meeting(meeting_id, local="queued")
        return self.db.enqueue("transcribe", meeting_id, {"mode": mode, "sample": sample, "file_names": file_names,
            "settings": {k: getattr(self.settings, k) for k in ("engine", "parakeet_model", "gigaam_model", "model", "language", "device", "cpu_confirmed", "vad",
                "noise_reduction", "normalize", "diarization", "min_speakers", "max_speakers")},
            "external_engine": self.settings.external_engine if self.settings.engine == "whisper" else "", "automatic": automatic})

    async def transcribe_job(self, job: dict, progress):
        meeting, payload = self.db.meeting(job["meeting_id"]), json.loads(job["payload"])
        folder = self.archive.folder(meeting)
        files = self.transcription_files(meeting, payload.get("file_names"))
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + str(job["id"])
        pending = folder / "local" / (".pending-" + run_id)
        output = folder / "local" / run_id
        self.db.update_meeting(meeting["id"], local="running")
        result = await self.module.transcribe(job["id"], {"files": [str(p) for p in files], "output": str(pending),
            "source_files": [p.relative_to(folder).as_posix() for p in files],
            "settings": payload["settings"], "mode": payload["mode"], "sample_seconds": 30 if payload["sample"] else 0},
            payload.get("external_engine", self.settings.external_engine), progress)
        pending.rename(output)
        self.db.update_meeting(meeting["id"], local="tested" if payload["sample"] else "saved")
        self.archive.manifest(self.db.meeting(meeting["id"]))
        if payload["sample"]:
            self.db.set_state("model_test", json.dumps({"meeting_id": meeting["id"], "model": result.get("model"), "engine": result.get("engine", payload["settings"].get("engine", "whisper")),
                "device": payload["settings"]["device"], "settings": payload["settings"], "seconds": result.get("seconds"),
                "audio_seconds": result.get("audio_seconds"), "testedAt": now_iso()}))

    async def job_loop(self, kinds: tuple[str, ...]):
        while self.alive:
            blocked = []
            if self.updates.waiting:
                await asyncio.sleep(QUEUE_POLL_SECONDS)
                continue
            if not window_status(self.settings.download_schedule)["allowed"]:
                blocked.extend(("fetch", "import"))
            if not window_status(self.settings.local_schedule)["allowed"]:
                blocked.append("transcribe")
            job = self.db.claim(kinds, manual_only=self.settings.paused, blocked_automatic=tuple(blocked))
            if job is None:
                await asyncio.sleep(QUEUE_POLL_SECONDS)
                continue
            def progress(pct, message):
                self.db.job_update(job["id"], progress=max(0, min(1, pct)), message=self.vault.redact(message))
            try:
                task = asyncio.create_task(self.perform(job, progress))
                self.active_tasks[job["id"]] = task
                await task
                if self.db.rows("SELECT state FROM jobs WHERE id=?", (job["id"],))[0]["state"] == "running":
                    message = self.fetch_completion_message(self.db.meeting(job["meeting_id"])) if job["kind"] == "fetch" else "Завершено"
                    self.db.job_update(job["id"], state="done", progress=1, message=message)
                    self.notifications.completed(job)
            except asyncio.CancelledError:
                if not self.alive:
                    self.db.job_update(job["id"], state="queued", message="Продолжим после запуска")
                    raise
                self.db.job_update(job["id"], state="cancelled", message="Отменено")
                if job["kind"] == "transcribe":
                    self.db.update_meeting(job["meeting_id"], local="cancelled")
            except Exception as exc:
                message = self.vault.redact(str(exc))
                auth = isinstance(exc, BitrixError) and exc.auth
                retry = isinstance(exc, httpx.HTTPError) or (isinstance(exc, BitrixError) and exc.retryable)
                if auth:
                    self.auth_error = message
                if auth or not retry:
                    await self.notifications.failed(job, message, auth)
                self.db.job_update(job["id"], state="queued" if retry else "failed", error=message,
                    message="Повторим позже" if retry else "Требуется внимание", next_at=time.time() + min(3600, 60 * 2**min(job["attempts"], 6)))
                if job["kind"] == "transcribe":
                    self.db.update_meeting(job["meeting_id"], local="error")
            finally:
                self.active_tasks.pop(job["id"], None)

    async def perform(self, job: dict, progress):
        payload = json.loads(job["payload"])
        if job["kind"] == "fetch":
            ready = await self.fetch(job, progress)
            if not ready:
                payload = json.loads(self.db.rows("SELECT payload FROM jobs WHERE id=?", (job["id"],))[0]["payload"])
                # Later material checks are automatic continuations; the first
                # manually requested download still starts immediately.
                if not payload.get("automatic"):
                    payload.setdefault("download_audio", bool(self.settings.auto_download_audio or self.settings.auto_local))
                payload["automatic"] = True
                self.db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), job["id"]))
                self.db.job_update(job["id"], state="queued", next_at=time.time() + retry_delay(job["created"]),
                    message="Ожидаем аудиозапись Bitrix24" if self.db.meeting(job["meeting_id"])["bitrix"] == "short_call" else "Ожидаем материалы Bitrix24", error="")
        elif job["kind"] == "import":
            await self.import_job(job, progress)
        elif job["kind"] == "transcribe":
            await self.transcribe_job(job, progress)
        elif job["kind"] == "install":
            if payload.get("reuse_plan"):
                await self.module.reuse(payload["reuse_plan"], progress)
            elif payload.get("packages"):
                await self.module.install_packages(payload["profile"], payload["packages"], progress,
                                                   full_features=payload.get("full_features", False))
            else:
                await self.module.install(payload["profile"], payload["model"], progress)
            self.settings.external_engine = ""
            self.db.set_state("gpu_probe", "")
            self.settings.save(self.home)
            self.schedule_local_pending()

    async def cancel(self, job_id: int):
        rows = self.db.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows:
            raise ValueError("Задание не найдено")
        if rows[0]["kind"] == "install":
            await self.module.stop_install()
        self.module.cancel_job(job_id)
        self.db.job_update(job_id, state="cancelled", message="Отменено")
        task = self.active_tasks.get(job_id)
        if task:
            task.cancel()
        if rows[0]["kind"] == "transcribe":
            self.db.update_meeting(rows[0]["meeting_id"], local="cancelled")

    def watch_once(self):
        folder = Path(self.settings.watch_folder).expanduser().resolve()
        archive = Path(self.settings.archive_root).resolve()
        if not folder.is_dir():
            raise ValueError("Наблюдаемая папка недоступна")
        if folder.is_relative_to(archive) or archive.is_relative_to(folder) or folder.is_relative_to(self.home):
            raise ValueError("Папка наблюдения пересекается с архивом или служебной папкой")
        for path in folder.iterdir():
            if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            stat = path.stat()
            signature = f"{stat.st_size}:{stat.st_mtime_ns}"
            rows = self.db.rows("SELECT * FROM watched WHERE path=?", (str(path),))
            if not rows:
                self.db.execute("INSERT INTO watched(path,signature,stable_since) VALUES(?,?,?)", (str(path), signature, time.time()))
            elif signature != rows[0]["signature"]:
                self.db.execute("UPDATE watched SET signature=?,stable_since=? WHERE path=?", (signature, time.time(), str(path)))
            elif time.time() - rows[0]["stable_since"] >= 60 and signature != rows[0]["imported_signature"] and stat.st_size > 0:
                if not exclusive_readable(path):
                    continue
                self.request_import(path, automatic=True)
                self.db.execute("UPDATE watched SET imported_signature=? WHERE path=?", (signature, str(path)))
        self.db.set_state("watch_error", "")

    async def link(self, source_id: int, target_id: int):
        self.check_material_maintenance(source_id)
        self.check_material_maintenance(target_id)
        if source_id == target_id:
            raise ValueError("Выберите другое совещание для привязки")
        source, target = self.db.meeting(source_id), self.db.meeting(target_id)
        if source["source"] != "import" or not source["folder"]:
            raise ValueError("Привязывать можно сохранённый локальный импорт")
        self.material_maintenance.update({source_id, target_id})
        try:
            for path in (self.archive.folder(source) / "audio").iterdir():
                if path.is_file():
                    await asyncio.to_thread(self.archive.import_file, target, path)
            folder = self.archive.ensure(target)
            self.db.update_meeting(target_id, folder=str(folder), audio="saved")
            metadata = json.loads(source["metadata"])
            metadata["linkedTo"] = target_id
            self.db.update_meeting(source_id, metadata=json.dumps(metadata, ensure_ascii=False))
            self.archive.manifest(self.db.meeting(target_id))
            self.archive.manifest(self.db.meeting(source_id))
        finally:
            self.material_maintenance.difference_update({source_id, target_id})


def exclusive_readable(path: Path) -> bool:
    if os.name != "nt":
        try:
            with path.open("rb"):
                return True
        except OSError:
            return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        return False
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle(handle)
    return True
