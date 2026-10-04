from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

from .hardware import MODELS
from .settings import atomic_json

PACKAGE = Path(__file__).parent


def terminate_owned_process(process):
    """Windows venv launchers can have a Python child; cancel that owned tree."""
    if process.returncode is not None:
        return
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            return
    try:
        process.terminate()
    except ProcessLookupError:
        pass


class ModuleManager:
    def __init__(self, home: Path, vault):
        self.home_path = home.expanduser().absolute()
        self.home, self.vault = home.resolve(), vault
        self.root = self.home / "module"
        self.process: asyncio.subprocess.Process | None = None
        self.cancel_install = False
        self.reuse_task = None
        self.processes: dict[int, asyncio.subprocess.Process] = {}
        self._size_cache = (0.0, 0)
        self.discovered = {"sources": [], "cached_models": []}
        self.discovery_at = 0.0
        self.discovery_lock = asyncio.Lock()

    async def discover(self, *, refresh=False):
        async with self.discovery_lock:
            if not self.discovery_at or refresh:
                from .resource_reuse import discover_resources
                cancelled = threading.Event()
                worker = asyncio.create_task(asyncio.to_thread(discover_resources, cancelled=cancelled.is_set))
                try:
                    self.discovered = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # Cancelling to_thread alone leaves the filesystem scan
                    # running, and asyncio waits for it at application exit.
                    cancelled.set()
                    await asyncio.gather(worker, return_exceptions=True)
                    raise
                self.discovery_at = time.monotonic()
        return self.discovered

    def borrowed_source(self, model: str | None = None, *, diarization=False):
        from .engine_catalog import whisper_installed
        from .resource_reuse import _normal_name
        return next((s for s in self.discovered["sources"] if s.get("compatible") and
                     (not diarization or any(_normal_name(name) == "pyannote-audio" for name in s.get("versions", {}))) and
                     (model is None or model in s.get("models", []) or whisper_installed(self.root, model))), None)

    def catalogue(self, profile="cuda"):
        from .engine_catalog import catalogue, full_runtime_ready
        result = catalogue(self.checked_root(), profile)
        for engine in result["engines"]:
            for model in engine["models"]:
                model["origin"] = "application" if model["installed"] else ""
                if engine["id"] == "whisper":
                    model["cached"] = model["id"] in self.discovered.get("cached_models", [])
                    if not model["installed"] and self.borrowed_source(model["id"]):
                        model.update(installed=True, origin="computer")
                    elif not model["installed"] and model["cached"] and full_runtime_ready(self.root, profile):
                        model.update(installed=True, origin="computer_weights")
                model["removable"] = any((self.root / name).exists() for name in self.model_targets(engine["id"], model["id"]))
        source = self.borrowed_source(diarization=True)
        result["features_ready"] = full_runtime_ready(self.root, profile) or bool(source)
        return result

    def model_targets(self, engine, model):
        from .engine_catalog import validate_packages
        validate_packages([{"engine": engine, "model": model}])
        if engine == "whisper":
            from .worker.core.model_catalog import whisper_repo
            return ["models/hub/models--" + whisper_repo(model).replace("/", "--")]
        if engine == "parakeet":
            from .worker.install_parakeet import MODEL_FILE
            return ["engines/parakeet/models/" + MODEL_FILE,
                    "engines/parakeet/ready-cpu.json", "engines/parakeet/ready-cuda.json"]
        return ["engines/gigaam/model", "engines/gigaam/ready.json"]

    def model_removal_plan(self, packages):
        from .engine_catalog import validate_packages
        from .cleanup import selection_plan
        packages = validate_packages(packages)
        root = self.checked_root()
        if not (root / ".meeting-archive-owned").is_file():
            raise ValueError("Выбранные модели не принадлежат приложению. Найденные на компьютере ресурсы удаляются в их исходном приложении")
        targets = [name for p in packages for name in self.model_targets(p["engine"], p["model"])]
        return {**selection_plan(root, targets, internal_links=True), "packages": packages}

    def remove_models(self, packages, token):
        from .cleanup import remove_selection
        plan = self.model_removal_plan(packages)
        result = remove_selection(self.checked_root(), plan["targets"], token, internal_links=True)
        self._size_cache = (0, 0)
        return result

    def processing_support(self, settings):
        enabled = bool(settings.diarization or settings.noise_reduction or settings.normalize)
        from .engine_catalog import full_runtime_ready
        own = full_runtime_ready(self.root, settings.device)
        if settings.diarization:
            own = own and (self.root / "venv/Lib/site-packages/pyannote/audio/__init__.py").is_file()
        source = self.borrowed_source(settings.model, diarization=settings.diarization) if settings.engine == "whisper" else None
        borrowed = bool(source)
        ready = own or borrowed
        reason = "" if not enabled or ready else ("Для диаризации нужно установить поддержку разделения по говорящим. Токен предоставляет доступ к моделям, а библиотеки устанавливаются отдельно" if settings.diarization else "Установите поддержку обработки звука")
        return {"ready": ready, "requires_install": enabled and not ready, "reason": reason}

    def installation_plan(self, profile, packages, *, full_features=False):
        from .engine_catalog import installation_plan
        from .hardware import runtime_plan
        source = self.borrowed_source(diarization=full_features)
        plan = installation_plan(self.checked_root(), profile, packages, full_features=full_features,
                                 cached_whisper=self.discovered.get("cached_models", []),
                                 borrowed_runtime=bool(source))
        plan["runtime"] = runtime_plan(profile)
        if not full_features and all(p["engine"] == "parakeet" for p in plan["packages"]):
            plan["runtime"]["description"] = "Parakeet — отдельный нативный комплект " + ("CUDA" if profile == "cuda" else "CPU")
        return plan

    def borrowed_environment(self, model: str | None, *, diarization=False):
        """Use compatible libraries directly; never copy or pip-update them."""
        from .resource_reuse import _probe_environment, _worker_distributions, _site_files, _ffmpeg, _models, _cache_root, _safe_file
        source = self.borrowed_source(model, diarization=diarization)
        if not source:
            raise ValueError("Совместимые ресурсы модели недоступны. Установите её в каталоге моделей")
        data = _probe_environment(Path(source["environment_path"]))
        _site_files(data, _worker_distributions(data))
        site = Path(data["site_packages"])
        for package in ("faster_whisper", "ctranslate2", "torch"):
            _safe_file(site / package / "__init__.py", site)
        ffmpeg = _ffmpeg(data)
        from .engine_catalog import whisper_installed
        own_weights = model is not None and whisper_installed(self.root, model)
        if not ffmpeg or (model is not None and not own_weights and model not in _models(_cache_root())):
            raise ValueError("Найденная среда или модель больше недоступна. Установите модель в Meeting Archive")
        env = self.env(external=True)
        cache = self.root / "models/hub" if own_weights else _cache_root()
        env.update(HF_HUB_CACHE=str(cache), HF_HOME=str(cache.parent),
                   HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        if diarization:
            # Whisper weights were checked above. Pyannote may still need its
            # own gated models; an offline Whisper reuse must not block them.
            env.pop("HF_HUB_OFFLINE", None)
            env.pop("TRANSFORMERS_OFFLINE", None)
        env["PATH"] = str(ffmpeg.parent) + os.pathsep + env.get("PATH", "")
        return str(Path(source["environment_path"]) / "Scripts/python.exe"), env

    def checked_root(self) -> Path:
        """Reject reparse redirection before resolving or changing module files."""
        expected = self.home / "module"
        if self.root.absolute() != expected.absolute():
            raise ValueError("Небезопасный путь модуля. Операция остановлена")

        def reparse(path):
            try:
                attrs = path.lstat()
            except FileNotFoundError:
                return False
            return path.is_symlink() or bool(getattr(attrs, "st_file_attributes", 0) &
                                            getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))

        for path in (self.root, *self.root.parents, self.home_path, *self.home_path.parents):
            if reparse(path):
                raise ValueError("Путь модуля содержит внешнюю ссылку. Операция остановлена")
        target = self.root.resolve()
        if target != expected or not target.is_relative_to(self.home):
            raise ValueError("Небезопасный путь модуля. Операция остановлена")
        if target.exists():
            # Inspect directory entries before walking into children. Internal
            # model-cache links are permitted; links outside the owned root are not.
            for current, dirs, files in os.walk(target, followlinks=False):
                for name in (*dirs, *files):
                    path = Path(current) / name
                    if reparse(path):
                        try:
                            internal = path.resolve().is_relative_to(target)
                        except (OSError, RuntimeError):
                            internal = False
                        if not internal:
                            raise ValueError("В модуле найдена внешняя ссылка. Операция остановлена")
        return target

    def status(self):
        checked = self.checked_root()
        ready_path = checked / "ready.json"
        from .engine_catalog import read_marker
        ready = read_marker(ready_path)
        timestamp, total = self._size_cache
        if time.monotonic() - timestamp > 15:
            total = sum(p.stat().st_size for p in checked.rglob("*") if p.is_file() and not p.is_symlink()) if checked.exists() else 0
            self._size_cache = (time.monotonic(), total)
        return {"installed": bool(ready) and (self.root / "venv/Scripts/python.exe").is_file(),
                "size_gb": round(total / 1024**3, 2),
                "installing": self.process is not None or (self.reuse_task is not None and not self.reuse_task.done()),
                "resources": ready,
                "profiles": [{"name": "cuda", "estimate_gb": 12}, {"name": "cpu", "estimate_gb": 6}],
                "models": MODELS}

    def env(self, external=False) -> dict:
        env = os.environ.copy()
        # An old launcher's environment must not select its source modules or
        # authenticate the new app with an unrelated process-level HF token.
        env.pop("MEETING_ARCHIVE_EXTERNAL_ENGINE", None)
        env.pop("HF_TOKEN", None)
        env.pop("HUGGING_FACE_HUB_TOKEN", None)
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1",
                   HF_HOME=str(self.root / "models"), HF_HUB_CACHE=str(self.root / "models/hub"),
                   UV_PYTHON_INSTALL_DIR=str(self.root / "python"), UV_CACHE_DIR=str(self.root / "uv-cache"),
                   UV_PYTHON_BIN_DIR=str(self.root / "bin"), HF_HUB_DISABLE_TELEMETRY="1",
                   PYANNOTE_METRICS_ENABLED="0", HF_HUB_DISABLE_XET="1")
        env["PATH"] = str(self.root / "tools") + os.pathsep + env.get("PATH", "")
        env["MEETING_ARCHIVE_PARAKEET_HOME"] = str(self.root / "engines/parakeet")
        env["MEETING_ARCHIVE_GIGAAM_HOME"] = str(self.root / "engines/gigaam")
        env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        from .resource_reuse import _cache_root
        env["MEETING_ARCHIVE_MODEL_CACHE"] = str(_cache_root())
        if not external:
            env["PYANNOTE_CACHE"] = str(self.root / "models/hub")
            env["TORCH_HOME"] = str(self.root / "models/torch")
        ready = self.root / "ready.json"
        from .engine_catalog import read_marker
        if not external and read_marker(ready).get("reused"):
            env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        token = self.vault.read().get("hf_token", "")
        if token:
            env["HF_TOKEN"] = token
        if external:
            # External caches belong to the existing application; never delete or relocate them.
            env.pop("HF_HOME", None)
            env.pop("HF_HUB_CACHE", None)
        return env

    def uv(self) -> Path:
        bundled = PACKAGE / "resources/uv.exe"
        if bundled.exists():
            return bundled
        try:
            import uv
            return Path(uv.find_uv_bin())
        except ImportError as exc:
            raise RuntimeError("В сборке отсутствует установщик uv. Используйте полную Windows-сборку") from exc

    async def command(self, args: list[str], progress, *, stage: str, environment=None):
        if self.cancel_install:
            raise asyncio.CancelledError()
        environment = environment if environment is not None else self.env()
        # Install commands are explicitly requested downloads; inference remains
        # offline when resources were reused from another installation.
        environment.pop("HF_HUB_OFFLINE", None)
        environment.pop("TRANSFORMERS_OFFLINE", None)
        self.process = await asyncio.create_subprocess_exec(*args, cwd=self.home, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            reported_error = ""
            while line := await self.process.stdout.readline():
                if self.cancel_install:
                    raise asyncio.CancelledError()
                text = line.decode("utf-8", "replace").strip()
                try:
                    event = json.loads(text)
                except ValueError:
                    event = None
                if isinstance(event, dict) and event.get("type") == "progress":
                    progress(float(event.get("progress", 0)), stage + ": " + self.vault.redact(str(event.get("message", "Установка"))))
                elif isinstance(event, dict) and event.get("type") == "error":
                    reported_error = self.vault.redact(str(event.get("message", "Ошибка установки")))
                elif isinstance(event, dict) and event.get("type") == "result":
                    progress(1, stage + ": готово")
                else:
                    progress(0, stage + ": " + self.vault.redact(text))
            code = await self.process.wait()
            if self.cancel_install:
                raise asyncio.CancelledError()
            if code:
                raise RuntimeError(reported_error or stage + " завершился с ошибкой. Проверьте интернет, место и повторите установку")
        finally:
            if self.process and self.process.returncode is None:
                await asyncio.to_thread(terminate_owned_process, self.process)
                await self.process.wait()
            self.process = None

    async def install(self, profile: str, model: str, progress, *, download=True):
        if profile not in {"cpu", "cuda"} or model not in {m["name"] for m in MODELS}:
            raise ValueError("Неизвестный профиль или модель")
        from .hardware import runtime_plan
        selected = await asyncio.to_thread(runtime_plan, profile)
        if not selected["available"]:
            raise ValueError(selected["reason"])
        progress(0, selected["description"])
        target = self.checked_root()
        if target.exists() and any(target.iterdir()) and not (target / ".meeting-archive-owned").is_file():
            raise ValueError("Папка модуля содержит чужие файлы и не помечена как Meeting Archive. Установка остановлена")
        self.cancel_install = False
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / ".meeting-archive-owned"
        marker.write_text("Meeting Archive optional module\n", encoding="utf-8")
        ready = self.root / "ready.json"
        from .engine_catalog import read_marker
        if read_marker(ready).get("reused"):
            # Pip must never update files sharing storage with another program.
            # Remove only our owned names before creating a pinned fresh venv.
            if (self.root / "venv").exists():
                await asyncio.to_thread(shutil.rmtree, self.root / "venv")
        (self.root / "ready.json").unlink(missing_ok=True)
        need = (12 if profile == "cuda" else 6) + next(m["download_gb"] for m in MODELS if m["name"] == model)
        if shutil.disk_usage(self.root).free < need * 1024**3:
            raise ValueError(f"Для установки нужно примерно {need:.1f} ГБ свободного места")
        uv = str(self.uv())
        await self.command([uv, "python", "install", "3.12.15", "--no-cache", "--no-bin"], progress, stage="Python")
        runtimes = list((self.root / "python").glob("cpython-3.12.15-*/python.exe"))
        if not runtimes:
            raise RuntimeError("Не найден управляемый Python после установки")
        await self.command([uv, "venv", "--allow-existing", "--python", str(runtimes[0]), str(self.root / "venv")], progress, stage="Среда модуля")
        python = str(self.root / "venv/Scripts/python.exe")
        lock = PACKAGE / "resources" / ("worker-" + selected["lock_profile"] + ".lock")
        if not lock.exists():
            raise RuntimeError("В сборке отсутствует список закреплённых зависимостей")
        await self.command([uv, "pip", "sync", "--python", python, str(lock), "--require-hashes", "--no-cache",
                            "--index-strategy", "unsafe-best-match"], progress, stage="Библиотеки")
        if download:
            await self.command([python, "-B", str(PACKAGE / "worker/entry.py"), "--download", model], progress, stage="Модель Whisper")
        atomic_json(self.root / "ready.json", {"profile": profile, "model": model if download else "", "installedAt": time.time(), "pinned": True,
                                             "runtime_profile": selected["lock_profile"], "cuda_version": selected.get("cuda_version", "")})
        progress(1, "Модуль установлен")

    async def ensure_runtime(self, profile, progress, *, minimal=False):
        target = self.checked_root()
        ready_path = target / "ready.json"
        from .engine_catalog import read_marker
        ready = read_marker(ready_path)
        python = target / "venv/Scripts/python.exe"
        full = (target / "venv/Lib/site-packages/torch/__init__.py").is_file()
        compatible = ready.get("profile") == profile or (ready.get("profile") == "cuda" and profile == "cpu")
        if python.is_file() and full and compatible and profile == "cuda" and not minimal:
            from .hardware import worker_probe
            try:
                compatible = (await asyncio.to_thread(worker_probe, str(python), self.env(False))).get("compatible", False)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                compatible = False
        if python.is_file() and ready and (minimal or (full and compatible)):
            progress(0, "Общая среда уже установлена; повторная загрузка не нужна")
            return str(python)
        if not minimal:
            await self.install(profile, "base", progress, download=False)
            return str(python)
        if target.exists() and any(target.iterdir()) and not (target / ".meeting-archive-owned").is_file():
            raise ValueError("Папка модуля не принадлежит Meeting Archive")
        target.mkdir(parents=True, exist_ok=True)
        (target / ".meeting-archive-owned").write_text("Meeting Archive optional module\n", encoding="utf-8")
        uv = str(self.uv())
        await self.command([uv, "python", "install", "3.12.15", "--no-cache", "--no-bin"], progress, stage="Python")
        runtimes = list((target / "python").glob("cpython-3.12.15-*/python.exe"))
        if not runtimes:
            raise RuntimeError("Не найден управляемый Python после установки")
        await self.command([uv, "venv", "--allow-existing", "--python", str(runtimes[0]), str(target / "venv")], progress, stage="Среда модуля")
        await self.command([uv, "pip", "sync", "--python", str(python), str(PACKAGE / "resources/worker-native.lock"),
                            "--require-hashes", "--no-cache"], progress, stage="Подготовка аудио")
        atomic_json(ready_path, {"profile": "native", "installedAt": time.time(), "pinned": True})
        return str(python)

    async def install_packages(self, profile, packages, progress, *, full_features=False):
        from .engine_catalog import validate_packages, whisper_installed
        packages = validate_packages(packages)
        from .hardware import runtime_plan
        selected = await asyncio.to_thread(runtime_plan, profile)
        if not selected["available"]:
            raise ValueError(selected["reason"])
        self.cancel_install = False
        full = full_features or any(p["engine"] in {"whisper", "gigaam"} for p in packages)
        source = self.borrowed_source(diarization=full_features)
        borrowed = all(p["engine"] == "whisper" for p in packages) and source
        borrowed_env = None
        if borrowed:
            python, borrowed_env = await asyncio.to_thread(self.borrowed_environment, None, diarization=full_features)
            target = self.checked_root()
            if target.exists() and any(target.iterdir()) and not (target / ".meeting-archive-owned").is_file():
                raise ValueError("Папка модуля содержит чужие файлы. Установка остановлена")
            target.mkdir(parents=True, exist_ok=True)
            (target / ".meeting-archive-owned").write_text("Meeting Archive optional module\n", encoding="utf-8")
            borrowed_env.update(HF_HUB_CACHE=str(target / "models/hub"), HF_HOME=str(target / "models"))
            borrowed_env.pop("HF_HUB_OFFLINE", None)
            borrowed_env.pop("TRANSFORMERS_OFFLINE", None)
        else:
            python = await self.ensure_runtime(profile, progress, minimal=not full)
        if profile == "cuda" and full:
            from .hardware import worker_probe
            progress(0, "Проверяем работу GPU с установленными библиотеками")
            probe = await asyncio.to_thread(worker_probe, python, borrowed_env or self.env(False))
            if not probe.get("compatible"):
                raise ValueError("Установленные библиотеки CUDA несовместимы с видеокартой. Проверьте драйвер NVIDIA; загрузка моделей остановлена")
        for index, package in enumerate(packages):
            if self.cancel_install:
                raise asyncio.CancelledError()
            def stage_progress(pct, message):
                progress((index + max(0, min(1, pct))) / len(packages), message)
            engine, model = package["engine"], package["model"]
            if engine == "whisper":
                if not whisper_installed(self.root, model) and model not in self.discovered.get("cached_models", []):
                    extra = {"environment": borrowed_env} if borrowed_env is not None else {}
                    await self.command([python, "-B", str(PACKAGE / "worker/entry.py"), "--download", model],
                                       stage_progress, stage="Whisper " + model, **extra)
                else:
                    stage_progress(1, "Whisper " + model + " уже установлен")
            elif engine == "gigaam":
                addons = self.root / "engines/gigaam/addons"
                addons.mkdir(parents=True, exist_ok=True)
                await self.command([str(self.uv()), "pip", "install", "--python", python,
                    "--target", str(addons), "--no-deps", "--require-hashes", "--no-cache",
                    "-r", str(PACKAGE / "resources/worker-gigaam-addons.lock")], stage_progress, stage="Библиотеки GigaAM")
                await self.command([python, "-B", str(PACKAGE / "worker/install_gigaam.py"),
                    "--root", str(self.root / "engines/gigaam")], stage_progress, stage="GigaAM v3")
            else:
                from .worker.install_parakeet import install_parakeet
                self.reuse_task = asyncio.create_task(asyncio.to_thread(install_parakeet, self.root, profile,
                    stage_progress, lambda: self.cancel_install))
                try:
                    await asyncio.shield(self.reuse_task)
                except asyncio.CancelledError:
                    self.cancel_install = True
                    await asyncio.gather(self.reuse_task, return_exceptions=True)
                    raise
                finally:
                    self.reuse_task = None
        self._size_cache = (0.0, 0)
        progress(1, "Выбранные пакеты установлены")

    async def reuse(self, plan: dict, progress):
        from .resource_reuse import ResourceReuseCancelled, reuse_resources
        target = self.checked_root()
        self.cancel_install = False
        self.reuse_task = asyncio.create_task(asyncio.to_thread(reuse_resources, plan, target,
            self.uv(), progress, lambda: self.cancel_install))
        try:
            ready = await asyncio.shield(self.reuse_task)
            atomic_json(target / "ready.json", ready)
            self._size_cache = (0.0, 0)
        except asyncio.CancelledError:
            self.cancel_install = True
            try:
                await self.reuse_task
            except ResourceReuseCancelled:
                pass
            raise
        finally:
            self.reuse_task = None

    async def stop_install(self):
        self.cancel_install = True
        if self.process and self.process.returncode is None:
            await asyncio.to_thread(terminate_owned_process, self.process)
        if self.reuse_task and not self.reuse_task.done():
            try:
                await asyncio.shield(self.reuse_task)
            except Exception:
                pass

    async def remove(self):
        target = self.checked_root()
        await self.stop_install()
        installing = self.process
        if installing and installing.returncode is None:
            await installing.wait()
        for process in list(self.processes.values()):
            if process.returncode is None:
                await asyncio.to_thread(terminate_owned_process, process)
                await process.wait()
        self.processes.clear()
        self.checked_root()
        if not target.exists():
            return
        if not (target / ".meeting-archive-owned").is_file():
            raise ValueError("Папка не помечена как модуль Meeting Archive. Удаление остановлено")
        await asyncio.to_thread(shutil.rmtree, target)

    def python(self, external: str = "") -> str:
        if external:
            path = Path(external).expanduser().resolve()
            python = path / ".venv-gpu-ready/Scripts/python.exe"
            if not python.is_file() or not (path / "src/transcriber.py").is_file():
                raise ValueError("Выберите папку приложения с src/transcriber.py и .venv-gpu-ready")
            return str(python)
        python = self.root / "venv/Scripts/python.exe"
        from .engine_catalog import read_marker
        if not read_marker(self.root / "ready.json") or not python.is_file():
            raise ValueError("Сначала установите модуль или используйте найденные библиотеки и модели")
        return str(python)

    def require_engine(self, settings):
        if settings.external_engine and settings.engine == "whisper":
            self.python(settings.external_engine)
            return
        model = settings.model if settings.engine == "whisper" else getattr(settings, settings.engine + "_model")
        engines = self.catalogue(settings.device)["engines"]
        available = next((m for e in engines if e["id"] == settings.engine for m in e["models"] if m["id"] == model), None)
        if not available or not available["installed"]:
            raise ValueError("Установите выбранную модель в разделе локальной расшифровки")
        support = self.processing_support(settings)
        if support["requires_install"]:
            raise ValueError(support["reason"])

    async def transcribe(self, job_id: int, job: dict, external: str, progress) -> dict:
        settings = job.get("settings", {})
        model = settings.get("model", "large-v3")
        from .engine_catalog import catalogue, full_runtime_ready
        own = next(m for e in catalogue(self.root, settings.get("device", "cuda"))["engines"] if e["id"] == "whisper" for m in e["models"] if m["id"] == model)
        own_ready = own["installed"] or (model in self.discovered.get("cached_models", []) and full_runtime_ready(self.root, settings.get("device", "cuda")))
        if settings.get("diarization") and not (self.root / "venv/Lib/site-packages/pyannote/audio/__init__.py").is_file():
            own_ready = False
        if not external and settings.get("engine", "whisper") == "whisper" and not own_ready:
            python, env = await asyncio.to_thread(self.borrowed_environment, model, diarization=bool(settings.get("diarization")))
        else:
            python = self.python(external)
            env = self.env(bool(external))
        if external:
            env["MEETING_ARCHIVE_EXTERNAL_ENGINE"] = str(Path(external).resolve())
        process = await asyncio.create_subprocess_exec(python, "-B", str(PACKAGE / "worker/entry.py"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.processes[job_id] = process
        process.stdin.write((json.dumps(job, ensure_ascii=False) + "\n").encode())
        await process.stdin.drain()
        process.stdin.close()
        result, error = None, ""

        async def drain_errors():
            # Bound stderr; never persist raw worker output containing filenames/tokens.
            while await process.stderr.read(8192):
                pass

        drain = asyncio.create_task(drain_errors())
        try:
            while line := await process.stdout.readline():
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if event.get("type") == "progress":
                    progress(float(event.get("progress", 0)), event.get("message", "Обработка"))
                elif event.get("type") == "result":
                    result = event
                elif event.get("type") == "error":
                    error = self.vault.redact(event.get("message", "Ошибка модуля"))
            code = await process.wait()
            await drain
            if code or not result:
                raise RuntimeError(error or "Обработка остановлена или завершилась с ошибкой")
            return result
        finally:
            if process.returncode is None:
                await asyncio.to_thread(terminate_owned_process, process)
                await process.wait()
            drain.cancel()
            self.processes.pop(job_id, None)

    def cancel_job(self, job_id: int):
        process = self.processes.get(job_id)
        if process and process.returncode is None:
            terminate_owned_process(process)
