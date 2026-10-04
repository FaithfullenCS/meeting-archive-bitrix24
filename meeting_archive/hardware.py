from __future__ import annotations

import json
import os
import platform
import subprocess

import psutil
from .engine_catalog import WHISPER_MODELS

MODELS = [{"name": name, "download_gb": size, "vram_gb": vram, "ram_gb": ram,
           "label": "Только английский" if name.endswith(".en") else "Многоязычная; качество зависит от записи"}
          for name, (size, vram, ram) in WHISPER_MODELS.items()]


def native_architecture() -> str:
    # An emulated x64 process can report AMD64 on an ARM64 Windows host.
    if os.name == "nt":
        try:
            import ctypes
            process, native = ctypes.c_ushort(), ctypes.c_ushort()
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            kernel.IsWow64Process2.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ushort)]
            kernel.IsWow64Process2.restype = ctypes.c_bool
            if kernel.IsWow64Process2(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
                return {0xAA64: "arm64", 0x8664: "x64", 0x014C: "x86"}.get(native.value, "unknown")
        except (AttributeError, OSError):
            pass
    name = os.environ.get("PROCESSOR_ARCHITEW6432") or platform.machine()
    return {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(name.lower(), name.lower())


def runtime_plan(profile: str, hardware: dict | None = None) -> dict:
    """Select a pinned Windows runtime from host architecture, GPU and driver.

    Driver thresholds are a conservative installation policy using the toolkit
    GA driver versions. No CPU fallback or system driver installation is implicit.
    Existing compatible borrowed environments are probed directly instead.
    """
    if profile not in {"cpu", "cuda"}:
        raise ValueError("Выберите CPU или GPU NVIDIA")
    hardware = hardware if hardware is not None else ({"architecture": native_architecture()} if profile == "cpu" else detect())
    result = {"available": False, "profile": profile, "lock_profile": "", "description": "", "reason": ""}
    architecture = hardware.get("architecture", "x64")
    if architecture != "x64":
        result["reason"] = "Автоматическая установка расшифровки доступна для Windows x64. Windows ARM пока требует отдельной сборки; скачивание остановлено"
        return result
    if profile == "cpu":
        result.update(available=True, lock_profile="cpu", description="CPU — процессоры Intel и AMD x64")
        return result
    gpu = hardware.get("gpu")
    if not gpu:
        result["reason"] = "GPU NVIDIA не обнаружена. Для AMD, Intel и компьютеров без NVIDIA выберите CPU в профиле расшифровки"
        return result
    try:
        capability = float(gpu["compute_capability"])
        driver = tuple(int(part) for part in gpu["driver"].split("."))
    except (KeyError, TypeError, ValueError):
        result["reason"] = "Не удалось определить архитектуру GPU или драйвер NVIDIA. Обновите драйвер либо выберите CPU"
        return result
    if not 5 <= capability <= 12:
        result["reason"] = "Для этой архитектуры NVIDIA нет проверенного комплекта библиотек в приложении. Выберите CPU"
        return result
    cuda, lock, minimum = ("12.8", "cuda", (570, 65)) if capability >= 10 else ("12.6", "cuda126", (560, 76))
    if driver < minimum:
        result["reason"] = f"Для выбранного комплекта CUDA {cuda} требуется драйвер NVIDIA {'.'.join(map(str, minimum))} или новее. Обновите драйвер либо выберите CPU"
        return result
    result.update(available=True, lock_profile=lock, cuda_version=cuda,
                  description=f"GPU NVIDIA — CUDA {cuda}, комплект выбран по видеокарте и драйверу")
    return result


def windows_device_names() -> dict:
    if os.name != "nt":
        return {"cpu_name": platform.processor(), "gpu_adapters": []}
    cpu = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            cpu = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    except OSError:
        pass
    adapters = []
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                 "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
                                capture_output=True, text=True, timeout=8,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            adapters = [name.strip() for name in result.stdout.splitlines() if name.strip()]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"cpu_name": cpu, "gpu_adapters": adapters}


def detect() -> dict:
    memory = psutil.virtual_memory()
    gpu = None
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version,compute_cap", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            parts = result.stdout.splitlines()[0].split(",")
            gpu = {"name": parts[0].strip(), "vram_gb": round(float(parts[1]) / 1024, 1),
                   "free_gb": round(float(parts[2]) / 1024, 1), "driver": parts[3].strip(),
                   "compute_capability": parts[4].strip()}
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    ram = round(memory.total / 1024**3, 1)
    catalogue = []
    for model in MODELS:
        possible = ram >= model["ram_gb"] and bool(gpu and gpu["free_gb"] >= model["vram_gb"] * 1.1)
        catalogue.append({**model, "gpu_estimate": possible, "cpu_estimate": ram >= model["ram_gb"] * 1.2,
                          "comparison": "Качество относительно Bitrix24 на вашей записи не проверено"})
    candidates = [m["name"] for m in catalogue if m["gpu_estimate"]]
    return {**windows_device_names(), "architecture": native_architecture(),
        "cpu_cores": psutil.cpu_count(logical=False), "cpu_threads": psutil.cpu_count(), "ram_gb": ram,
        "available_ram_gb": round(memory.available / 1024**3, 1), "gpu": gpu,
        "cuda": "Не проверена в выбранном профиле" if gpu else "GPU NVIDIA недоступна; можно использовать CPU",
        "recommended": "large-v3" if "large-v3" in candidates else (candidates[-1] if candidates else "base"), "models": catalogue,
        "note": "Оценки памяти приблизительные, с запасом. Максимально проверенная модель определяется коротким тестом; таблица не гарантирует запуск."}


def worker_probe(python: str, env: dict) -> dict:
    code = "import json,torch; ok=torch.cuda.is_available(); x=torch.ones(1,device='cuda')+1 if ok else None; torch.cuda.synchronize() if ok else None; print(json.dumps({'cuda':ok,'torch':torch.__version__,'arch':torch.cuda.get_arch_list() if ok else [],'capability':list(torch.cuda.get_device_capability()) if ok else []}))"
    result = subprocess.run([python, "-B", "-c", code], env=env, capture_output=True, text=True, timeout=40,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("Проверка CUDA не прошла. Проверьте установку модуля и драйвер NVIDIA")
    data = json.loads(result.stdout.splitlines()[-1])
    # Executing and synchronizing a real kernel proves support. Exact sm_* name
    # matching would wrongly reject GPUs that use compatible cubins or PTX.
    data["compatible"] = bool(data["cuda"])
    return data
