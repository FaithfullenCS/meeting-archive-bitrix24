"""Discover compatible public Python libraries and model caches.

The normal desktop flow reads these resources directly. Legacy snapshot APIs
remain for previously configured clients; they share immutable bytes through
hardlinks and must never pip-update another application's files.
Application configuration and model credentials are never scanned.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

from .worker.core.model_catalog import WHISPER_MODELS, whisper_repo

SUPPORTED_MODELS = frozenset(WHISPER_MODELS)
DIARIZATION_REPOS = ("models--pyannote--speaker-diarization-3.1", "models--pyannote--segmentation-3.0",
                    "models--pyannote--wespeaker-voxceleb-resnet34-LM")
WORKER_PACKAGES = (
    "faster-whisper", "ctranslate2", "torch", "torchaudio", "pyannote.audio",
    "ffmpeg-python", "imageio-ffmpeg", "soundfile", "librosa", "noisereduce",
    "pyloudnorm", "numpy", "scipy", "pyyaml", "packaging",
)
REQUIRED_PACKAGES = frozenset(WORKER_PACKAGES) - {"imageio-ffmpeg", "pyannote.audio"}
MARKER = ".meeting-archive-owned"

# -S prevents executing .pth/custom site hooks in the source environment.
# Metadata is read explicitly from its known site-packages directory instead.
PROBE = """import sys,json,importlib.metadata as metadata
items={}
for dist in metadata.distributions(path=[sys.argv[1]]):
    name=dist.metadata.get('Name','')
    if name:
        items[name]={'version':dist.version,'requires':dist.requires or [],
                     'files':[str(p) for p in (dist.files or [])]}
print(json.dumps({'base_python_root':sys.base_prefix,'python_version':list(sys.version_info[:3]),
                  'bits':64 if sys.maxsize>2**32 else 32,'distributions':items}))
"""


class ResourceReuseCancelled(RuntimeError):
    """Cancellation is distinct from compatibility/download failures."""


def _normal_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _source_id(source: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(source.resolve())).encode("utf-8")).hexdigest()[:16]


def _reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) &
                                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _plain_root(path: Path) -> Path:
    """Check ancestors before resolve, so junctions cannot redirect ownership."""
    path = path.expanduser().absolute()
    if any(_reparse(p) for p in (path, *path.parents)):
        raise ValueError("Путь ресурсов содержит внешнюю ссылку или junction")
    return path.resolve()


def _safe_file(path: Path, root: Path) -> Path:
    if not path.absolute().is_relative_to(root):
        raise ValueError("Файл находится вне разрешённого источника ресурсов")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("Ссылка из источника ресурсов ведёт за пределы разрешённой папки")
    # Directory links are rejected even when their target happens to be internal.
    for parent in path.parents:
        if parent == root:
            break
        if _reparse(parent):
            raise ValueError("Источник ресурсов содержит ссылку на каталог")
    return resolved


def _excluded(path: Path) -> bool:
    parts = [p.lower() for p in path.parts]
    if any(p in {"__pycache__", ".git", ".hg", ".svn", ".locks", ".cache", ".venv"} for p in parts):
        return True
    name = path.name.lower()
    if name == "cacert.pem" and len(parts) > 1 and parts[-2] == "certifi":
        return False  # Public CA bundle is required even for HF's offline HTTP client initialization.
    if name.endswith((".pth", ".egg-link", ".pyc", ".pyo", ".pem", ".key", ".log", ".db", ".sqlite",
                      ".sqlite3", ".wav", ".mp3", ".m4a", ".mp4", ".srt", ".vtt")):
        return True
    return name in {"direct_url.json", ".env", "token", "stored_tokens", "credentials", "credentials.json",
                    "secrets.json", "settings.json", "config.private.json", "sitecustomize.py",
                    "usercustomize.py"} or (name.startswith(("token.", ".env.", "private.", "credentials."))
                                            and path.suffix.lower() not in {".py", ".pyi", ".pyd", ".dll"})


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    # Explicitly remove both model tokens and Python injection from the caller.
    for key in ("PYTHONPATH", "PYTHONHOME", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_TOKEN_PATH"):
        env.pop(key, None)
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
               HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_OFFLINE="1", UV_OFFLINE="1", UV_PYTHON_DOWNLOADS="never")
    return env


def _probe_environment(source: Path) -> dict:
    source = _plain_root(source)
    python = _safe_file(source / "Scripts/python.exe", source)
    site = _plain_root(source / "Lib/site-packages")
    if not site.is_dir():
        raise ValueError("В выбранной Python-среде нет Lib/site-packages")
    try:
        result = subprocess.run([str(python), "-I", "-S", "-B", "-c", PROBE, str(site)],
            env=_environment(), capture_output=True, text=True, encoding="utf-8", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        if result.returncode:
            raise ValueError("Не удалось проверить выбранную Python-среду")
        data = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ValueError("Выбранная Python-среда недоступна или не отвечает") from exc
    if data.get("python_version", [])[:2] != [3, 12] or data.get("bits") != 64:
        raise ValueError("Для независимого модуля нужна Python-среда 3.12 x64")
    base = _plain_root(Path(data["base_python_root"]))
    _safe_file(base / "python.exe", base)
    data.update(environment_path=str(source), base_python_root=str(base), site_packages=str(site))
    return data


def _worker_distributions(data: dict) -> dict:
    all_distributions = {_normal_name(name): dict(value, name=name)
                         for name, value in data["distributions"].items()}
    missing = REQUIRED_PACKAGES - all_distributions.keys()
    if missing:
        raise ValueError("В среде не хватает библиотек: " + ", ".join(sorted(missing)))
    # Follow installed runtime dependencies. Extras are not enabled by this
    # worker; missing optional/platform-specific dependencies are not adopted.
    selected, pending = {}, list(WORKER_PACKAGES)
    while pending:
        name = _normal_name(pending.pop())
        if name in selected or name not in all_distributions:
            continue
        dist = all_distributions[name]
        if not dist["files"]:
            raise ValueError("Библиотека без RECORD не подходит для независимого модуля: " + name)
        selected[name] = dist
        for requirement in dist.get("requires", []):
            if re.search(r"\bextra\s*(?:==|!=|in|not\s+in)", requirement, re.IGNORECASE):
                continue
            match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
            if match:
                pending.append(match.group(1))
    # Keep compatibility evidence explicit. Actual GPU support is verified by
    # a separate short worker run; metadata alone is not a CUDA test.
    def version(name):
        return tuple(int(n) for n in re.findall(r"\d+", selected[name]["version"].split("+")[0])[:3])
    if version("faster-whisper") < (1, 0, 3) or version("ctranslate2") < (4, 4, 0):
        raise ValueError("Версии faster-whisper/CTranslate2 слишком старые для этого модуля")
    return selected


def _site_files(data: dict, distributions: dict, cancelled: Callable[[], bool] | None = None) -> list[tuple[Path, Path]]:
    root = Path(data["site_packages"])
    found = {}
    for dist in distributions.values():
        for entry in dist["files"]:
            if cancelled and cancelled():
                raise ResourceReuseCancelled("Поиск ресурсов остановлен")
            # Console scripts/editable installs outside site-packages are never
            # followed. The worker uses its own entry point and no old scripts.
            relative = Path(entry.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts or _excluded(relative):
                continue
            source = root / relative
            for parent in source.parents:
                if parent == root:
                    break
                if _reparse(parent):
                    raise ValueError("Источник библиотек содержит ссылку на каталог")
            if source.exists() or source.is_symlink():
                found[relative] = _safe_file(source, root)
    if not found:
        raise ValueError("В выбранной среде не найдены переносимые библиотеки")
    return [(found[relative], relative) for relative in sorted(found)]


def _runtime_files(root: Path) -> list[tuple[Path, Path]]:
    files = []
    for item in root.iterdir():
        if item.is_file() and (re.fullmatch(r"python(?:w|\d*)\.(?:exe|dll)", item.name, re.I)
                               or re.fullmatch(r"vcruntime\w*\.dll", item.name, re.I)
                               or item.name == "LICENSE.txt"):
            files.append((_safe_file(item, root), Path(item.name)))
    for name in ("Lib", "DLLs"):
        directory = root / name
        if not directory.is_dir():
            continue
        if _reparse(directory):
            raise ValueError("Стандартная библиотека Python содержит ссылку на каталог")
        for current, dirs, names in os.walk(directory, followlinks=False):
            dirs[:] = [d for d in dirs if d.lower() not in {"site-packages", "__pycache__", "test", "tests"}]
            for child in dirs:
                if _reparse(Path(current) / child):
                    raise ValueError("Стандартная библиотека Python содержит ссылку на каталог")
            for child in names:
                path = Path(current) / child
                if not _excluded(path.relative_to(root)):
                    files.append((_safe_file(path, root), path.relative_to(root)))
    return files


def _cache_root() -> Path:
    # Do not read HF_HOME/token files from either application's configuration.
    return Path.home() / ".cache/huggingface/hub"


def _snapshot(repo: Path, *, whisper: bool = False) -> Path | None:
    if not repo.is_dir():
        return None
    repo = _plain_root(repo)
    reference = repo / "refs/main"
    commit = ""
    if reference.is_file():
        commit = _safe_file(reference, repo).read_text("utf-8").strip()
    if commit and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", commit):
        raise ValueError("Некорректная ссылка на снимок модели в публичном кеше")
    candidates = [repo / "snapshots" / commit] if commit else []
    snapshots = repo / "snapshots"
    if snapshots.is_dir():
        if _reparse(snapshots):
            raise ValueError("Кеш модели содержит ссылку на каталог")
        candidates.extend(sorted(snapshots.iterdir(), key=lambda p: p.name))
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        if _reparse(candidate):
            raise ValueError("Снимок модели содержит ссылку на каталог")
        required = ("model.bin", "config.json", "tokenizer.json") if whisper else ()
        if required and not all((candidate / p).is_file() and (candidate / p).stat().st_size > 0 for p in required):
            continue
        if whisper and not any((candidate / name).is_file() and (candidate / name).stat().st_size > 0 for name in ("vocabulary.json", "vocabulary.txt")):
            continue
        # Validate links before declaring the model reusable.
        for current, dirs, files in os.walk(candidate, followlinks=False):
            for child in dirs:
                if _reparse(Path(current) / child):
                    raise ValueError("Снимок модели содержит ссылку на каталог")
            for child in files:
                _safe_file(Path(current) / child, repo)
        if required or any(candidate.iterdir()):
            return candidate
    return None


def _diarization_caches() -> tuple[Path, ...]:
    # pyannote 3.x uses its separate public model cache by default. Read only
    # named model snapshots, never its locks, tokens or application settings.
    return (_cache_root(), Path.home() / ".cache/torch/pyannote")


def _models(cache: Path) -> list[str]:
    if not cache.exists():
        return []
    cache = _plain_root(cache)
    return [model for model in sorted(SUPPORTED_MODELS)
            if _snapshot(cache / ("models--" + whisper_repo(model).replace("/", "--")), whisper=True)]


def _model_files(repos: list[dict]) -> list[tuple[Path, Path]]:
    result = []
    for repo in repos:
        root, snapshot = Path(repo["path"]), Path(repo["snapshot"])
        for current, dirs, files in os.walk(snapshot, followlinks=False):
            for child in dirs:
                if _reparse(Path(current) / child):
                    raise ValueError("Снимок модели содержит ссылку на каталог")
            for child in files:
                path = Path(current) / child
                relative = path.relative_to(root)
                if not _excluded(relative):
                    result.append((_safe_file(path, root), Path(repo["repo"]) / relative))
    return result


def _ffmpeg(data: dict) -> Path | None:
    found = shutil.which("ffmpeg")
    if found:
        # Windows package managers expose a file link; adopt its resolved public
        # executable, never keep the alias as a runtime dependency.
        resolved = Path(found).resolve(strict=True)
        if resolved.is_file():
            return resolved
    site = Path(data["site_packages"])
    binaries = site / "imageio_ffmpeg/binaries"
    if binaries.is_dir():
        for executable in sorted(binaries.glob("ffmpeg*.exe")):
            return _safe_file(executable, site)
    return None


def _bytes(files: list[tuple[Path, Path]]) -> int:
    unique = {}
    for source, _ in files:
        info = source.stat()
        unique[(info.st_dev, info.st_ino)] = info.st_size
    return sum(unique.values())


def _destination_device(target: Path) -> int:
    while not target.exists():
        target = target.parent
    return target.stat().st_dev


def environment_candidates() -> list[Path]:
    """Bounded local discovery: conventional venvs, no recursive disk search."""
    from .settings import documents_folder
    roots = [Path.home(), documents_folder(), Path.home() / "Desktop", Path.home() / ".virtualenvs"]
    for origin in (Path(sys.executable).parent, Path.cwd()):
        roots.extend(p for p in (origin, *origin.parents) if p != Path(p.anchor))
    found = []
    for root in dict.fromkeys(roots):
        try:
            children = sorted((p for p in root.iterdir() if p.is_dir() and not _reparse(p)), key=lambda p: p.name)[:100]
            for directory in (root, *children):
                if (directory / "Scripts/python.exe").is_file() and (directory / "Lib/site-packages/faster_whisper").is_dir():
                    found.append(directory)
                for candidate in directory.glob(".venv*"):
                    if not _reparse(candidate) and (candidate / "Scripts/python.exe").is_file() and (candidate / "Lib/site-packages/faster_whisper").is_dir():
                        found.append(candidate)
        except (OSError, RuntimeError):
            continue
    return list(dict.fromkeys(found))[:32]


def discover_resources(additional_environment: Path | None = None, *, cancelled: Callable[[], bool] | None = None) -> dict:
    def check_cancelled():
        if cancelled and cancelled():
            raise ResourceReuseCancelled("Поиск ресурсов остановлен")

    check_cancelled()
    cache = _cache_root()
    sources = []
    candidates = environment_candidates()
    if additional_environment is not None:
        candidates.append(Path(additional_environment))
    seen = set()
    for source in candidates:
        check_cancelled()
        identity = _source_id(source)
        if identity in seen:
            continue
        seen.add(identity)
        item = {"source_id": identity, "label": "Установленные ресурсы Python · " + source.name,
                "environment_path": str(source), "source_kind": "existing-python-environment",
                "versions": {}, "models": [], "compatible": False, "reason": ""}
        try:
            data = _probe_environment(source)
            check_cancelled()
            distributions = _worker_distributions(data)
            _site_files(data, distributions, cancelled)
            check_cancelled()
            item.update(environment_path=data["environment_path"], python_version=".".join(map(str, data["python_version"])),
                        versions={name: d["version"] for name, d in distributions.items()}, models=_models(cache))
            if not _ffmpeg(data):
                raise ValueError("FFmpeg не найден среди установленных ресурсов")
            item.update(compatible=True, ffmpeg=str(_ffmpeg(data)), reason="Библиотеки подходят; CUDA и выбранная модель проверяются отдельно")
        except ResourceReuseCancelled:
            raise
        except (ValueError, OSError, RuntimeError) as exc:
            item["reason"] = str(exc)
        sources.append(item)
    check_cancelled()
    try:
        cached_models = _models(cache)
    except (OSError, ValueError):
        cached_models = []
    return {"sources": sources, "model_cache": str(cache), "cached_models": cached_models}


def _prepared_plan(source: Path, model: str, target: Path | None = None) -> tuple[dict, list]:
    if model not in SUPPORTED_MODELS:
        raise ValueError("Неизвестная модель Whisper")
    data = _probe_environment(source)
    distributions = _worker_distributions(data)
    cache = _cache_root()
    if not cache.is_dir():
        raise ValueError("Публичный кеш моделей Hugging Face не найден; выберите обычную установку")
    cache = _plain_root(cache)
    repo = cache / ("models--" + whisper_repo(model).replace("/", "--"))
    snapshot = _snapshot(repo, whisper=True)
    if snapshot is None:
        raise ValueError("Выбранная модель ещё не сохранена в публичном кеше: " + model)
    repos = [{"repo": repo.name, "path": str(repo), "snapshot": str(snapshot), "commit": snapshot.name}]
    for name in DIARIZATION_REPOS:
        for model_cache in _diarization_caches():
            if not model_cache.is_dir():
                continue
            model_cache = _plain_root(model_cache)
            directory = model_cache / name
            available = _snapshot(directory)
            if available:
                repos.append({"repo": name, "path": str(directory), "snapshot": str(available), "commit": available.name})
                break
    ffmpeg = _ffmpeg(data)
    if ffmpeg is None:
        raise ValueError("FFmpeg не найден среди установленных ресурсов")
    groups = [("python/reused-python", _runtime_files(Path(data["base_python_root"]))),
              ("venv/Lib/site-packages", _site_files(data, distributions)),
              ("models/hub", _model_files(repos)), ("tools", [(ffmpeg, Path("ffmpeg.exe"))])]
    files = [entry for _, entries in groups for entry in entries]
    target = _plain_root(target or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MeetingArchive/module")
    device = _destination_device(target)
    # Never silently fall back to copying gigabytes when hardlinks were approved.
    storage = "hardlink" if all(p.stat().st_dev == device for p, _ in files) else "copy"
    source_bytes = _bytes(files)
    plan = {"source_id": _source_id(source), "source_kind": "existing-python-environment", "model": model,
            "environment_path": data["environment_path"], "base_python_root": data["base_python_root"],
            "python_version": ".".join(map(str, data["python_version"])), "site_packages": data["site_packages"],
            "versions": {name: d["version"] for name, d in distributions.items()},
            "packages": sorted(distributions), "model_repos": repos, "ffmpeg": str(ffmpeg),
            "download_bytes": 0, "source_bytes": source_bytes, "storage_mode": storage,
            "required_bytes": source_bytes if storage == "copy" else 128 * 1024**2,
            "target_path": str(target), "immutable_resources": True, "pinned": False}
    return plan, groups


def plan_resources(source: Path, model: str, target: Path | None = None) -> dict:
    """No downloads or changes; exact disk estimate when target is provided."""
    return _prepared_plan(Path(source), model, target)[0]


def _cancel(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise ResourceReuseCancelled("Подключение установленных ресурсов отменено")


def _uv_venv(uv: Path, runtime: Path, venv: Path, cancelled: Callable[[], bool]) -> None:
    env = _environment()
    env.update(UV_CACHE_DIR=str(venv.parent / "uv-cache"), UV_PYTHON_INSTALL_DIR=str(venv.parent / "python"))
    process = subprocess.Popen([str(uv), "venv", "--allow-existing", "--python", str(runtime), str(venv)],
        cwd=venv.parent, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    started = time.monotonic()
    try:
        while process.poll() is None:
            _cancel(cancelled)
            if time.monotonic() - started > 120:
                raise RuntimeError("Создание независимой Python-среды превысило время ожидания")
            time.sleep(.1)
        _cancel(cancelled)
        if process.returncode:
            raise RuntimeError("Не удалось создать независимую Python-среду без скачивания")
    finally:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                    timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def _owned_target(target: Path) -> Path:
    target = _plain_root(target)
    if target.exists() and any(target.iterdir()) and not (target / MARKER).is_file():
        raise ValueError("Папка не принадлежит Meeting Archive; подключение ресурсов остановлено")
    if target.exists():
        for current, dirs, names in os.walk(target, followlinks=False):
            for child in (*dirs, *names):
                path = Path(current) / child
                if _reparse(path):
                    # Materialized resources contain no links. Refuse existing
                    # ones before replacing anything, even internally pointing.
                    raise ValueError("Папка модуля содержит ссылку; подключение ресурсов остановлено")
    target.mkdir(parents=True, exist_ok=True)
    marker = target / MARKER
    if not marker.exists():
        marker.write_text("Meeting Archive optional module\n", encoding="utf-8")
    return target


def _rewrite_configuration(stage: Path, target: Path) -> None:
    config = stage / "venv/pyvenv.cfg"
    if not config.is_file() or _reparse(config):
        raise ValueError("Установщик не создал собственный pyvenv.cfg")
    runtime = target / "python/reused-python"
    lines = []
    for line in config.read_text("utf-8").splitlines():
        key, separator, _ = line.partition("=")
        if separator and key.strip() in {"home", "base-prefix", "base-exec-prefix"}:
            line = key.strip() + " = " + str(runtime)
        elif separator and key.strip() == "base-executable":
            line = "base-executable = " + str(runtime / "python.exe")
        else:
            line = line.replace(str(stage), str(target))
        lines.append(line)
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")


def reuse_resources(plan: dict, target: Path, uv: Path, progress: Callable[[float, str], None],
                    cancelled: Callable[[], bool]) -> dict:
    """Clone immutable resources, atomically publish components, return evidence.

    The caller writes ready.json only after successful completion. Any exception
    leaves source resources untouched and rolls back an existing owned module.
    """
    _cancel(cancelled)
    target = _plain_root(Path(target))
    current, groups = _prepared_plan(Path(plan["environment_path"]), str(plan["model"]), target)
    for key in ("source_id", "python_version", "versions", "storage_mode", "model_repos"):
        if current[key] != plan.get(key):
            raise ValueError("Установленные ресурсы или том назначения изменились; заново подтвердите план")
    # The source and destination cannot contain each other: independence would
    # otherwise be false and rollback/cleanup could touch the source.
    sources = [Path(current[k]) for k in ("environment_path", "base_python_root", "site_packages")]
    sources.extend(Path(repo["path"]) for repo in current["model_repos"])
    allowed_roots = [Path(current["base_python_root"]), Path(current["site_packages"]),
                     *(Path(repo["path"]) for repo in current["model_repos"]), Path(current["ffmpeg"]).parent]
    if any(target.is_relative_to(p) or p.is_relative_to(target) for p in sources):
        raise ValueError("Папка независимого модуля пересекается с исходными ресурсами")
    target = _owned_target(target)
    if shutil.disk_usage(target).free < current["required_bytes"]:
        raise ValueError("Для подключения ресурсов недостаточно свободного места")
    stage = target / (".reuse-staging-" + uuid.uuid4().hex)
    stage.mkdir()
    total = sum(len(entries) for _, entries in groups)
    completed, updated, backed_up = 0, [], []
    last_progress = 0.0
    cloned = {}
    try:
        progress(0, "Подключение установленных ресурсов без скачивания")
        for prefix, entries in groups:
            if prefix == "venv/Lib/site-packages":
                _cancel(cancelled)
                _uv_venv(Path(uv), stage / "python/reused-python/python.exe", stage / "venv", cancelled)
            for source, relative in entries:
                _cancel(cancelled)
                destination = stage / prefix / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Revalidate file resolution immediately before opening/linking.
                allowed = next((p for p in allowed_roots if source.is_relative_to(p)), None)
                if allowed is None:
                    raise ValueError("Файл ресурсов находится вне проверенного плана")
                source = _safe_file(source, allowed)
                info = source.stat()
                if current["storage_mode"] == "hardlink":
                    try:
                        os.link(source, destination)
                    except OSError as exc:
                        raise RuntimeError("Том не позволяет разделить байты hardlink; выберите копирование отдельно") from exc
                else:
                    shutil.copy2(source, destination)
                after = source.stat()
                if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("Исходные ресурсы изменились во время подключения; повторите план")
                cloned[(info.st_dev, info.st_ino)] = info.st_size
                completed += 1
                if time.monotonic() - last_progress > .25:
                    progress(completed / max(total, 1) * .9, "Создание независимых путей к библиотекам и моделям")
                    last_progress = time.monotonic()
        for repo in current["model_repos"]:
            reference = stage / "models/hub" / repo["repo"] / "refs/main"
            reference.parent.mkdir(parents=True, exist_ok=True)
            reference.write_text(repo["commit"], encoding="utf-8")
        _rewrite_configuration(stage, target)
        _cancel(cancelled)
        backup = stage / "backup"
        backup.mkdir()
        # Only named components under this checked, owned root are replaced.
        # Keep backups until all components are published; roll back on failure.
        for component in ("python", "venv", "models", "tools"):
            _cancel(cancelled)
            destination = target / component
            if destination.exists():
                os.replace(destination, backup / component)
                backed_up.append(component)
            os.replace(stage / component, destination)
            updated.append(component)
        _cancel(cancelled)
        actual = _probe_environment(target / "venv")
        if Path(actual["base_python_root"]) != target / "python/reused-python":
            raise RuntimeError("Новая среда всё ещё зависит от исходного Python")
        actual_versions = {name: value["version"] for name, value in actual["distributions"].items()}
        normalized_actual = {_normal_name(name): value for name, value in actual_versions.items()}
        if any(normalized_actual.get(name) != version for name, version in current["versions"].items()):
            raise RuntimeError("Проверка скопированных библиотек не совпала с планом")
        shared = sum(cloned.values()) if current["storage_mode"] == "hardlink" else 0
        exclusive = current["source_bytes"] - shared
        ready = {"profile": "cuda" if "+cu" in current["versions"]["torch"] else "cpu",
                 "model": current["model"], "installedAt": time.time(), "reused": True, "pinned": False,
                 "source_kind": current["source_kind"], "source_id": current["source_id"],
                 "versions": normalized_actual, "python_version": current["python_version"],
                 "storage_mode": current["storage_mode"], "source_bytes": current["source_bytes"],
                 "shared_bytes": shared, "exclusive_bytes": exclusive,
                 "runtime_path": str(target / "python/reused-python"), "environment_path": str(target / "venv"),
                 "model_cache": str(target / "models/hub"), "ffmpeg": str(target / "tools/ffmpeg.exe"),
                 "immutable_resources": True, "gpu_verified": False}
        progress(1, "Ресурсы подключены; приложения используют независимые папки")
        return ready
    except BaseException:
        for component in reversed(updated):
            destination = target / component
            if destination.exists():
                shutil.rmtree(destination)
        for component in reversed(backed_up):
            backup_component = stage / "backup" / component
            if backup_component.exists():
                os.replace(backup_component, target / component)
        raise
    finally:
        # Stage was created by this call; no source path is ever removed.
        if stage.exists() and stage.parent == target and stage.name.startswith(".reuse-staging-"):
            shutil.rmtree(stage)
