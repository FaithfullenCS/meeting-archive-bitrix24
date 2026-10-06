from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import tempfile
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path
from .scheduling import default_schedule


def app_home() -> Path:
    return Path(os.environ.get("MEETING_ARCHIVE_HOME", str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MeetingArchive")))


def documents_folder() -> Path:
    if os.name == "nt":
        buffer = ctypes.create_unicode_buffer(32768)
        # Use the actual Documents folder, including OneDrive/redirection.
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0:
            return Path(buffer.value)
    return Path.home() / "Documents"


def default_files(home: Path | None = None) -> Path:
    if home is not None and home.resolve() != app_home().resolve():
        # Isolated portable/test profiles keep their defaults beside the profile.
        return home.parent / (home.name + "-files")
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "Данные"
    return documents_folder() / "Meeting Archive"


def default_archive(home: Path | None = None) -> str:
    return str(default_files(home) / "Совещания")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def portal_domain(value: str) -> str:
    from urllib.parse import urlsplit
    value = value.strip().lower()
    parsed = urlsplit(value if "://" in value else "https://" + value)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError("Укажите HTTPS-адрес вашего облачного портала Bitrix24")
    if not re.fullmatch(r"[a-z0-9-]+\.bitrix24\.(ru|com|by|kz|ua|eu|de|es|pl|it|fr|com\.br|in|cn|vn)", host):
        raise ValueError("Первая версия поддерживает облачные порталы вида название.bitrix24.ru")
    return host


@dataclass
class Settings:
    archive_root: str = ""
    chat_archive_root: str = ""
    chat_auto_save: bool = False
    chat_scope: str = "all"
    chat_selected_ids: list[int] = field(default_factory=list)
    chat_excluded_ids: list[int] = field(default_factory=list)
    chat_history_since: str = ""
    chat_poll_seconds: int = 60
    chat_events: bool = False
    chat_download_images: bool = False
    chat_download_documents: bool = False
    chat_download_audio: bool = False
    chat_download_video: bool = False
    chat_download_other: bool = False
    chat_download_history: bool = False
    chat_attachment_schedule: dict = field(default_factory=default_schedule)
    chat_max_file_mb: int = 0
    portal: str = ""
    auth_mode: str = "oauth"
    user_id: int = 0
    member_id: str = ""
    oauth_relay: str = ""
    oauth_flow: str = "local"
    auto_download: bool = False
    auto_download_audio: bool = False
    auto_local: bool = False
    auto_since: str = ""
    download_schedule: dict = field(default_factory=default_schedule)
    local_schedule: dict = field(default_factory=default_schedule)
    watch_enabled: bool = False
    watch_folder: str = ""
    paused: bool = False
    model: str = "large-v3"
    engine: str = "whisper"
    parakeet_model: str = "parakeet-tdt-0.6b-v3-q8"
    gigaam_model: str = "gigaam-v3-e2e-rnnt"
    language: str = "auto"
    device: str = "cuda"
    cpu_confirmed: bool = False
    vad: bool = True
    noise_reduction: bool = False
    normalize: bool = False
    diarization: bool = False
    min_speakers: int = 0
    max_speakers: int = 0
    external_engine: str = ""
    autostart: bool = False
    auto_update: bool = True
    notifications_enabled: bool = True
    notify_download: bool = True
    notify_transcription: bool = True
    notify_errors: bool = True

    @classmethod
    def load(cls, home: Path) -> Settings:
        path = home / "settings.json"
        data = json.loads(path.read_text("utf-8")) if path.exists() else {}
        result = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        if result.auto_local:
            result.auto_download_audio = True
        if not result.archive_root:
            result.archive_root = default_archive(home)
            Path(result.archive_root).mkdir(parents=True, exist_ok=True)
        if not result.chat_archive_root:
            result.chat_archive_root = str(Path(result.archive_root).parent / "Чаты")
        if not result.watch_folder:
            result.watch_folder = str(default_files(home) / "Входящие записи")
            Path(result.watch_folder).mkdir(parents=True, exist_ok=True)
        # Recreate default directories if they were removed, without changing
        # explicitly selected paths or moving an existing archive.
        for value, name in ((result.archive_root, "Совещания"), (result.watch_folder, "Входящие записи")):
            if Path(value) == default_files(home) / name:
                Path(value).mkdir(parents=True, exist_ok=True)
        return result

    def save(self, home: Path) -> None:
        atomic_json(home / "settings.json", asdict(self))


class Vault:
    """Per-Windows-user DPAPI; deliberately no plaintext fallback."""

    def __init__(self, home: Path):
        self.path = home / "secrets.dpapi"

    @staticmethod
    def _crypt(data: bytes, decrypt: bool = False) -> bytes:
        if os.name != "nt":
            raise RuntimeError("Хранилище секретов доступно только в Windows (DPAPI)")

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        source, result = Blob(len(data), buffer), Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        func = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        func.restype = wintypes.BOOL
        func.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        if not func(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise OSError(ctypes.get_last_error(), "Не удалось открыть защищённое хранилище Windows")
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(result.pbData)

    def read(self) -> dict:
        return json.loads(self._crypt(self.path.read_bytes(), True)) if self.path.exists() else {}

    def write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._crypt(json.dumps(value).encode())
        temp = self.path.with_suffix(".tmp")
        temp.write_bytes(payload)
        os.replace(temp, self.path)

    def update(self, **values) -> None:
        self.write({**self.read(), **values})

    def redact(self, value: str) -> str:
        try:
            secrets = self.read()
        except Exception:
            secrets = {}
        for key in ("access_token", "refresh_token", "client_secret", "webhook", "hf_token", "ui_session"):
            secret = secrets.get(key)
            if secret:
                value = value.replace(str(secret), "[скрыто]")
        value = re.sub(r"https?://\S+", "[адрес скрыт]", value)
        return value[:800]
