"""Install a pinned GigaAM engine into the application's isolated directory.

The shared worker and hash-pinned addon wheels must already be installed.
Only this explicit installer accesses the network; inference stays offline.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

try:
    from .windows_runtime import reset_dll_search
except ImportError:
    from windows_runtime import reset_dll_search


try:
    from .core.gigaam import MODEL_FILES, MODEL_NAME, MODEL_REVISION, SOURCE_COMMIT, SOURCE_SHA256, OFFICIAL_MODEL_FILES, OFFICIAL_CDN, sha256
except ImportError:
    from core.gigaam import MODEL_FILES, MODEL_NAME, MODEL_REVISION, SOURCE_COMMIT, SOURCE_SHA256, OFFICIAL_MODEL_FILES, OFFICIAL_CDN, sha256

SOURCE_URL = "https://codeload.github.com/salute-developers/GigaAM/zip/" + SOURCE_COMMIT
SOURCE_SIZE = 626844
SOURCE_FILES = {"__init__.py", "decoder.py", "decoding.py", "encoder.py", "model.py", "onnx_utils.py",
                "preprocess.py", "timestamps_utils.py", "types.py", "utils.py", "vad_utils.py"}


def emit(kind, **data):
    print(json.dumps({"type": kind, **data}, ensure_ascii=False), flush=True)


def checked_root(root: Path) -> Path:
    if not root.is_absolute():
        raise ValueError("Укажите абсолютную служебную папку GigaAM")
    for path in (root, *root.parents):
        try:
            attrs = path.lstat()
        except FileNotFoundError:
            continue
        if path.is_symlink() or getattr(attrs, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("Папка GigaAM содержит внешнюю ссылку")
    root.mkdir(parents=True, exist_ok=True)
    for name in ("model", "packages", "packages/gigaam"):
        child = root / name
        if child.exists() and (child.is_symlink() or not child.resolve().is_relative_to(root.resolve())):
            raise ValueError("Файлы GigaAM перенаправлены за служебную папку")
    return root.resolve()


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("Сервер загрузки GigaAM вернул небезопасный адрес")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class DownloadError(RuntimeError):
    """Safe user-facing failure: never includes signed URLs or remote bodies."""


def download(url: str, target: Path, size: int, digest: str, progress):
    if target.is_symlink():
        raise ValueError("Файл GigaAM содержит внешнюю ссылку")
    if target.is_file() and target.stat().st_size == size and sha256(target) == digest:
        progress(1, "Файл уже проверен: " + target.name)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    if partial.is_symlink():
        raise ValueError("Временный файл GigaAM содержит внешнюю ссылку")
    import certifi
    opener = urllib.request.build_opener(HTTPSRedirect(), urllib.request.HTTPSHandler(
        context=ssl.create_default_context(cafile=certifi.where())))
    for attempt in range(4):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == size:
            if sha256(partial) == digest:
                os.replace(partial, target)
                progress(1, "Файл проверен: " + target.name)
                return
            partial.unlink()
            offset = 0
        elif offset > size:
            partial.unlink()
            offset = 0
        headers = {"User-Agent": "MeetingArchive-GigaAM", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        reason = "соединение прервано"
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
                if response.status == 206:
                    expected = f"bytes {offset}-"
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith(expected) or not content_range.endswith(f"/{size}"):
                        raise ValueError("Сервер вернул неверный диапазон файла GigaAM")
                elif response.status == 200:
                    offset = 0  # A server may ignore Range; never append a full response.
                else:
                    raise DownloadError("Сервер не вернул файл GigaAM: " + target.name)
                total, last_progress = offset, 0.0
                with partial.open("ab" if offset else "wb") as output:
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > size:
                            raise ValueError("Размер файла GigaAM не соответствует закреплённой версии")
                        output.write(chunk)
                        if time.monotonic() - last_progress > .5:
                            progress(total / size, "Загрузка: " + target.name)
                            last_progress = time.monotonic()
                    output.flush()
                    os.fsync(output.fileno())
            if total != size:
                raise http.client.IncompleteRead(b"")
            if sha256(partial) != digest:
                raise ValueError("SHA-256 файла GigaAM не совпадает. Файл не установлен")
            os.replace(partial, target)
            progress(1, "Файл проверен: " + target.name)
            return
        except urllib.error.HTTPError as exc:
            reason = f"HTTP {exc.code}"
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                raise DownloadError(f"Не удалось загрузить {target.name}: {reason}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError, http.client.HTTPException) as exc:
            cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            reason = "тайм-аут" if isinstance(cause, TimeoutError) else "соединение прервано"
        except ValueError:
            partial.unlink(missing_ok=True)
            raise
        if attempt < 3:
            progress((partial.stat().st_size if partial.exists() else 0) / size,
                     f"{target.name}: {reason}; повтор {attempt + 2}/4")
            time.sleep(2 ** attempt)
    raise DownloadError(f"Не удалось загрузить {target.name}: {reason}. "
                        "Проверьте доступ к серверу загрузки модели. Скачанная часть сохранена; повтор продолжит загрузку.")


def extract_source(archive: Path, root: Path):
    if archive.stat().st_size != SOURCE_SIZE or sha256(archive) != SOURCE_SHA256:
        raise ValueError("Архив исходников GigaAM не прошёл проверку SHA-256")
    prefix = "GigaAM-" + SOURCE_COMMIT + "/gigaam/"
    sources = {}
    with zipfile.ZipFile(archive) as package:
        for info in package.infolist():
            if info.filename.startswith(prefix) and not info.is_dir():
                name = info.filename.removeprefix(prefix)
                if name not in SOURCE_FILES or name in sources or info.file_size > 1024 * 1024:
                    raise ValueError("Неизвестное содержимое архива GigaAM")
                sources[name] = package.read(info)
        if set(sources) != SOURCE_FILES:
            raise ValueError("В архиве GigaAM отсутствуют обязательные исходники")
        license_name = "GigaAM-" + SOURCE_COMMIT + "/LICENSE"
        license_data = package.read(license_name)
    destination = root / "packages/gigaam"
    destination.mkdir(parents=True, exist_ok=True)
    checksums = {}
    for name, content in sources.items():
        path = destination / name
        if path.is_symlink():
            raise ValueError("Папка исходников GigaAM содержит внешнюю ссылку")
        temporary = path.with_name(name + ".part")
        if temporary.is_symlink():
            raise ValueError("Временный исходник GigaAM содержит внешнюю ссылку")
        temporary.unlink(missing_ok=True)
        with temporary.open("xb") as stream:
            stream.write(content)
        os.replace(temporary, path)
        checksums[name] = hashlib.sha256(content).hexdigest()
    license_path = root / "GIGAAM-LICENSE.txt"
    if license_path.is_symlink():
        raise ValueError("Файл лицензии GigaAM содержит внешнюю ссылку")
    license_path.unlink(missing_ok=True)
    with license_path.open("xb") as stream:
        stream.write(license_data)
    return checksums


def install(root: Path, progress=lambda pct, message: emit("progress", progress=pct, message=message)):
    root = checked_root(root)
    required = sum(size for size, _ in MODEL_FILES.values()) + SOURCE_SIZE + 32 * 1024**2
    if shutil.disk_usage(root).free < required:
        raise ValueError("Для GigaAM нужно не менее 500 МБ свободного места")
    source = root / "source.zip"
    download(SOURCE_URL, source, SOURCE_SIZE, SOURCE_SHA256, lambda p, m: progress(p * .05, m))
    checksums = extract_source(source, root)
    # The official developer CDN avoids the Hugging Face Xet redirect path.
    # Reuse a complete existing HF model; new installs use pinned official bytes.
    files = MODEL_FILES if all((root / "model" / name).is_file() and
        (root / "model" / name).stat().st_size == size and sha256(root / "model" / name) == digest
        for name, (size, digest) in MODEL_FILES.items()) else OFFICIAL_MODEL_FILES
    for i, (name, (size, digest)) in enumerate(files.items()):
        target = root / "model" / name
        if name == "config.json":
            bundled = Path(__file__).resolve().parents[1] / "resources/gigaam-v3-config.json"
            if bundled.stat().st_size != size or sha256(bundled) != digest:
                raise ValueError("Встроенная конфигурация GigaAM не прошла SHA-256")
            if target.is_symlink():
                raise ValueError("Конфигурация GigaAM содержит внешнюю ссылку")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_config = target.with_name("config.json.part")
            if temporary_config.is_symlink():
                raise ValueError("Конфигурация GigaAM содержит внешнюю ссылку")
            temporary_config.write_bytes(bundled.read_bytes())
            os.replace(temporary_config, target)
            continue
        if files is OFFICIAL_MODEL_FILES:
            remote = "v3_e2e_rnnt_tokenizer.model" if name == "tokenizer.model" else name
            url = OFFICIAL_CDN + remote
        else:
            url = "https://huggingface.co/ai-sage/GigaAM-v3/resolve/" + MODEL_REVISION + "/" + name
        download(url, target, size, digest,
                 lambda p, m, i=i: progress(.05 + .9 * (i + p) / len(files), m))
    ready = {"model": MODEL_NAME, "source_commit": SOURCE_COMMIT, "source_sha256": SOURCE_SHA256,
             "model_revision": MODEL_REVISION, "source_files": checksums,
             "files": {name: {"size": size, "sha256": digest} for name, (size, digest) in files.items()},
             "timestamps": "chunk-boundaries", "language": "ru", "installedAt": time.time()}
    temporary = root / "ready.json.part"
    if temporary.is_symlink() or (root / "ready.json").is_symlink():
        raise ValueError("Описание установки GigaAM содержит внешнюю ссылку")
    temporary.unlink(missing_ok=True)
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(ready, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, root / "ready.json")
    source.unlink(missing_ok=True)
    progress(1, "GigaAM установлен; качество на вашей записи ещё не проверено")
    return ready


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        reset_dll_search()
        installed = install(args.root)
        emit("result", model=installed["model"], root=str(args.root))
    except DownloadError as exc:
        emit("error", message=str(exc))
        sys.exit(1)
    except Exception:
        # Network exceptions may contain temporary signed URLs. Never reflect
        # exception text or request/response objects through the UI event stream.
        emit("error", message="Установка GigaAM не завершена. Проверьте интернет, место и повторите установку")
        sys.exit(1)


if __name__ == "__main__":
    main()
