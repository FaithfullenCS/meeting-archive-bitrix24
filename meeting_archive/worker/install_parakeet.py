"""Pinned, cancellable native Windows Parakeet installation (no pip/CUDA setup).

Sources: NVIDIA/NeMo-Speech.cpp v0.1.0 release assets and its models/index.json.
The archive includes the native runtime libraries; the NVIDIA driver remains
system-owned. Neither shell installers nor remote Python code are executed.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import urlparse

VERSION = "0.1.0"
MODEL_ID = "parakeet-tdt-0.6b-v3-q8"
MODEL_FILE = "parakeet-tdt-0.6b-v3.q8_0.gguf"
MODEL_REVISION = "541d1f99c6b0c3cd0b11a95167540bb8edefd82b"
MODEL_SHA256 = "e3880d0aaaaf2c308ea2c35016b2b895c423eb3fda924c1b463d1c19b7f4d32e"
MODEL_BYTES = 713975456
MODEL_URL = f"https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3/resolve/{MODEL_REVISION}/{MODEL_FILE}"
RUNTIMES = {
    "cuda": {"size": 106044768, "sha256": "ba024204e76ca2fa4eefa8787506c3c49e418147f627f60cf9206a582b60089c"},
    "cpu": {"size": 4730421, "sha256": "5e4ea81046012edcd77fd8848de8eefb5a4ba38cc26f52eb544ab184695a75d6"},
}
MARKER = ".meeting-archive-parakeet"


class ParakeetInstallCancelled(RuntimeError):
    pass


def _cancel(cancelled):
    if cancelled():
        raise ParakeetInstallCancelled("Установка Parakeet отменена")


def _reparse(path):
    try:
        attributes = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(attributes, "st_file_attributes", 0) &
                                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _checked(root: Path, *, walk: bool = True) -> Path:
    root = root.expanduser().absolute()
    if any(_reparse(p) for p in (root, *root.parents)):
        raise ValueError("Папка Parakeet содержит внешнюю ссылку")
    root = root.resolve()
    if root.exists() and walk:
        for current, dirs, files in os.walk(root, followlinks=False):
            if any(_reparse(Path(current) / name) for name in (*dirs, *files)):
                raise ValueError("Папка Parakeet содержит ссылку; операция остановлена")
    return root


def _hash(path: Path, cancelled=lambda: False) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024**2):
            _cancel(cancelled)
            digest.update(data)
    return digest.hexdigest()


def _verified(path: Path, size: int, digest: str, cancelled=lambda: False) -> bool:
    return path.is_file() and not _reparse(path) and path.stat().st_size == size and _hash(path, cancelled) == digest


def _download(url: str, destination: Path, size: int, digest: str,
              progress: Callable[[float, str], None], cancelled: Callable[[], bool]) -> None:
    import httpx
    if urlparse(url).scheme != "https":
        raise ValueError("Модели загружаются только через HTTPS")
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    count, sha, last = 0, hashlib.sha256(), 0.0
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, connect=20)) as client:
            with client.stream("GET", url) as response:
                if any(r.url.scheme != "https" for r in (*response.history, response)):
                    raise ValueError("Сервер перенаправил загрузку на небезопасный адрес")
                response.raise_for_status()
                with temporary.open("xb") as stream:
                    for block in response.iter_bytes(1024**2):
                        _cancel(cancelled)
                        count += len(block)
                        if count > size:
                            raise ValueError("Размер загруженного файла не соответствует закреплённой версии")
                        stream.write(block)
                        sha.update(block)
                        if time.monotonic() - last > .2:
                            progress(count / size, "Загрузка закреплённого пакета Parakeet")
                            last = time.monotonic()
                    stream.flush()
                    os.fsync(stream.fileno())
        _cancel(cancelled)
        if count != size or sha.hexdigest() != digest:
            raise ValueError("Проверка размера или SHA-256 Parakeet не прошла")
        os.replace(temporary, destination)
    except httpx.HTTPError as exc:
        # Do not log signed CDN redirects or response bodies.
        raise RuntimeError("Не удалось загрузить Parakeet. Проверьте сеть и повторите установку") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _extract(archive: Path, destination: Path, cancelled: Callable[[], bool]) -> Path:
    destination.mkdir(parents=True)
    expanded = 0
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            _cancel(cancelled)
            relative = PurePosixPath(info.filename.replace("\\", "/"))
            mode = info.external_attr >> 16
            if (relative.is_absolute() or ".." in relative.parts or ":" in info.filename or
                    stat.S_ISLNK(mode)):
                raise ValueError("Архив Parakeet содержит небезопасный путь или ссылку")
            expanded += info.file_size
            if expanded > 2 * 1024**3:
                raise ValueError("Распакованный архив Parakeet превышает допустимый размер")
            path = destination / Path(*relative.parts)
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, path.open("xb") as target:
                while data := source.read(1024**2):
                    _cancel(cancelled)
                    target.write(data)
    executables = list(destination.rglob("nemo-speech.exe"))
    if len(executables) != 1:
        raise ValueError("В архиве не найден единственный официальный nemo-speech.exe")
    return executables[0]


def install_parakeet(root: Path, profile: str, progress: Callable[[float, str], None],
                     cancelled: Callable[[], bool]) -> dict:
    if profile not in RUNTIMES:
        raise ValueError("Выберите CUDA или явно подтверждённый CPU")
    _cancel(cancelled)
    root = _checked(Path(root), walk=False)
    if _reparse(root / ".meeting-archive-owned"):
        raise ValueError("Маркер принадлежности модуля является внешней ссылкой")
    if root.exists() and any(root.iterdir()) and not (root / ".meeting-archive-owned").is_file():
        raise ValueError("Папка модуля не принадлежит Meeting Archive")
    root.mkdir(parents=True, exist_ok=True)
    (root / ".meeting-archive-owned").touch(exist_ok=True)
    engine = _checked(root / "engines/parakeet")
    if engine.exists() and any(engine.iterdir()) and not (engine / MARKER).is_file():
        raise ValueError("Папка Parakeet содержит чужие файлы")
    engine.mkdir(parents=True, exist_ok=True)
    (engine / MARKER).touch(exist_ok=True)
    artifact = RUNTIMES[profile]
    filename = f"nemo-speech-{VERSION}-windows-x86_64-{profile}.zip"
    runtime_url = f"https://github.com/NVIDIA/NeMo-Speech.cpp/releases/download/v{VERSION}/{filename}"
    model = engine / "models" / MODEL_FILE
    have_model = _verified(model, MODEL_BYTES, MODEL_SHA256, cancelled)
    runtime = engine / ("runtime-" + profile)
    metadata = engine / ("ready-" + profile + ".json")
    if metadata.is_file() and have_model:
        existing = json.loads(metadata.read_text("utf-8"))
        binary = Path(existing.get("runtime_path", ""))
        if binary.is_relative_to(runtime) and binary.is_file() and existing.get("runtime_version") == VERSION:
            progress(1, "Parakeet выбранного профиля уже установлен")
            return existing
    required = artifact["size"] * 4 + (0 if have_model else MODEL_BYTES * 2) + 128 * 1024**2
    if shutil.disk_usage(engine).free < required:
        raise ValueError(f"Для установки Parakeet нужно примерно {required / 1024**3:.2f} ГБ свободного места")
    stage = engine / (".install-" + uuid.uuid4().hex)
    stage.mkdir()
    published, backed_up, added_model = False, False, False
    try:
        progress(0, "Установка Parakeet без Python/PyTorch/CUDA Toolkit")
        downloads = engine / "downloads"
        downloads.mkdir(exist_ok=True)
        archive = downloads / filename
        if not _verified(archive, artifact["size"], artifact["sha256"], cancelled):
            _download(runtime_url, archive, artifact["size"], artifact["sha256"],
                      lambda p, m: progress(p * .14, "Загрузка native runtime"), cancelled)
        executable = _extract(archive, stage / "runtime", cancelled)
        relative_binary = executable.relative_to(stage / "runtime")
        if not have_model:
            _download(MODEL_URL, stage / MODEL_FILE, MODEL_BYTES, MODEL_SHA256,
                      lambda p, m: progress(.14 + p * .8, "Загрузка Parakeet Q8 · 714 МБ"), cancelled)
        _cancel(cancelled)
        if runtime.exists():
            os.replace(runtime, stage / "old-runtime")
            backed_up = True
        os.replace(stage / "runtime", runtime)
        published = True
        model.parent.mkdir(exist_ok=True)
        if not have_model:
            # An invalid old model is retained for rollback until publication ends.
            if model.exists():
                os.replace(model, stage / "old-model")
            os.replace(stage / MODEL_FILE, model)
            added_model = True
        _cancel(cancelled)
        ready = {"engine": "parakeet", "model": MODEL_ID, "profile": profile, "installedAt": time.time(),
                 "runtime_version": VERSION, "runtime_path": str(runtime / relative_binary),
                 "model_path": str(model), "model_revision": MODEL_REVISION,
                 "model_sha256": MODEL_SHA256, "model_bytes": MODEL_BYTES, "runtime_sha256": artifact["sha256"],
                 "license": "CC-BY-4.0", "runtime_license": "Apache-2.0", "gpu_verified": False,
                 "pause_detection": "alignment-gaps", "vad_preprocessing": False}
        license_note = ("Parakeet TDT 0.6B v3 Q8 — NVIDIA, CC-BY-4.0\n"
                        "https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3\n"
                        "Native runtime — NVIDIA/NeMo-Speech.cpp v0.1.0, Apache-2.0\n"
                        "https://github.com/NVIDIA/NeMo-Speech.cpp/releases/tag/v0.1.0\n")
        (engine / "MODEL-SOURCES.txt").write_text(license_note, encoding="utf-8")
        pending = stage / "ready.json"
        pending.write_text(json.dumps(ready, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(pending, metadata)
        progress(1, "Parakeet установлен; видеокарта проверяется перед обработкой")
        return ready
    except BaseException:
        if published and runtime.exists():
            shutil.rmtree(runtime)
        if backed_up and (stage / "old-runtime").exists():
            os.replace(stage / "old-runtime", runtime)
        if added_model and model.exists():
            model.unlink()
        if (stage / "old-model").exists():
            os.replace(stage / "old-model", model)
        raise
    finally:
        if stage.exists() and stage.parent == engine and stage.name.startswith(".install-"):
            shutil.rmtree(stage)


def installed_paths(root: Path, profile: str) -> tuple[Path, Path]:
    if profile not in RUNTIMES:
        raise ValueError("Неизвестный профиль Parakeet")
    engine = _checked(Path(root) / "engines/parakeet")
    metadata = engine / ("ready-" + profile + ".json")
    if not metadata.is_file() or metadata.stat().st_size > 2 * 1024**2:
        raise RuntimeError("Сначала установите Parakeet выбранного профиля в каталоге моделей")
    ready = json.loads(metadata.read_text("utf-8"))
    binary, model = Path(ready["runtime_path"]), Path(ready["model_path"])
    if (not binary.is_absolute() or not model.is_absolute() or not binary.is_relative_to(engine) or
            not model.is_relative_to(engine) or not binary.is_file() or not model.is_file()):
        raise RuntimeError("Файлы установленного Parakeet недоступны; повторите установку")
    return binary, model


def probe_parakeet(root: Path, profile: str = "cuda") -> dict:
    """Read-only native doctor; no model loading, downloads or Torch imports."""
    result = {"engine": "parakeet", "profile": profile, "runtime": "NeMo-Speech.cpp", "cuda": False,
              "compatible": False, "version": VERSION, "devices": [], "reason": ""}
    try:
        binary, _ = installed_paths(root, profile)
        environment = os.environ.copy()
        environment["PATH"] = str(binary.parent) + os.pathsep + environment.get("PATH", "")
        output = subprocess.run([str(binary), "doctor", "--json"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=20, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if len(output.stdout) > 2 * 1024**2:
            raise RuntimeError("Диагностика Parakeet вернула слишком большой ответ")
        data = json.loads(output.stdout)
        compiled = bool(data.get("features", {}).get("backend_cuda"))
        for device in data.get("devices", [])[:32]:
            result["devices"].append({"type": str(device.get("type", "unknown"))[:30],
                "name": str(device.get("name", ""))[:100], "description": str(device.get("description", ""))[:160],
                "memory_total": max(0, int(device.get("memory_total", 0))),
                "memory_free": max(0, int(device.get("memory_free", 0)))})
        gpu = any(device["type"] == "gpu" for device in result["devices"])
        result.update(version=str(data.get("version", VERSION))[:30], cuda=compiled and gpu)
        result["compatible"] = output.returncode == 0 and (result["cuda"] if profile == "cuda" else True)
        result["reason"] = ("Native CUDA доступна" if profile == "cuda" and result["compatible"]
                            else "Явно выбранный CPU доступен" if result["compatible"]
                            else "Native CUDA/драйвер недоступны. Автоматический переход на CPU отключён")
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as exc:
        result["reason"] = (str(exc) if isinstance(exc, RuntimeError) else
                            "Диагностика Parakeet не прошла; повторите установку и проверьте драйвер")
    return result
