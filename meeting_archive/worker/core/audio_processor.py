"""Предобработка аудио: конвертация в 16kHz mono wav, шумоподавление, нормализация."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional


class AudioProcessingError(RuntimeError):
    pass


ProgressCallback = Callable[[str, float], None]   # (этап, прогресс 0..1)


def _noop(msg: str, pct: float) -> None:
    pass


def ffmpeg_executable() -> str:
    executable = os.environ.get("MEETING_ARCHIVE_FFMPEG") or shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise AudioProcessingError("FFmpeg модуля недоступен. Повторите установку модуля") from exc


def convert_to_wav(
    src: str | Path,
    dst: str | Path,
    sample_rate: int = 16000,
    mono: bool = True,
    normalize_loudness: bool = False,
    progress: ProgressCallback = _noop,
) -> None:
    """Конвертирует любой аудио/видео файл в WAV с нужными параметрами.

    Если normalize_loudness=True, применяет EBU R128 (loudnorm) — стандарт для
    нормализации речи. Это намного качественнее простой peak-нормализации.
    """
    ffmpeg = ffmpeg_executable()

    progress("Конвертация аудио…", 0.0)

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-ar", str(sample_rate),
    ]
    if mono:
        cmd += ["-ac", "1"]

    if normalize_loudness:
        # целевые значения EBU R128 для речи
        cmd += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]

    cmd += ["-c:a", "pcm_s16le", str(dst)]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.CalledProcessError as e:
        raise AudioProcessingError(
            f"ffmpeg завершился с ошибкой:\n{e.stderr}"
        ) from e

    progress("Конвертация завершена", 1.0)


def reduce_noise(
    wav_path: str | Path,
    output_path: str | Path,
    progress: ProgressCallback = _noop,
) -> None:
    """Применяет стационарное шумоподавление (noisereduce).

    Обрабатывает файл блоками по 60 секунд, чтобы не упасть на OOM
    для длинных аудио (1+ час).
    """
    try:
        import numpy as np
        import soundfile as sf
        import noisereduce as nr
    except ImportError as e:
        raise AudioProcessingError(
            f"Не установлен пакет для шумоподавления: {e}"
        ) from e

    progress("Шумоподавление…", 0.0)

    with sf.SoundFile(str(wav_path)) as src:
        sr = src.samplerate
        channels = src.channels
        total_frames = len(src)
        block_frames = sr * 60  # 60-секундные блоки

        # первые 2 секунды используем как профиль шума
        src.seek(0)
        noise_sample = src.read(frames=min(sr * 2, total_frames), dtype="float32")
        if channels > 1 and noise_sample.ndim > 1:
            noise_sample = noise_sample.mean(axis=1)

        src.seek(0)
        with sf.SoundFile(
            str(output_path),
            mode="w",
            samplerate=sr,
            channels=1,
            subtype="PCM_16",
        ) as dst:
            processed = 0
            while processed < total_frames:
                chunk = src.read(frames=block_frames, dtype="float32")
                if chunk.size == 0:
                    break
                if channels > 1 and chunk.ndim > 1:
                    chunk = chunk.mean(axis=1)

                reduced = nr.reduce_noise(
                    y=chunk,
                    sr=sr,
                    y_noise=noise_sample,
                    stationary=True,
                    prop_decrease=0.8,
                )
                dst.write(reduced.astype(np.float32))
                processed += len(chunk)
                progress("Шумоподавление…", processed / max(total_frames, 1))

    progress("Шумоподавление завершено", 1.0)


def prepare_audio(
    src: str | Path,
    *,
    use_noise_reduction: bool = True,
    use_loudness_normalization: bool = True,
    progress: ProgressCallback = _noop,
    workdir: Optional[Path] = None,
) -> Path:
    """Полный пайплайн предобработки. Возвращает путь к готовому WAV 16kHz mono.

    Файл создаётся во временной папке, вызывающий код отвечает за её удаление.
    """
    src = Path(src)
    if not src.exists():
        raise AudioProcessingError(f"Файл не найден: {src}")

    workdir = workdir or Path(tempfile.mkdtemp(prefix="lt_audio_"))
    workdir.mkdir(parents=True, exist_ok=True)

    stage1 = workdir / "converted.wav"
    convert_to_wav(
        src,
        stage1,
        sample_rate=16000,
        mono=True,
        normalize_loudness=use_loudness_normalization,
        progress=lambda m, p: progress(m, p * 0.4),
    )

    if not use_noise_reduction:
        return stage1

    stage2 = workdir / "denoised.wav"
    reduce_noise(
        stage1,
        stage2,
        progress=lambda m, p: progress(m, 0.4 + p * 0.6),
    )
    try:
        stage1.unlink(missing_ok=True)
    except Exception:
        pass
    return stage2


def concat_wav_files(
    wav_files: list[str | Path],
    destination: str | Path,
    progress: ProgressCallback = _noop,
) -> Path:
    """Склеивает подготовленные WAV-файлы в один PCM WAV без перекодирования.

    ``prepare_audio`` всегда выдаёт 16 kHz mono PCM WAV. Поэтому concat demuxer
    FFmpeg может соединить части без изменения длительности или дополнительной
    потери качества. Файл списка создаётся рядом с временными частями и
    удаляется сразу после завершения команды.
    """
    paths = [Path(path) for path in wav_files]
    if not paths:
        raise AudioProcessingError("Нечего объединять: список файлов пуст")
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise AudioProcessingError(f"Не найдены подготовленные файлы: {', '.join(missing)}")
    ffmpeg = ffmpeg_executable()

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    list_file = paths[0].parent / "concat-list.txt"

    def as_concat_path(path: Path) -> str:
        # Синтаксис concat demuxer использует одинарные кавычки. Обратные
        # слеши заменяем на прямые, чтобы Windows-пути корректно читались FFmpeg.
        value = str(path.resolve()).replace("\\", "/")
        return value.replace("'", r"'\''")

    try:
        list_file.write_text(
            "".join(f"file '{as_concat_path(path)}'\n" for path in paths),
            encoding="utf-8",
        )
        progress("Склейка аудио…", 0.0)
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:a", "pcm_s16le", str(destination),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.CalledProcessError as e:
            raise AudioProcessingError(
                f"Не удалось объединить аудио:\n{e.stderr.strip()}"
            ) from e
        progress("Аудио объединено", 1.0)
        return destination
    finally:
        try:
            list_file.unlink(missing_ok=True)
        except Exception:
            pass


def cleanup_workdir(workdir: Path) -> None:
    """Удаляет временные файлы, созданные prepare_audio."""
    try:
        if workdir and workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)
    except Exception:
        pass
