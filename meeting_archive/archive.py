from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from .bitrix import BitrixClient, BitrixError
from .settings import atomic_json

META_KEYS = {"callId", "uuid", "callType", "chatId", "initiatorId", "startDate", "endDate", "durationSeconds", "outcomes", "createdAt", "version"}


def followup_state(item: dict, *, saved=False) -> str | None:
    """Infer availability, not an API-confirmed reason for rejection."""
    if saved:
        return "saved"
    transcription = item.get("transcription") or {}
    if isinstance(transcription, dict) and any(
        isinstance(segment, dict) and str(segment.get("text") or "").strip()
        for segment in transcription.get("segments") or []
    ):
        return "available"
    if "transcription" in (item.get("outcomes") or []) or item.get("availability", {}).get("bitrix") == "available":
        return "available"
    duration = item.get("durationSeconds")
    if (item.get("endDate") and isinstance(duration, (int, float)) and not isinstance(duration, bool)
            and math.isfinite(duration) and 0 < duration < 60):
        return "short_call"
    return "waiting" if "outcomes" in item else None


def clean_metadata(item: dict) -> dict:
    result = {k: v for k, v in item.items() if k in META_KEYS}
    result["chatId"] = item.get("chatId")
    result["participants"] = [{k: p[k] for k in ("userId", "name", "talkedSeconds", "workPosition") if k in p}
                              for p in item.get("participants", []) if isinstance(p, dict)]
    result["overview"] = {"topic": (item.get("overview") or {}).get("topic", "")}
    availability = {}
    state = followup_state(item)
    if state:
        availability["bitrix"] = state
    if "tracks" in item:
        availability["audio"] = "available" if item.get("tracks") else "not_available"
    if availability:
        result["availability"] = availability
    return result


def safe_name(value: str, limit=90) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:limit].rstrip(" .")
    if not value or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", value):
        value = "file_" + value
    return value


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def atomic_text(path: Path, text: str):
    atomic_bytes(path, text.encode("utf-8"))


def transcript_digest(path: Path) -> str:
    # Legacy Windows saves used CRLF but named revisions from the LF payload.
    return hashlib.sha256(path.read_text("utf-8").encode("utf-8")).hexdigest()


class Archive:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def contained(self, path: Path, *, within: Path | None = None) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root) or (within is not None and not resolved.is_relative_to(within.resolve())):
            raise ValueError("Путь материалов выходит за пределы архива. Проверьте внешние ссылки в папке")
        return resolved

    def folder(self, meeting: dict) -> Path:
        if meeting.get("folder"):
            folder = Path(meeting["folder"]).resolve()
            if not folder.is_relative_to(self.root):
                raise ValueError("Архив этой записи находится в прежней папке. Верните прежний корень архива")
            return folder
        item = json.loads(meeting["metadata"])
        date = datetime.fromisoformat((item.get("startDate") or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00"))
        date = date.astimezone()
        session = meeting.get("uuid") or ""
        identifier = safe_name(str(meeting["call_id"])) + ("_" + safe_name(session) if session else "")
        return self.contained(self.root / safe_name(meeting["portal"]) / date.strftime("%Y") /
                              date.strftime("%m") / (date.strftime("%Y-%m-%d_%H%M%S") + "_" + identifier))

    def ensure(self, meeting: dict) -> Path:
        folder = self.folder(meeting)
        for name in ("audio", "bitrix", "local", "notes"):
            self.contained(folder / name, within=folder).mkdir(parents=True, exist_ok=True)
        return folder

    def manifest(self, meeting: dict):
        folder = self.ensure(meeting)
        manifest = self.contained(folder / "meeting.json", within=folder)
        old = json.loads(manifest.read_text("utf-8")) if manifest.exists() else {}
        files = {name: value for name, value in old.get("files", {}).items()
                 if not any(part.startswith(".") for part in Path(name).parts) and (folder / name).is_file()}
        for name in ("bitrix", "local"):
            for path in (folder / name).rglob("*"):
                if path.is_file() and not any(part.startswith(".") for part in path.relative_to(folder).parts):
                    self.contained(path, within=folder)
                    files[path.relative_to(folder).as_posix()] = {"sha256": sha256(path), "size": path.stat().st_size}
        # Audio hashes are recorded while streaming to avoid re-reading huge recordings on every poll.
        atomic_json(folder / "meeting.json", {"schemaVersion": 1, "source": meeting["source"], "portal": meeting["portal"],
            "callId": meeting["call_id"], "uuid": meeting["uuid"], "metadata": json.loads(meeting["metadata"]),
            "states": {k: meeting[k] for k in ("audio", "bitrix", "local")}, "files": files,
            "reading": self.reading_paths(folder, meeting["portal"]),
            "updatedAt": datetime.now(timezone.utc).isoformat(), "quality": "Качество на вашей записи не проверено"})

    def reading_paths(self, folder: Path, portal: str) -> dict:
        local = None
        for marker in sorted((folder / "local").glob("*/run.json"), reverse=True):
            if marker.parent.name.startswith("."):
                continue
            self.contained(marker, within=folder)
            try:
                run = json.loads(marker.read_text("utf-8"))
            except (ValueError, OSError):
                continue
            transcript = self.contained(marker.parent / "transcript.md", within=folder)
            if isinstance(run, dict) and run.get("sample") is False and transcript.is_file():
                local = transcript.relative_to(folder).as_posix()
                break
        notebook = self.contained(self.root / "_notebook" / safe_name(portal))
        return {"format": "markdown", "followup": "bitrix/transcript.md" if (folder / "bitrix/transcript.md").is_file() else None,
                "local": local, "notebook": Path(os.path.relpath(notebook, folder)).as_posix()}

    def prune_current_copy(self, folder: Path, *, dry_run=False) -> list[Path]:
        """Remove only a byte-identical, complete legacy copy of the current text."""
        target = self.contained(folder / "bitrix", within=folder)
        names = {"transcript.json", "transcript.txt", "transcript.md"}
        current = [self.contained(target / name, within=folder) for name in sorted(names)]
        if not all(path.is_file() for path in current):
            return []
        versions = target / "versions"
        if not versions.is_dir() or versions.is_symlink() or versions.is_junction():
            return []
        version = versions / transcript_digest(target / "transcript.json")[:16]
        if not version.is_dir() or version.is_symlink() or version.is_junction():
            return []
        self.contained(version, within=folder)
        copies = sorted(version.iterdir())
        if {path.name for path in copies} != names or any(not path.is_file() or path.is_symlink() for path in copies):
            return []
        for path in copies:
            self.contained(path, within=folder)
            if path.read_bytes() != (target / path.name).read_bytes():
                return []
        if not dry_run:
            for path in copies:
                path.unlink()
            version.rmdir()
            if not any(versions.iterdir()):
                versions.rmdir()
        return copies

    def remember_audio(self, folder: Path, path: Path, *, track_key="", digest="", size=0):
        self.contained(path, within=folder)
        manifest = self.contained(folder / "meeting.json", within=folder)
        data = json.loads(manifest.read_text("utf-8")) if manifest.exists() else {"files": {}}
        data.setdefault("files", {})[path.relative_to(folder).as_posix()] = {"sha256": digest or sha256(path),
            "size": size or path.stat().st_size, "trackKey": track_key, "mtime_ns": path.stat().st_mtime_ns}
        atomic_json(manifest, data)

    def transcript(self, folder: Path, transcription: dict) -> bool:
        segments = transcription.get("segments") or []
        if not segments:
            return False
        # Persist speech and speaker attribution only, never transport/auth fields
        # unexpectedly returned alongside transcription by the upstream service.
        if not isinstance(segments, list) or any(not isinstance(segment, dict) for segment in segments):
            raise ValueError("Неизвестный формат сегментов расшифровки Bitrix24")
        segments = [{key: segment[key] for key in ("start", "end", "userId", "userName", "text") if key in segment}
                    for segment in segments]
        transcription = {"segments": segments}
        payload = json.dumps(transcription, ensure_ascii=False, indent=2)
        target = self.contained(folder / "bitrix", within=folder)
        existing = self.contained(target / "transcript.json", within=folder)
        self.prune_current_copy(folder)
        if existing.is_file() and existing.read_text("utf-8") != payload:
            # Snapshot the previous text once, before replacing the current text.
            version = self.contained(target / "versions" / transcript_digest(existing)[:16], within=folder)
            for name in ("transcript.json", "transcript.txt", "transcript.md"):
                old = self.contained(target / name, within=folder)
                saved = self.contained(version / name, within=folder)
                if old.is_file():
                    if saved.exists() and saved.read_bytes() != old.read_bytes():
                        raise ValueError("Сохранённая версия текста отличается от исходной. Обновление остановлено")
                    if not saved.exists():
                        atomic_bytes(saved, old.read_bytes())
        lines = [f"[{s.get('start', '')} — {s.get('end', '')}] {s.get('userName') or 'Говорящий ' + str(s.get('userId', '?'))}: {s.get('text', '')}" for s in segments]
        txt = "\n\n".join(lines) + "\n"
        markdown = "# Расшифровка Bitrix24\n\nИсточник: BitrixGPT Follow-up. Может покрывать часть звонка.\n\n" + txt
        for name, content in (("transcript.json", payload), ("transcript.txt", txt), ("transcript.md", markdown)):
            path = self.contained(target / name, within=folder)
            if not path.exists() or path.read_text("utf-8") != content:
                atomic_text(path, content)
        self.prune_current_copy(folder)  # A return to an earlier revision makes its history copy redundant.
        return True

    async def download(self, client: BitrixClient, folder: Path, track: dict, progress) -> tuple[Path, bool]:
        folder = self.contained(folder)
        key = str(track.get("trackId") or track.get("fileId") or track.get("diskFileId") or "recording")
        filename = safe_name(key) + "_" + safe_name(track.get("fileName") or "recording.webm", 65)
        target = self.contained(folder / "audio" / filename, within=folder)
        expected = int(track.get("fileSize") or 0)
        manifest = self.contained(folder / "meeting.json", within=folder)
        ledger = json.loads(manifest.read_text("utf-8")).get("files", {}) if manifest.exists() else {}
        record = ledger.get(target.relative_to(folder).as_posix(), {})
        if target.exists() and record.get("trackKey") == key and target.stat().st_size == record.get("size") and (not expected or target.stat().st_size == expected):
            # Size and mtime can survive same-size corruption, especially on
            # Windows. Verify content before skipping a download; keep large
            # recording reads off the UI event loop.
            if record.get("sha256") and await asyncio.to_thread(sha256, target) == record["sha256"]:
                if record.get("mtime_ns") != target.stat().st_mtime_ns:
                    self.remember_audio(folder, target, track_key=key, digest=record["sha256"])
                return target, False
        free = shutil.disk_usage(folder).free
        if free < expected + 32 * 1024 * 1024:
            raise ValueError("Недостаточно места для записи. Освободите место в папке архива")
        url = await client.download_url(track)
        fd, temp = tempfile.mkstemp(prefix=".download-", dir=target.parent)
        digest, total, content_length = hashlib.sha256(), 0, 0
        try:
            with os.fdopen(fd, "wb") as stream:
                for _ in range(5):
                    async with client.http.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise BitrixError("В перенаправлении записи отсутствует адрес", retryable=True)
                            url = client.safe_download_url(urljoin(url, location))
                            continue
                        if response.status_code >= 400:
                            raise BitrixError(f"Запись: HTTP {response.status_code}. Адрес будет обновлён при повторе", retryable=True)
                        if response.headers.get("content-type", "").lower().startswith("text/") or "application/json" in response.headers.get("content-type", ""):
                            raise BitrixError("Вместо записи получена страница входа. Проверьте право disk", auth=True)
                        try:
                            content_length = int(response.headers.get("content-length", "0"))
                        except ValueError as exc:
                            raise BitrixError("Некорректный размер записи в ответе сервера", retryable=True) from exc
                        if content_length < 0:
                            raise BitrixError("Некорректный размер записи в ответе сервера", retryable=True)
                        if content_length > free - 32 * 1024 * 1024:
                            raise ValueError("Недостаточно места для записи")
                        if response.headers.get("content-encoding", "identity") != "identity":
                            content_length = 0  # Encoded wire length is not decoded audio length.
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            if (expected and total + len(chunk) > expected) or total + len(chunk) > free - 32 * 1024 * 1024:
                                raise BitrixError("Размер записи превысил ожидаемый. Скачивание остановлено", retryable=True)
                            stream.write(chunk)
                            digest.update(chunk)
                            total += len(chunk)
                            progress(min(total / expected, 0.99) if expected else 0, f"Запись: {total // 1024 // 1024} МБ")
                        break
                else:
                    raise BitrixError("Слишком много перенаправлений записи")
            if not total or (expected and total != expected) or (content_length and total != content_length):
                raise BitrixError("Размер записи не совпал. Повторим загрузку", retryable=True)
            os.replace(temp, target)
            self.remember_audio(folder, target, track_key=key, digest=digest.hexdigest(), size=total)
            return target, True
        finally:
            Path(temp).unlink(missing_ok=True)

    def import_file(self, meeting: dict, source: Path) -> tuple[Path, bool]:
        if not source.is_file():
            raise ValueError("Файл записи не найден")
        folder = self.ensure(meeting)
        digest = sha256(source)
        path = self.contained(folder / "audio" / (digest[:12] + "_" + safe_name(source.name, 65)), within=folder)
        if path.exists() and sha256(path) == digest:
            return path, False
        if shutil.disk_usage(folder).free < source.stat().st_size + 32 * 1024 * 1024:
            raise ValueError("Недостаточно места для импорта")
        fd, name = tempfile.mkstemp(prefix=".import-", dir=path.parent)
        os.close(fd)
        try:
            shutil.copyfile(source, name)
            if sha256(Path(name)) != digest:
                raise ValueError("Исходник изменился во время копирования. Повторите импорт после окончания записи")
            os.replace(name, path)
            self.remember_audio(folder, path, digest=digest)
        finally:
            Path(name).unlink(missing_ok=True)
        return path, True
