"""GitHub release downloads and program-only update staging."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx

from . import __version__
from .settings import atomic_json

REPOSITORY = "FaithfullenCS/meeting-archive-bitrix24"
ASSET = "MeetingArchive-Windows-x64.zip"


def version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise ValueError("Неподдерживаемый номер версии")
    return tuple(map(int, match.groups()))


def safe_relative(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Некорректный путь обновления")
    path = PurePosixPath(value)
    if (not value or "\\" in value or ":" in value or path.is_absolute()
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or path.parts[0].casefold() in {"данные", "models", "profile", ".git"}
            or any(part.endswith((".", " ")) for part in path.parts)):
        raise ValueError("Небезопасный путь обновления")
    if any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in path.parts):
        raise ValueError("Зарезервированное имя Windows")
    return path.as_posix()


def read_manifest(root: Path) -> dict:
    data = json.loads((root / "update-manifest.json").read_text("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("version"), str):
        raise ValueError("Некорректный манифест программы")
    version(data["version"])
    files = data.get("files")
    if not isinstance(files, dict) or not files or "MeetingArchive.exe" not in files:
        raise ValueError("В сборке отсутствует манифест программы")
    seen = set()
    for name, digest in files.items():
        safe_relative(name)
        if name.casefold() in seen or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Некорректный манифест программы")
        seen.add(name.casefold())
    return data


def stage_archive(archive: Path, target: Path, expected_version: str) -> dict:
    # Never extract arbitrary ZIP paths, including links or user data.
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        names = [i.orig_filename for i in infos]
        if len({n.casefold() for n in names}) != len(names):
            raise ValueError("Повторяющиеся файлы в сборке")
        for item in infos:
            safe_relative(item.orig_filename.rstrip("/"))
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Ссылки в сборке запрещены")
        data = json.loads(bundle.read("MeetingArchive/update-manifest.json"))
        if version(data["version"]) != version(expected_version):
            raise ValueError("Версия сборки не совпадает с релизом")
        # Validate manifest before any payload is written.
        target.mkdir(parents=True, exist_ok=True)
        atomic_json(target / "update-manifest.json", data)
        read_manifest(target)
        total = sum(bundle.getinfo("MeetingArchive/" + safe_relative(n)).file_size for n in data["files"])
        if total > 1024**3 or shutil.disk_usage(target).free < total * 2:
            raise ValueError("Недостаточно места для распаковки обновления")
        for name, digest in data["files"].items():
            content = bundle.read("MeetingArchive/" + name)
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("Контрольная сумма файла сборки не совпадает")
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    return data


class UpdateManager:
    def __init__(self, service):
        self.service = service
        self.root = service.home / "updates"
        self.state = {"installed": __version__, "state": "idle", "available": "", "progress": 0,
                      "checked_at": 0, "retry_at": 0, "error": "", "size": 0, "release_url": ""}
        saved = self.root / "state.json"
        if saved.exists():
            try:
                self.state.update(json.loads(saved.read_text("utf-8")))
            except (ValueError, OSError):
                pass
        self.state.update(installed=__version__, state="idle", error="")
        for key in ("checked_at", "retry_at", "size", "progress"):
            if not isinstance(self.state[key], (int, float)):
                self.state[key] = 0
        try:
            if self.state["available"]:
                version(self.state["available"])
        except (ValueError, TypeError):
            self.state["available"] = ""
        self.release = None
        self.lock = asyncio.Lock()
        self.task = None
        self.install_task = None
        self.shutdown = None
        self.waiting = False
        if self.state["available"] and version(self.state["available"]) > version(__version__):
            staged = self.root / self.state["available"] / "program"
            try:
                manifest = read_manifest(staged)
                if all(hashlib.sha256((staged / n).read_bytes()).hexdigest() == d for n, d in manifest["files"].items()):
                    self.state["state"] = "ready"
            except (ValueError, OSError, KeyError):
                pass
        receipt = self.root / "receipt.json"
        if receipt.exists():
            try:
                result = json.loads(receipt.read_text("utf-8-sig"))
                if result["state"] in {"error", "rolled_back"}:
                    self.state.update(state="error", error="Обновление не установлено: " + result.get("error", ""))
            except (ValueError, OSError, KeyError):
                pass

    def save(self):
        atomic_json(self.root / "state.json", self.state)

    def status(self):
        return {**self.state, "waiting": self.waiting, "supported": bool(getattr(sys, "frozen", False))}

    def launch_check(self, download=False):
        if self.waiting or (self.task and not self.task.done()):
            raise ValueError("Обновление уже выполняется")
        self.task = asyncio.create_task(self.check(download))

    async def loop(self):
        while self.service.alive:
            if (self.service.settings.auto_update and not self.waiting
                    and time.time() >= max(self.state["checked_at"] + 86400, self.state["retry_at"])):
                await self.check(True)
            await asyncio.sleep(60)

    @staticmethod
    def asset_url(asset):
        url = asset["browser_download_url"]
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username
                or not parsed.path.startswith("/" + REPOSITORY + "/releases/download/")):
            raise ValueError("Неожиданный адрес сборки")
        return url

    async def check(self, download=False):
        if self.lock.locked() or time.time() < self.state["retry_at"]:
            return
        async with self.lock:
            try:
                self.state.update(state="checking", error="")
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    response = await client.get(f"https://api.github.com/repos/{REPOSITORY}/releases/latest")
                    if response.status_code in {403, 429}:
                        retry = response.headers.get("retry-after", "3600")
                        try:
                            delay = max(60, int(retry))
                        except ValueError:
                            from email.utils import parsedate_to_datetime
                            delay = max(60, int(parsedate_to_datetime(retry).timestamp() - time.time()))
                        self.state["retry_at"] = time.time() + delay
                    response.raise_for_status()
                    release = response.json()
                    self.state["checked_at"] = time.time()
                    self.state["retry_at"] = 0
                    if release["draft"] or release["prerelease"] or version(release["tag_name"]) <= version(__version__):
                        self.state.update(state="current", available="")
                        return
                    assets = {a["name"]: a for a in release["assets"] if a["state"] == "uploaded"}
                    if ASSET not in assets or ASSET + ".sha256" not in assets:
                        raise ValueError("Релиз ещё не содержит полной сборки")
                    self.release = assets
                    self.state.update(state="available", available=release["tag_name"], size=assets[ASSET]["size"],
                                      release_url=f"https://github.com/{REPOSITORY}/releases/tag/{release['tag_name']}")
                    if download:
                        await self.download(client)
            except (ValueError, TypeError, KeyError, OSError, httpx.HTTPError, zipfile.BadZipFile) as exc:
                self.state.update(state="error", error=self.service.vault.redact(str(exc)))
                self.state["retry_at"] = max(self.state["retry_at"], time.time() + 300)
            finally:
                self.save()

    async def download(self, client):
        self.root.mkdir(parents=True, exist_ok=True)
        size = self.state["size"]
        if size <= 0 or size > 512 * 1024**2 or shutil.disk_usage(self.root).free < size * 5:
            raise ValueError("Недостаточно места или недопустимый размер обновления")
        response = await client.get(self.asset_url(self.release[ASSET + ".sha256"]))
        response.raise_for_status()
        checksum = response.text.strip().split()
        if len(checksum) != 2 or checksum[1] != ASSET or not re.fullmatch(r"[a-f0-9]{64}", checksum[0]):
            raise ValueError("Некорректная контрольная сумма обновления")
        folder = self.root / self.state["available"]
        folder.mkdir(exist_ok=True)
        archive = folder / "download.zip"
        digest, received = hashlib.sha256(), 0
        self.state.update(state="downloading", progress=0)
        async with client.stream("GET", self.asset_url(self.release[ASSET])) as stream:
            stream.raise_for_status()
            with archive.open("wb") as output:
                async for block in stream.aiter_bytes():
                    received += len(block)
                    if received > size:
                        raise ValueError("Размер сборки не совпадает")
                    digest.update(block)
                    output.write(block)
                    self.state["progress"] = received / size
        if received != size or digest.hexdigest() != checksum[0]:
            archive.unlink(missing_ok=True)
            raise ValueError("Контрольная сумма обновления не совпадает")
        await asyncio.to_thread(stage_archive, archive, folder / "program", self.state["available"])
        self.state.update(state="ready", progress=1)

    def request_install(self):
        if not getattr(sys, "frozen", False) or not self.shutdown:
            raise ValueError("Установка доступна в собранном Windows-приложении")
        if self.state["state"] != "ready" or self.waiting:
            raise ValueError("Проверенная сборка ещё не готова")
        self.waiting = True
        self.install_task = asyncio.create_task(self.install_when_idle())

    def cancel_install(self):
        if self.state["state"] == "installing":
            raise ValueError("Замена файлов уже началась")
        if self.install_task:
            self.install_task.cancel()
        self.waiting = False

    async def install_when_idle(self):
        try:
            while (self.service.active_tasks or self.service.catalogue["running"] or self.service.module_maintenance
                   or self.service.module.processes or self.service.module.process
                   or (self.service.module.reuse_task and not self.service.module.reuse_task.done())):
                await asyncio.sleep(.5)
            target = Path(sys.executable).resolve().parent
            staged = self.root / self.state["available"] / "program"
            old, new = read_manifest(target), read_manifest(staged)
            protected = [self.service.home, Path(self.service.settings.archive_root), Path(self.service.settings.watch_folder)]
            for name in set(old["files"]) | set(new["files"]) | {"update-manifest.json"}:
                dest = target / name
                parents = [dest, *dest.parents]
                if (any(p.is_symlink() or p.is_junction() for p in parents if p.is_relative_to(target))
                        or not dest.resolve().is_relative_to(target)
                        or any(dest.resolve().is_relative_to(p.resolve()) for p in protected)):
                    raise ValueError("Файл программы пересекается с пользовательскими данными")
                if dest.exists() and name not in old["files"] and name != "update-manifest.json":
                    raise ValueError("Обновление затронуло бы неизвестный пользовательский файл")
            if shutil.disk_usage(target).free < sum((staged / n).stat().st_size for n in new["files"]) * 2:
                raise ValueError("Недостаточно места для установки и отката")
            probe = target / (".update-write-" + str(os.getpid()))
            probe.write_bytes(b"")
            probe.unlink()
            config = {"target": str(target), "staged": str(staged), "home": str(self.service.home),
                      "pid": os.getpid(), "version": self.state["available"].lstrip("v"),
                      "old_files": sorted(old["files"]), "new_files": sorted(new["files"])}
            atomic_json(self.root / "install.json", config)
            helper = self.root / "apply-update.ps1"
            shutil.copy2(Path(__file__).parent / "resources/apply-update.ps1", helper)
            subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
                              "-File", str(helper), "-Config", str(self.root / "install.json")],
                             creationflags=subprocess.CREATE_NO_WINDOW)
            self.state["state"] = "installing"
            self.save()
            self.shutdown()
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError, KeyError) as exc:
            self.state.update(state="error", error=self.service.vault.redact(str(exc)))
            self.waiting = False
            self.save()

    async def stop(self):
        for task in (self.task, self.install_task):
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
