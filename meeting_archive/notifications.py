"""Local Windows toast notifications; payloads contain only routing identifiers."""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

APP_ID = "MeetingArchive.Desktop"
ACTIVATOR = "{28E0343B-EAE4-478D-AFCF-CE1C91D461BC}"


def parse_activation(uri: str) -> dict:
    match = re.fullmatch(r"meetingarchive:(meeting|jobs|settings|connection)(?:/([0-9,]+))?", uri)
    if not match:
        raise ValueError("Неизвестный переход уведомления")
    route, raw = match.groups()
    ids = [int(n) for n in raw.split(",")] if raw else []
    if (len(ids) > 100 or any(n <= 0 or n > 2**63-1 for n in ids)
            or (route == "meeting" and len(ids) != 1) or (route in {"settings", "connection"} and ids)):
        raise ValueError("Некорректный переход уведомления")
    return {"route": route, "ids": ids}


def register_windows(home):
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    import winreg
    command = subprocess.list2cmdline([sys.executable, "--home", str(home), "--activate"]) + ' "%1"'
    values = {
        r"Software\Classes\meetingarchive": {"": "URL:Meeting Archive", "URL Protocol": ""},
        r"Software\Classes\meetingarchive\shell\open\command": {"": command},
        rf"Software\Classes\AppUserModelId\{APP_ID}": {
            "DisplayName": "Meeting Archive", "IconUri": sys.executable, "CustomActivator": ACTIVATOR},
        rf"Software\Classes\CLSID\{ACTIVATOR}": {"": "Meeting Archive notification identity"},
    }
    for path, entries in values.items():
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            for name, value in entries.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    from .desktop_shortcut import notification_shortcut
    shortcut = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Meeting Archive.lnk"
    notification_shortcut(shortcut, sys.executable, subprocess.list2cmdline(["--home", str(home)]), APP_ID, ACTIVATOR)


def windows_toast(title, message, uri):
    parse_activation(uri)
    if os.name != "nt":
        return False
    from winrt.windows.data.xml.dom import XmlDocument
    from winrt.windows.ui.notifications import ToastNotification, ToastNotificationManager
    document = XmlDocument()
    document.load_xml(f'<toast activationType="protocol" launch="{escape(uri)}"><visual><binding template="ToastGeneric">'
                      f'<text>{escape(title)}</text><text>{escape(message)}</text></binding></visual></toast>')
    ToastNotificationManager.create_toast_notifier_with_id(APP_ID).show(ToastNotification(document))
    return True


class Notifications:
    def __init__(self, service, sender=windows_toast):
        self.service, self.sender = service, sender
        self.pending = {}
        self.timers = {}
        self.error = ""
        self.auth_seen = False
        self.activation = None

    def enabled(self, kind):
        settings = self.service.settings
        return settings.notifications_enabled and getattr(settings, "notify_" + kind)

    async def send(self, title, message, uri):
        try:
            shown = await asyncio.to_thread(self.sender, title, self.service.vault.redact(message), uri)
            self.error = "" if shown else "Системные уведомления доступны в Windows"
            return shown
        except Exception as exc:
            self.error = self.service.vault.redact(str(exc))
            return False  # Notification delivery must never fail a completed job.

    def completed(self, job):
        kind = "download" if job["kind"] in {"fetch", "import"} else "transcription" if job["kind"] == "transcribe" else None
        if not kind or not self.enabled(kind):
            return
        self.pending.setdefault(kind, {})[job["id"]] = job["meeting_id"]
        if kind not in self.timers:
            self.timers[kind] = asyncio.create_task(self.flush(kind))

    async def flush(self, kind):
        try:
            await asyncio.sleep(10)
            jobs = self.pending.pop(kind, {})
            if not jobs or not self.enabled(kind):
                return
            title = "Загрузка совещания завершена" if kind == "download" else "Локальная расшифровка завершена"
            if len(jobs) == 1:
                meeting_id = next(iter(jobs.values()))
                uri = f"meetingarchive:meeting/{meeting_id}" if meeting_id else "meetingarchive:jobs"
                message = "Материалы сохранены. Нажмите, чтобы открыть результат."
            else:
                uri = "meetingarchive:jobs/" + ",".join(str(n) for n in list(jobs)[:100])
                message = f"Завершено задач: {len(jobs)}. Нажмите, чтобы открыть очередь."
            await self.send(title, message, uri)
        finally:
            self.timers.pop(kind, None)

    async def failed(self, job, message, auth=False):
        if not self.enabled("errors") or (auth and self.auth_seen):
            return
        if auth:
            self.auth_seen = True
        await self.send("Требуется внимание", message,
                        "meetingarchive:connection" if auth else f"meetingarchive:jobs/{job['id']}")

    def activate(self, uri):
        self.activation = {**parse_activation(uri), "sequence": uuid.uuid4().hex}
        return self.activation

    async def stop(self):
        timers = list(self.timers.values())
        for task in timers:
            task.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
