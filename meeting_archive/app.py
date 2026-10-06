from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import os
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from .bitrix import oauth_scopes
from .hardware import MODELS, detect, worker_probe
from .service import MEDIA_EXTENSIONS, Service, now_iso
from .settings import portal_domain
from .participants import meeting_participants, options as participant_options
from .archive import followup_state

STATIC = Path(__file__).parent / "static"
CALLBACK = "http://127.0.0.1:8765/oauth/callback"
LOCAL_CALLBACK = "http://localhost:8765/callback"


def relay_base(value: str) -> str:
    parsed = urlsplit(value.strip())
    try:
        ipaddress.ip_address(parsed.hostname or "")
        is_ip = True
    except ValueError:
        is_ip = False
    if (parsed.scheme != "https" or not parsed.hostname or "." not in parsed.hostname or is_ip
        or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.query
        or parsed.fragment or parsed.hostname.endswith((".local", ".localhost"))
        or any(part in {".", ".."} for part in parsed.path.split("/"))):
        raise ValueError("Укажите HTTPS-адрес собственного OAuth-обработчика без параметров")
    return value.strip().rstrip("/")


async def probe_relay(base: str, portal: str) -> dict:
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get(base + "/oauth/health")
        response.raise_for_status()
        data = response.json()
    path = urlsplit(base).path
    expected = {"protocol": "meeting-archive-oauth-relay", "version": 1,
                "callback_path": path + "/oauth/callback", "install_path": path + "/oauth/install",
                "loopback": CALLBACK, "credentials": "desktop-only"}
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items()):
        raise ValueError("HTTPS-адрес не является совместимым обработчиком Meeting Archive")
    if portal not in data.get("allowed_portals", []):
        raise ValueError("Обработчик не разрешает этот портал. Используйте собственный обработчик или webhook")
    return data


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archive_root: str | None = None
    chat_archive_root: str | None = None
    chat_auto_save: bool | None = None
    chat_scope: str | None = None
    chat_selected_ids: list[int] | None = None
    chat_excluded_ids: list[int] | None = None
    chat_history_since: str | None = None
    chat_poll_seconds: int | None = None
    chat_events: bool | None = None
    chat_download_images: bool | None = None
    chat_download_documents: bool | None = None
    chat_download_audio: bool | None = None
    chat_download_video: bool | None = None
    chat_download_other: bool | None = None
    chat_download_history: bool | None = None
    chat_attachment_schedule: dict | None = None
    chat_max_file_mb: int | None = Field(None, ge=0, le=1000000)
    auto_download: bool | None = None
    auto_download_audio: bool | None = None
    auto_local: bool | None = None
    download_schedule: dict | None = None
    local_schedule: dict | None = None
    watch_enabled: bool | None = None
    watch_folder: str | None = None
    paused: bool | None = None
    model: str | None = None
    engine: str | None = None
    parakeet_model: str | None = None
    gigaam_model: str | None = None
    language: str | None = None
    device: str | None = None
    cpu_confirmed: bool | None = None
    vad: bool | None = None
    noise_reduction: bool | None = None
    normalize: bool | None = None
    diarization: bool | None = None
    min_speakers: int | None = Field(None, ge=0, le=50)
    max_speakers: int | None = Field(None, ge=0, le=50)
    external_engine: str | None = None
    autostart: bool | None = None
    auto_update: bool | None = None
    notifications_enabled: bool | None = None
    notify_download: bool | None = None
    notify_transcription: bool | None = None
    notify_errors: bool | None = None


def meeting_view(record: dict) -> dict:
    record = dict(record)
    metadata = json.loads(record["metadata"])
    record.update(metadata=metadata, title=(metadata.get("overview") or {}).get("topic") or "Без названия",
                  startDate=metadata.get("startDate", ""), durationSeconds=metadata.get("durationSeconds", 0),
                  participants=meeting_participants(record["portal"], metadata),
                  participant_ids=[p["id"] for p in meeting_participants(record["portal"], metadata)],
                  chat_id=metadata.get("chatId"), chat_title=metadata.get("chatTitle") or "")
    availability = metadata.get("availability", {})
    if record["source"] == "bitrix":
        state = followup_state(metadata, saved=record["bitrix"] == "saved")
        if state == "short_call":
            record["bitrix"] = state
        elif record["bitrix"] == "short_call":
            record["bitrix"] = "not_saved"
        for field in ("audio", "bitrix"):
            if availability.get(field) and (record[field] == "not_saved" or
                    (field == "audio" and record[field] == "waiting" and availability[field] == "not_available")):
                record[field] = availability[field]
    return record


def autostart(enabled: bool):
    import subprocess
    import sys
    import winreg
    command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "meeting_archive.launcher"]
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        if enabled:
            winreg.SetValueEx(key, "MeetingArchive", 0, winreg.REG_SZ, subprocess.list2cmdline(command + ["--no-browser"]))
        else:
            try:
                winreg.DeleteValue(key, "MeetingArchive")
            except FileNotFoundError:
                pass


def picker(kind: str) -> list[str]:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if kind == "folder":
            selected = filedialog.askdirectory(parent=root, title="Выберите папку")
            return [selected] if selected else []
        if kind == "files":
            return list(filedialog.askopenfilenames(parent=root, title="Добавить записи", filetypes=[("Аудио и видео", " ".join("*" + e for e in sorted(MEDIA_EXTENSIONS))), ("Все файлы", "*.*")]))
        raise ValueError("Неизвестный тип выбора")
    finally:
        root.destroy()


def create_app(service: Service, launch_token: str | None = None, *, manage_lifecycle=True, shutdown_callback=None) -> FastAPI:
    launch_token = launch_token or secrets.token_urlsafe(32)
    # A surviving window can reconnect after an EXE restart without synthetic
    # navigation. The local session stays in the per-user DPAPI vault; deleting
    # the profile revokes it. CSRF and launch tokens remain per-run.
    session = service.vault.read().get("ui_session", "")
    if not isinstance(session, str) or len(session) != 43 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in session):
        session = secrets.token_urlsafe(32)
        service.vault.update(ui_session=session)
    csrf = secrets.token_urlsafe(32)
    pending_oauth: dict = {}
    resource_sources: dict = {}
    pick_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        if manage_lifecycle:
            await service.start()
        yield
        if manage_lifecycle:
            await service.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.launch_token = launch_token

    @app.middleware("http")
    async def security(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in {"127.0.0.1:8765", "localhost:8765", "testserver"}:
            return JSONResponse({"error": "Недопустимый адрес интерфейса"}, status_code=403)
        if request.url.path in {"/api/desktop/activate", "/api/desktop/open"} and request.method == "POST":
            if request.headers.get("origin") or not secrets.compare_digest(request.headers.get("x-desktop-token", ""), launch_token):
                return JSONResponse({"error": "Недопустимый переход"}, status_code=403)
            return await call_next(request)
        public = request.url.path in {"/", "/callback", "/oauth/callback"} or request.url.path.startswith("/static/")
        authenticated = secrets.compare_digest(request.cookies.get("meeting_session", ""), session)
        if not public and not authenticated:
            return JSONResponse({"error": "Откройте интерфейс из трея Meeting Archive"}, status_code=401)
        if request.method not in {"GET", "HEAD"}:
            origin = request.headers.get("origin")
            if not authenticated or not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf) or (origin and origin not in {"http://127.0.0.1:8765", "http://localhost:8765", "http://testserver"}):
                return JSONResponse({"error": "Запрос не подтверждён локальным интерфейсом"}, status_code=403)
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(Exception)
    async def failure(request, exc):
        return JSONResponse({"error": service.vault.redact(str(exc))}, status_code=400)

    @app.get("/")
    async def index(request: Request):
        if request.query_params.get("launch") and secrets.compare_digest(request.query_params["launch"], launch_token):
            response = RedirectResponse("/", status_code=303)
            response.set_cookie("meeting_session", session, httponly=True, samesite="lax", path="/")
            return response
        if not secrets.compare_digest(request.cookies.get("meeting_session", ""), session):
            return HTMLResponse("<html lang='ru'><meta charset='utf-8'><title>Meeting Archive</title><p>Откройте Meeting Archive через значок в трее или запустите приложение ещё раз.</p></html>", status_code=401)
        return FileResponse(STATIC / "index.html")

    @app.get("/static/{name}")
    async def static(name: str):
        if name not in {"app.js", "styles.css", "chat-archive.js", "chat-archive.css", "icon.svg", "icon.png", "favicon.ico"}:
            return JSONResponse({"error": "Файл не найден"}, status_code=404)
        return FileResponse(STATIC / name)

    @app.get("/api/bootstrap")
    async def bootstrap():
        from .scheduling import window_status
        jobs = service.db.rows("SELECT id,kind,meeting_id,state,attempts,created,error,progress,message,next_at FROM jobs ORDER BY id DESC LIMIT 100")
        windows = {"download": window_status(service.settings.download_schedule), "local": window_status(service.settings.local_schedule), "chat_attachment": window_status(service.settings.chat_attachment_schedule)}
        for job in jobs:
            payload = json.loads(service.db.rows("SELECT payload FROM jobs WHERE id=?", (job["id"],))[0]["payload"])
            job["automatic"] = bool(payload.get("automatic"))
            window = windows["local" if job["kind"] == "transcribe" else "download"]
            if job["state"] == "queued" and job["automatic"] and not window["allowed"]:
                job.update(schedule_wait=True, message=window["label"], schedule_next_at=window["next_at"])
        saved = service.vault.read()
        return {"csrf": csrf, "settings": asdict(service.settings), "connected": service.connected(),
                "updates": service.updates.status(), "activation": service.notifications.activation,
                "notification_error": service.notifications.error,
                "account_name": service.db.get_state(service.identity_key()) if service.connected() else "",
                "chat_warning": service.chat_warning, "chat_revision": service.db.get_state("chat_revision"),
                "secret_status": {"webhook_saved": bool(saved.get("webhook")),
                                  "hf_token_saved": bool(saved.get("hf_token")),
                                  "client_secret_saved": bool(saved.get("client_secret")),
                                  "client_id": saved.get("client_id", "")},
                "automation_windows": windows, "local_automation": await asyncio.to_thread(service.automation_status),
                "automation_timing": service.automation_timing(),
                "catalogue": {**service.catalogue, "total": service.db.rows("SELECT COUNT(*) AS total FROM meetings")[0]["total"]}, "auth_error": service.auth_error, "jobs": jobs,
                "module": await asyncio.to_thread(service.module.status), "watch_error": service.db.get_state("watch_error"),
                "hf_verified": bool(service.db.get_state("hf_verified")), "model_test": service.db.get_state("model_test"),
                "transcription": await asyncio.to_thread(service.transcription_status)}

    @app.post("/api/desktop/ready")
    async def desktop_ready():
        from .browser import mark_ready
        return {"ready": bool(mark_ready())}

    @app.post("/api/desktop/open")
    async def desktop_open():
        from .browser import open_browser
        await asyncio.to_thread(open_browser, "http://localhost:8765/?launch=" + app.state.launch_token)
        return {"opened": True}

    @app.post("/api/desktop/activate")
    async def desktop_activate(data: dict):
        try:
            service.notifications.activate(str(data.get("uri", "")))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True}

    @app.post("/api/notifications/ack")
    async def notification_ack(data: dict):
        current = service.notifications.activation
        if current and current["sequence"] == data.get("sequence"):
            service.notifications.activation = None
        return {"ok": True}

    @app.post("/api/notifications/test")
    async def notification_test():
        if not service.settings.notifications_enabled:
            return JSONResponse({"error": "Системные уведомления выключены"}, status_code=400)
        shown = await service.notifications.send("Meeting Archive", "Нажмите, чтобы открыть настройки.", "meetingarchive:settings")
        return {"sent": shown, "error": service.notifications.error}

    @app.get("/api/updates")
    async def update_status():
        return service.updates.status()

    @app.post("/api/updates/{operation}")
    async def update_operation(operation: str):
        try:
            if operation in {"check", "download"}:
                service.updates.launch_check(operation == "download")
            elif operation == "install":
                service.updates.request_install()
            elif operation == "cancel":
                service.updates.cancel_install()
            else:
                return JSONResponse({"error": "Неизвестное действие"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return service.updates.status()

    @app.get("/api/meetings")
    async def meetings(q: str = "", date_from: str = "", date_to: str = "", min_minutes: float = 0,
                       max_minutes: float = 0, audio: str = "", bitrix: str = "", local: str = "", offset: int = 0, limit: int = 50,
                       participants: str = "", chat: str = "", chats: str = ""):
        if len(participants) > 12000:
            raise ValueError("Слишком длинный список участников")
        selected = {value for value in participants.split(",") if value}
        if len(chats) > 12000:
            raise ValueError("Слишком длинный список чатов")
        selected_chats = {value for value in chats.split(",") if value}
        if len(selected_chats) > 100 or any(not value.isdecimal() for value in selected_chats):
            raise ValueError("Выберите до 100 чатов из списка")
        if len(selected) > 100:
            raise ValueError("Можно выбрать до 100 участников")
        records = [meeting_view(r) for r in service.db.rows("SELECT * FROM meetings")]
        def matches(r):
            minutes = float(r["durationSeconds"] or 0) / 60
            day = (r["startDate"] or "")[:10]
            if selected_chats and str(r["chat_id"] or "") not in selected_chats:
                return False
            if chat and (str(r["chat_id"] or "") != chat if chat.isdecimal() else chat.casefold() not in r["chat_title"].casefold()):
                return False
            return selected.issubset(r["participant_ids"]) and (not q or q.casefold() in r["title"].casefold() or q == str(r["id"]) or q == r["call_id"]) and (not date_from or day >= date_from) and (not date_to or day <= date_to) and minutes >= min_minutes and (not max_minutes or minutes <= max_minutes) and all(not value or r[key] == value for key, value in (("audio", audio), ("bitrix", bitrix), ("local", local)))
        records = sorted((r for r in records if matches(r)), key=lambda r: r["startDate"] or "", reverse=True)
        offset, limit = max(0, offset), max(1, min(200, limit))
        return {"items": records[offset:offset + limit], "total": len(records)}

    @app.get("/api/participants")
    async def participants():
        records = [meeting_view(r) for r in service.db.rows("SELECT * FROM meetings")]
        return {"items": participant_options(records)}

    @app.get("/api/chats")
    async def chats():
        choices = {}
        for row in service.db.rows("SELECT * FROM meetings WHERE portal=?", (service.settings.portal,)):
            record = meeting_view(row)
            if not record["chat_id"]:
                continue
            chat_id = str(record["chat_id"])
            cached = json.loads(service.db.get_state(f"chat_title:{record['portal']}:{chat_id}") or "{}")
            title = cached.get("title") or record["chat_title"]
            option = choices.setdefault(chat_id, {"id": chat_id, "label": title or f"Чат ID {chat_id}", "count": 0})
            option["count"] += 1
        return {"items": sorted(choices.values(), key=lambda item: item["label"].casefold())}

    @app.get("/api/meeting/{meeting_id}")
    async def detail(meeting_id: int):
        record = service.db.meeting(meeting_id)
        failed = service.db.rows("SELECT error FROM jobs WHERE meeting_id=? AND kind='transcribe' AND state='failed' ORDER BY id DESC LIMIT 1", (meeting_id,))
        result = {"meeting": meeting_view(record), "files": [], "bitrix_text": "", "local_text": "", "runs": [],
                  "transcription": await asyncio.to_thread(service.transcription_status),
                  "local_error": failed[0]["error"] if failed else ""}
        if not record["folder"]:
            return result
        folder = service.archive.ensure(record)
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder)
            if path.is_file() and not any(part.startswith(".") for part in relative.parts) and relative.parts[0] in {"audio", "bitrix", "local"}:
                result["files"].append({"name": relative.as_posix(), "url": f"/api/file/{meeting_id}/" + relative.as_posix(), "kind": relative.parts[0]})
        bitrix_path = folder / "bitrix/transcript.txt"
        if bitrix_path.exists():
            result["bitrix_text"] = bitrix_path.read_text("utf-8")
        for path in sorted((folder / "local").glob("*/run.json"), reverse=True):
            if path.parent.name.startswith("."):
                continue
            run = json.loads(path.read_text("utf-8"))
            transcript = path.parent / "transcript.txt"
            text = transcript.read_text("utf-8") if transcript.exists() else ""
            result["runs"].append({"id": path.parent.name, "text": text, **run})
            if not result["local_text"] and not run.get("sample") and transcript.exists():
                result["local_text"] = text
        return result

    @app.get("/api/file/{meeting_id}/{relative:path}")
    async def file(meeting_id: int, relative: str):
        record = service.db.meeting(meeting_id)
        if not record["folder"]:
            raise ValueError("Материалы ещё не сохранены")
        folder = service.archive.folder(record).resolve()
        path = (folder / relative).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or not Path(relative).parts or Path(relative).parts[0] not in {"audio", "bitrix", "local"} or any(p.startswith(".") for p in Path(relative).parts):
            raise ValueError("Файл недоступен")
        return FileResponse(path)

    @app.post("/api/settings")
    async def settings(data: SettingsPatch):
        from .scheduling import validate_schedule
        from .chat_sync import validate_chat_settings
        values = data.model_dump(exclude_unset=True, exclude_none=True)
        validate_chat_settings(values, service.settings, service.home)
        old_settings = replace(service.settings)
        if values.get("auto_local", service.settings.auto_local):
            if values.get("auto_download_audio") is False:
                raise ValueError("Для локальной авторасшифровки требуется скачивание аудио. Сначала выключите локальную авторасшифровку")
            values["auto_download_audio"] = True
        for key in ("download_schedule", "local_schedule", "chat_attachment_schedule"):
            if key in values:
                values[key] = validate_schedule(values[key])
        if values.get("engine", service.settings.engine) not in {"whisper", "parakeet", "gigaam"}:
            raise ValueError("Выберите поддерживаемый движок из каталога")
        if values.get("parakeet_model", service.settings.parakeet_model) != "parakeet-tdt-0.6b-v3-q8":
            raise ValueError("Неизвестная модель Parakeet")
        if values.get("gigaam_model", service.settings.gigaam_model) != "gigaam-v3-e2e-rnnt":
            raise ValueError("Неизвестная модель GigaAM")
        if values.get("model", service.settings.model) not in {m["name"] for m in MODELS}:
            raise ValueError("Неизвестная модель")
        if values.get("device", service.settings.device) not in {"cpu", "cuda"}:
            raise ValueError("Выберите CPU или CUDA")
        if values.get("language", service.settings.language) not in {"auto", "ru", "en"}:
            raise ValueError("Выберите автоматический, русский или английский язык")
        if values.get("device", service.settings.device) == "cpu" and not values.get("cpu_confirmed", service.settings.cpu_confirmed):
            raise ValueError("CPU работает существенно медленнее. Подтвердите этот выбор")
        if values.get("diarization", service.settings.diarization) and not (service.vault.read().get("hf_token") and service.db.get_state("hf_verified")):
            raise ValueError("Сначала проверьте доступ к обеим моделям Hugging Face")
        if "archive_root" in values:
            path = Path(values["archive_root"]).expanduser()
            if not path.is_absolute() or path.resolve().is_relative_to(service.home) or service.home.is_relative_to(path.resolve()):
                raise ValueError("Архив должен находиться отдельно от служебной папки приложения")
            if path.resolve() != Path(service.settings.archive_root).resolve() and service.db.rows("SELECT id FROM meetings WHERE folder!='' LIMIT 1"):
                raise ValueError("Папка связана с сохранёнными совещаниями текущего профиля. Для новой установки закройте приложение и запустите uninstall.cmd рядом с EXE: выберите сброс профиля с сохранением совещаний. После сброса можно выбрать другую папку. Текущая папка пока сохранена")
            values["archive_root"] = str(path.resolve())
        if values.get("external_engine"):
            service.module.python(values["external_engine"])
        if values.get("auto_download") and not service.settings.auto_download:
            service.settings.auto_since = now_iso()
        if "autostart" in values and values["autostart"] != service.settings.autostart:
            autostart(values["autostart"])
        async with service.auth_lock, service.chat_archive.lock:
            for key, value in values.items():
                setattr(service.settings, key, value)
            service.settings.save(service.home)
            await service.chat_archive.settings_changed(old_settings)
        service.schedule_local_pending()
        return {"settings": asdict(service.settings)}

    @app.post("/api/auth/oauth")
    async def oauth(request: Request):
        data = await request.json()
        portal = portal_domain(data.get("portal", ""))
        client_id = data.get("client_id", "").strip()
        saved = service.vault.read()
        client_secret = data.get("client_secret", "").strip()
        if not client_secret and portal == service.settings.portal and client_id == saved.get("client_id"):
            client_secret = saved.get("client_secret", "")
        if not client_id or not client_secret:
            raise ValueError("Введите код приложения и секрет из карточки Bitrix24")
        flow = data.get("flow", "local")
        if flow not in {"local", "relay", "oob"}:
            raise ValueError("Выберите вход на этом компьютере, HTTPS-обработчик или одноразовый код")
        base = relay_base(data.get("oauth_relay", "")) if flow == "relay" else ""
        if flow == "relay":
            await probe_relay(base, portal)
        pending_oauth.clear()
        pending_oauth.update(portal=portal, client_id=client_id, client_secret=client_secret,
                             oauth_relay=base, flow=flow, state=secrets.token_urlsafe(32), created=time.time())
        parameters = {"client_id": client_id, "state": pending_oauth["state"]}
        if flow == "local":
            # Keep the same redirect URI as the local application's registered handler.
            parameters["redirect_uri"] = LOCAL_CALLBACK
        return {"flow": flow, "attempt": pending_oauth["state"],
                "url": "https://" + portal + "/oauth/authorize/?" + urlencode(parameters)}

    async def complete_oauth(pending: dict, code: str, expected_member: str = ""):
        import copy
        candidate = copy.copy(service.client)
        candidate.settings = replace(service.settings, portal=pending["portal"], user_id=0, auth_mode="oauth",
            oauth_relay=pending["oauth_relay"], oauth_flow=pending["flow"])
        if pending["flow"] == "local":
            tokens = await candidate.exchange(code, pending["client_id"], pending["client_secret"], LOCAL_CALLBACK)
        else:
            tokens = await candidate.exchange(code, pending["client_id"], pending["client_secret"])
        old_member = service.settings.member_id if service.settings.portal == pending["portal"] else ""
        if ((expected_member and tokens.get("member_id") != expected_member)
            or (old_member and tokens.get("member_id") != old_member)):
            raise ValueError("Идентификатор портала OAuth не совпадает")
        candidate.settings.member_id = tokens["member_id"]
        await service.activate_connection(candidate.settings, dict(client_id=pending["client_id"],
            client_secret=pending["client_secret"], access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"], expires_at=tokens["expires_at"], webhook=""))
        asyncio.create_task(service.scan(full=True))

    @app.post("/api/auth/oauth/code")
    async def oob_code(request: Request):
        data = await request.json()
        if (not pending_oauth or pending_oauth.get("flow") != "oob"
            or time.time() - pending_oauth["created"] > 600
            or not secrets.compare_digest(data.get("attempt", ""), pending_oauth["state"])):
            raise ValueError("Попытка входа истекла. Откройте новый вход Bitrix24")
        code = data.get("code", "").strip()
        if not code or len(code) > 256 or any(ord(c) < 32 for c in code):
            raise ValueError("Введите одноразовый код из браузера сразу после входа")
        pending = dict(pending_oauth)
        pending_oauth.clear()
        try:
            await complete_oauth(pending, code)
        except Exception as exc:
            message = service.vault.redact(str(exc)).replace(code, "[скрыто]").replace(pending["client_secret"], "[скрыто]")
            raise ValueError(message) from None
        return {"connected": True}

    @app.get("/callback")
    @app.get("/oauth/callback")
    async def callback(request: Request):
        params = request.query_params
        if (len(params.multi_items()) != len(params)
            or any(key not in {"state", "domain", "code", "member_id", "scope", "server_domain"} for key in params)):
            return HTMLResponse("Некорректные параметры OAuth. Начните вход заново.", status_code=400)
        expected_flow = "local" if request.url.path == "/callback" else "relay"
        if not pending_oauth or pending_oauth.get("flow") != expected_flow or time.time() - pending_oauth["created"] > 600 or not secrets.compare_digest(params.get("state", ""), pending_oauth["state"]):
            return HTMLResponse("Недействительный или просроченный state. Начните вход заново.", status_code=400)
        pending = dict(pending_oauth)
        pending_oauth.clear()
        code = params.get("code", "")
        if (params.get("domain") != pending["portal"] or not code or len(code) > 256
            or any(ord(c) < 32 for c in code) or not params.get("member_id")
            or "call" not in oauth_scopes(params.get("scope", ""))
            or params.get("server_domain", "oauth.bitrix.info") not in {"oauth.bitrix.info", "oauth.bitrix24.tech"}):
            return HTMLResponse("Портал или права OAuth не совпадают. Начните вход заново.", status_code=400)
        try:
            await complete_oauth(pending, code, params["member_id"])
            # Authorize the canonical host even when an old UI used 127.0.0.1.
            response = RedirectResponse("http://localhost:8765/?launch=" + launch_token + "#connection", status_code=303)
            response.set_cookie("meeting_session", session, httponly=True, samesite="lax", path="/")
            return response
        except Exception as exc:
            message = service.vault.redact(str(exc)).replace(code, "[скрыто]").replace(pending["client_secret"], "[скрыто]")
            return HTMLResponse("<meta charset='utf-8'><p>" + html.escape(message) + "</p><a href='/'>Вернуться к подключению</a>", status_code=400)

    @app.post("/api/auth/webhook")
    async def webhook(request: Request):
        value = (await request.json()).get("webhook", "").strip()
        parsed = urlsplit(value)
        portal = portal_domain("https://" + (parsed.hostname or ""))
        parts = parsed.path.removeprefix("/rest/").removeprefix("api/").strip("/").split("/")
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or not parsed.path.startswith("/rest/") or len(parts) != 2 or not parts[0].isdigit() or not parts[1] or parsed.query or parsed.fragment:
            raise ValueError("Введите базовый адрес входящего webhook из Bitrix24 без имени метода")
        candidate = replace(service.settings, portal=portal, auth_mode="webhook", user_id=0, member_id="")
        await service.activate_connection(candidate, dict(webhook=value, access_token="", refresh_token="", client_secret="", client_id=""))
        asyncio.create_task(service.scan(full=True))
        return {"connected": True}

    @app.post("/api/auth/disconnect")
    async def disconnect():
        pending_oauth.clear()
        async with service.auth_lock, service.scan_lock, service.client.refresh_lock:
            for record in service.db.rows("SELECT id FROM jobs WHERE kind='fetch' AND state IN ('queued','running')"):
                await service.cancel(record["id"])
            service.vault.update(webhook="", access_token="", refresh_token="", client_secret="")
            service.settings.user_id = 0
            service.settings.save(service.home)
        return {"connected": False}

    @app.post("/api/catalogue/refresh")
    async def refresh():
        if not service.connected():
            raise ValueError("Сначала подключите Bitrix24")
        asyncio.create_task(service.scan(full=True))
        return {"started": True}

    @app.post("/api/download")
    async def download(request: Request):
        data = await request.json()
        audio_only = data.get("audio_only", False)
        if not isinstance(audio_only, bool):
            raise ValueError("Укажите, требуется ли только аудиозапись")
        return {"jobs": [service.request_download(int(i), audio_only=audio_only) for i in data.get("ids", [])]}

    @app.post("/api/import")
    async def import_files(request: Request):
        data = await request.json()
        return {"jobs": [service.request_import(Path(p), data.get("meeting_id")) for p in data.get("paths", [])]}

    @app.post("/api/upload")
    async def upload(request: Request):
        form = await request.form(max_files=50)
        meeting_id = int(form["meeting_id"]) if form.get("meeting_id") else None
        jobs = []
        for incoming in form.getlist("files"):
            if not isinstance(incoming, UploadFile) and not hasattr(incoming, "filename"):
                continue
            name = Path(incoming.filename or "recording").name
            if Path(name).suffix.lower() not in MEDIA_EXTENSIONS:
                raise ValueError("Этот формат записи не поддерживается")
            directory = service.home / "uploads" / uuid.uuid4().hex
            directory.mkdir(parents=True)
            path = directory / name
            try:
                with path.open("wb") as output:
                    while chunk := await incoming.read(1024 * 1024):
                        if shutil.disk_usage(directory).free < len(chunk) + 50 * 1024**2:
                            raise ValueError("Недостаточно места для импорта")
                        output.write(chunk)
                job = service.request_import(path, meeting_id)
                row = service.db.rows("SELECT payload FROM jobs WHERE id=?", (job,))[0]
                payload = json.loads(row["payload"])
                payload["upload"] = True
                service.db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), job))
                jobs.append(job)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            finally:
                await incoming.close()
        return {"jobs": jobs}

    @app.post("/api/picker")
    async def pick(request: Request):
        async with pick_lock:
            return {"paths": await asyncio.to_thread(picker, (await request.json()).get("kind"))}

    @app.post("/api/transcribe")
    async def transcribe(request: Request):
        data = await request.json()
        return {"job": service.request_transcribe(int(data["id"]), mode=data.get("mode", "combined"), sample=bool(data.get("sample", False)), file_names=data.get("file_names"))}

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: int):
        await service.cancel(job_id)
        return {"cancelled": True}

    @app.post("/api/jobs/{job_id}/retry")
    async def retry(job_id: int):
        rows = service.db.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows or rows[0]["state"] not in {"failed", "cancelled", "done"}:
            raise ValueError("Задание нельзя повторить в текущем состоянии")
        if rows[0]["meeting_id"]:
            service.check_material_maintenance(rows[0]["meeting_id"])
        if service.module_maintenance and rows[0]["kind"] in {"install", "transcribe"}:
            raise ValueError("Сначала дождитесь удаления моделей")
        payload = json.loads(rows[0]["payload"])
        payload["automatic"] = False
        service.db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), job_id))
        service.db.job_update(job_id, state="queued", next_at=0, error="", progress=0, message="В очереди")
        return {"queued": True}

    @app.post("/api/jobs/{job_id}/start-now")
    async def start_now(job_id: int):
        rows = service.db.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows or rows[0]["state"] != "queued":
            raise ValueError("Запустить сейчас можно только ожидающую задачу")
        payload = json.loads(rows[0]["payload"])
        payload["automatic"] = False
        service.db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), job_id))
        service.db.job_update(job_id, next_at=0, error="", message="Ручной запуск")
        return {"queued": True}

    @app.get("/api/hardware")
    async def hardware():
        data = await asyncio.to_thread(detect)
        saved = service.db.get_state("gpu_probe")
        if saved:
            probe = json.loads(saved)
            if probe.get("profile") == probe_profile():
                data["probe"] = probe
                data["cuda"] = ("Обнаружена движком Parakeet; проверьте модель на записи" if probe.get("engine") == "parakeet" else "Проверена — GPU работает") if probe.get("compatible") else "Проверка не пройдена"
        return data

    def probe_profile():
        settings = service.settings
        return {"engine": settings.engine, "device": settings.device,
                "model": settings.model if settings.engine == "whisper" else getattr(settings, settings.engine + "_model")}

    @app.post("/api/hardware/probe")
    async def probe():
        profile = probe_profile()
        if profile["device"] != "cuda":
            raise ValueError("В профиле выбран CPU. Для проверки GPU выберите GPU NVIDIA и сохраните настройки")
        if service.settings.engine == "parakeet":
            from .worker.install_parakeet import probe_parakeet
            result = await asyncio.to_thread(probe_parakeet, service.module.checked_root(), service.settings.device)
        else:
            external = service.settings.external_engine if service.settings.engine == "whisper" else ""
            if not external and service.settings.engine == "whisper" and service.module.borrowed_source(service.settings.model):
                python, env = await asyncio.to_thread(service.module.borrowed_environment, service.settings.model)
            else:
                python, env = service.module.python(external), service.module.env(bool(external))
            result = await asyncio.to_thread(worker_probe, python, env)
        result.update(profile=profile, checkedAt=now_iso())
        service.db.set_state("gpu_probe", json.dumps(result))
        return result

    @app.post("/api/module/install")
    async def install(request: Request):
        if service.module_maintenance:
            raise ValueError("Дождитесь удаления выбранных моделей")
        data = await request.json()
        packages = data.get("packages") or [{"engine": data.get("engine", "whisper"), "model": data.get("model")}]
        features = bool(service.settings.diarization or service.settings.noise_reduction or service.settings.normalize)
        await service.module.discover()
        plan = await asyncio.to_thread(service.module.installation_plan, data.get("profile"), packages, full_features=features)
        if not plan["runtime"]["available"]:
            raise ValueError(plan["runtime"]["reason"])
        if data["profile"] == "cpu" and not service.settings.cpu_confirmed:
            raise ValueError("Перед установкой CPU-профиля подтвердите выбор CPU в настройках")
        if service.db.rows("SELECT id FROM jobs WHERE kind IN ('install','transcribe') AND state IN ('queued','running')"):
            raise ValueError("Сначала завершите или отмените задачи модуля")
        return {"job": service.db.enqueue("install", payload={"profile": data["profile"], "packages": plan["packages"], "full_features": features})}

    @app.get("/api/module/engines")
    async def engines():
        await service.module.discover()
        return {**service.module.catalogue(service.settings.device),
                "processing_support": service.module.processing_support(service.settings)}

    @app.post("/api/materials/plan")
    async def material_plan(request: Request):
        data = await request.json()
        plan = await asyncio.to_thread(service.material_plan, data.get("ids"), data.get("targets"))
        return {**{k: v for k, v in plan.items() if k != "plans"},
                "choices": await asyncio.to_thread(service.material_choices, data.get("ids"))}

    @app.post("/api/materials/remove")
    async def material_remove(request: Request):
        data = await request.json()
        if data.get("confirm") is not True:
            raise ValueError("Подтвердите удаление выбранных материалов")
        return await service.remove_materials(data.get("ids"), data.get("targets"), data.get("token"))

    @app.post("/api/module/models/remove-plan")
    async def model_remove_plan(request: Request):
        data = await request.json()
        if service.module_maintenance or service.db.rows("SELECT id FROM jobs WHERE kind IN ('install','transcribe') AND state IN ('queued','running')"):
            raise ValueError("Сначала завершите или отмените задачи расшифровки и установки")
        return await asyncio.to_thread(service.module.model_removal_plan, data.get("packages"))

    @app.post("/api/module/models/remove")
    async def model_remove(request: Request):
        data = await request.json()
        if data.get("confirm") is not True:
            raise ValueError("Подтвердите удаление выбранных моделей")
        if service.module_maintenance or service.db.rows("SELECT id FROM jobs WHERE kind IN ('install','transcribe') AND state IN ('queued','running')"):
            raise ValueError("Сначала завершите или отмените задачи расшифровки и установки")
        service.module_maintenance = True
        try:
            result = await asyncio.to_thread(service.module.remove_models, data.get("packages"), data.get("token"))
            service.db.set_state("model_test", "")
            return result
        finally:
            service.module_maintenance = False

    @app.post("/api/module/install-plan")
    async def install_plan(request: Request):
        data = await request.json()
        features = bool(service.settings.diarization or service.settings.noise_reduction or service.settings.normalize)
        await service.module.discover()
        return await asyncio.to_thread(service.module.installation_plan, data.get("profile"), data.get("packages"), full_features=features)

    @app.get("/api/module/resources")
    async def resources(environment: str = ""):
        from .resource_reuse import discover_resources
        extra = Path(environment).expanduser() if environment else None
        result = await asyncio.to_thread(discover_resources, extra)
        resource_sources.clear()
        resource_sources.update({s["source_id"]: s for s in result["sources"]})
        return result

    @app.post("/api/module/reuse-plan")
    async def reuse_plan(request: Request):
        from .resource_reuse import plan_resources
        data = await request.json()
        source = resource_sources.get(data.get("source_id"))
        if not source or not source["compatible"]:
            raise ValueError("Сначала найдите совместимые установленные ресурсы")
        return await asyncio.to_thread(plan_resources, Path(source["environment_path"]),
                                      data.get("model", service.settings.model), service.module.checked_root())

    @app.post("/api/module/reuse")
    async def reuse(request: Request):
        from .resource_reuse import plan_resources
        data = await request.json()
        if service.module_maintenance:
            raise ValueError("Сначала дождитесь удаления моделей")
        source = resource_sources.get(data.get("source_id"))
        if not source or not source["compatible"]:
            raise ValueError("Обновите список установленных ресурсов")
        plan = await asyncio.to_thread(plan_resources, Path(source["environment_path"]),
                                      data.get("model", service.settings.model), service.module.checked_root())
        if plan != data.get("plan"):
            raise ValueError("План ресурсов изменился. Просмотрите и подтвердите новый план")
        if service.settings.device == "cpu" and not service.settings.cpu_confirmed:
            raise ValueError("Подтвердите выбор CPU в настройках")
        if service.db.rows("SELECT id FROM jobs WHERE kind IN ('install','transcribe') AND state IN ('queued','running')"):
            raise ValueError("Сначала завершите или отмените задачи модуля")
        return {"job": service.db.enqueue("install", payload={"reuse_plan": plan})}

    @app.post("/api/module/cancel")
    async def cancel_install():
        for record in service.db.rows("SELECT id FROM jobs WHERE kind='install' AND state IN ('queued','running')"):
            await service.cancel(record["id"])
        return {"cancelled": True}

    @app.post("/api/module/remove")
    async def remove(request: Request):
        if (await request.json()).get("confirm") is not True:
            raise ValueError("Подтвердите удаление после просмотра объёма")
        if service.module_maintenance:
            raise ValueError("Удаление ресурсов уже выполняется")
        service.module_maintenance = True
        try:
            active = []
            for record in service.db.rows("SELECT id FROM jobs WHERE kind IN ('install','transcribe') AND state IN ('queued','running')"):
                task = service.active_tasks.get(record["id"])
                if task:
                    active.append(task)
                await service.cancel(record["id"])
            await asyncio.gather(*active, return_exceptions=True)
            await service.module.remove()
            return {"removed": True}
        finally:
            service.module_maintenance = False

    @app.post("/api/hf/check")
    async def check_hf(request: Request):
        token = (await request.json()).get("token", "").strip()
        token = token or service.vault.read().get("hf_token", "")
        if not token.startswith("hf_") or len(token) > 500:
            raise ValueError("Введите токен Read из Hugging Face")
        checks = []
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for model, file_name in (("speaker-diarization-3.1", "config.yaml"), ("segmentation-3.0", "pytorch_model.bin")):
                response = await client.head(f"https://huggingface.co/pyannote/{model}/resolve/main/{file_name}", headers={"Authorization": "Bearer " + token})
                available = response.status_code == 200 or (response.status_code in {302, 303, 307, 308} and bool(response.headers.get("location")))
                checks.append({"model": model, "available": available, "status": response.status_code})
        if not all(c["available"] for c in checks):
            return {"verified": False, "checks": checks, "error": "Примите условия обеих моделей под тем же аккаунтом и проверьте токен Read"}
        service.vault.update(hf_token=token)
        service.db.set_state("hf_verified", hashlib.sha256(token.encode()).hexdigest())
        return {"verified": True, "checks": checks}

    @app.post("/api/hf/remove")
    async def remove_hf():
        service.vault.update(hf_token="")
        service.db.set_state("hf_verified", "")
        service.settings.diarization = False
        service.settings.save(service.home)
        return {"removed": True}

    @app.post("/api/open-folder")
    async def open_folder(request: Request):
        data = await request.json()
        path = service.archive.ensure(service.db.meeting(int(data["id"]))) if data.get("id") else Path(service.settings.archive_root)
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)
        return {"opened": True}

    @app.post("/api/link")
    async def link(request: Request):
        data = await request.json()
        await service.link(int(data["source_id"]), int(data["target_id"]))
        return {"linked": True}

    @app.post("/api/shutdown")
    async def shutdown():
        if shutdown_callback:
            shutdown_callback()
        return {"stopping": True}

    from .chat_api import register_chat_api
    register_chat_api(app, service)
    return app
