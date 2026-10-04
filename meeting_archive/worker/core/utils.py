"""Утилиты: определение устройства, форматирование времени, оценка времени транскрипции."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class DeviceInfo:
    type: str           # "cuda" | "cpu"
    name: str           # имя устройства
    vram_gb: float      # только для GPU
    compute_type: str   # "float16" / "int8_float16" / "int8"

    def __str__(self) -> str:
        if self.type == "cuda":
            return f"GPU: {self.name} ({self.vram_gb:.1f} GB VRAM, {self.compute_type})"
        return f"CPU ({self.compute_type})"


def detect_device() -> DeviceInfo:
    """Определяет доступное устройство и оптимальный compute_type."""
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            vram = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            # float16 оптимально для RTX 30/40/50
            compute = "float16" if vram >= 4.0 else "int8_float16"
            return DeviceInfo("cuda", name, vram, compute)
    except Exception:
        pass
    return DeviceInfo("cpu", "CPU", 0.0, "int8")


def format_duration(seconds: float) -> str:
    """Форматирует секунды в ЧЧ:ММ:СС."""
    if seconds is None or seconds < 0:
        return "--:--:--"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_timestamp_srt(seconds: float) -> str:
    """ЧЧ:ММ:СС,мс (формат SRT)."""
    if seconds is None or seconds < 0:
        seconds = 0
    s, ms = divmod(int(round(seconds * 1000)), 1000)
    h = s // 3600
    m = (s % 3600) // 60
    s = s % 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_timestamp_vtt(seconds: float) -> str:
    """ЧЧ:ММ:СС.мс (формат WebVTT)."""
    return format_timestamp_srt(seconds).replace(",", ".")


def format_size(num_bytes: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if num_bytes < 1024 or unit == "ТБ":
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} ТБ"


# --- Расчётное время транскрипции ---
# Эмпирические коэффициенты realtime factor для faster-whisper
# (во сколько раз быстрее реального времени аудио)
_RTF_GPU = {
    "tiny":     80.0,
    "base":     60.0,
    "small":    40.0,
    "medium":   25.0,
    "large-v2": 14.0,
    "large-v3": 12.0,
}
_RTF_CPU = {
    "tiny":     6.0,
    "base":     4.0,
    "small":    2.0,
    "medium":   0.8,
    "large-v2": 0.3,
    "large-v3": 0.25,
}


def estimate_transcription_time(
    audio_duration_s: float,
    model_name: str,
    device: DeviceInfo,
    diarize: bool = False,
) -> float:
    """Оценивает длительность транскрипции в секундах."""
    table = _RTF_GPU if device.type == "cuda" else _RTF_CPU
    rtf = table.get(model_name, 10.0 if device.type == "cuda" else 1.0)
    base = audio_duration_s / rtf
    # диаризация добавляет ~1x реального времени на CPU, ~0.15x на GPU
    if diarize:
        base += audio_duration_s * (0.15 if device.type == "cuda" else 1.0)
    # оверхед на загрузку модели + предобработку
    return base + 15.0


def get_audio_duration(path: str | Path) -> Optional[float]:
    """Возвращает длительность аудиофайла в секундах через ffprobe, либо None."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        return float(out.strip())
    except Exception:
        return None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def app_data_dir() -> Path:
    """Папка для настроек и кэша приложения."""
    base = os.environ.get("MEETING_ARCHIVE_HOME") or str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MeetingArchive")
    p = Path(base) / "module"
    p.mkdir(parents=True, exist_ok=True)
    return p
