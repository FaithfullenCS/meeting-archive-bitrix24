"""Обёртка над faster-whisper.

Даёт streaming-прогресс и возвращает список сегментов в единообразном виде.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from .utils import DeviceInfo
from .model_catalog import WHISPER_MODELS, whisper_repo


# список поддерживаемых моделей, отсортированных от маленькой к большой
AVAILABLE_MODELS = list(WHISPER_MODELS)


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: Optional[str] = None
    words: List[dict] = field(default_factory=list)
    source_file: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "speaker": self.speaker,
            "words": self.words,
            "source_file": self.source_file,
        }


@dataclass
class TranscriptionResult:
    segments: List[Segment]
    language: str
    language_probability: float | None
    duration: float
    model_name: str
    device: str
    source_files: List[str] = field(default_factory=list)

    def full_text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())


# callback: (этап, прогресс 0..1, подсказка)
ProgressCallback = Callable[[str, float, str], None]


def _noop(*_a, **_k) -> None:
    pass


def installed_model_path(model_name: str) -> Path:
    """Resolve only complete local snapshots, including newer CT2 models.

    Passing a directory keeps faster-whisper 1.0.3 compatible with turbo/en
    variants and prevents inference from implicitly downloading a model.
    """
    if model_name not in AVAILABLE_MODELS:
        raise ValueError("Неизвестная модель Whisper")
    repo = whisper_repo(model_name)
    cache = Path(os.environ.get("HF_HUB_CACHE") or
                 str(Path(os.environ.get("HF_HOME", str(Path.home() / ".cache/huggingface"))) / "hub"))
    caches = [cache]
    shared = os.environ.get("MEETING_ARCHIVE_MODEL_CACHE")
    if shared and Path(shared) not in caches:
        caches.append(Path(shared))
    for cache in caches:
        directory = cache / ("models--" + repo.replace("/", "--"))
        candidates = []
        reference = directory / "refs/main"
        if reference.is_file():
            commit = reference.read_text("utf-8").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", commit):
                raise RuntimeError("Некорректная ссылка на локальную модель Whisper")
            candidates.append(directory / "snapshots" / commit)
        candidates.extend(sorted(directory.glob("snapshots/*"), key=lambda p: p.name, reverse=True))
        for snapshot in candidates:
            if all((snapshot / file).is_file() and (snapshot / file).stat().st_size > 0
                   for file in ("model.bin", "config.json", "tokenizer.json")) and any(
                       (snapshot / file).is_file() and (snapshot / file).stat().st_size > 0
                       for file in ("vocabulary.json", "vocabulary.txt")):
                return snapshot
    raise RuntimeError("Модель " + model_name + " не установлена. Выберите её в каталоге и нажмите «Установить»")


class Transcriber:
    """Оборачивает faster-whisper.WhisperModel. Модель держится в памяти
    между вызовами transcribe, чтобы не перезагружать её каждый раз."""

    def __init__(self, model_name: str, device: DeviceInfo):
        self.model_name = model_name
        self.device = device
        self._model = None
        self._cancel = False

    # ---- управление ----
    def load(self, progress: ProgressCallback = _noop) -> None:
        if self._model is not None:
            return
        progress("Загрузка модели…", 0.0, f"Модель: {self.model_name}")
        if self.device.type == "cuda":
            # На Windows DLL CUDA из колеса PyTorch должны быть загружены до
            # инициализации CTranslate2, используемого faster-whisper.
            import torch

            torch.cuda.init()
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError(
                "Не установлен пакет faster-whisper. Нажмите «Установить модуль» в Meeting Archive."
            ) from e

        self._model = WhisperModel(
            str(installed_model_path(self.model_name)),
            device=self.device.type,
            compute_type=self.device.compute_type,
            local_files_only=True,
        )
        progress("Модель загружена", 1.0, "")

    def cancel(self) -> None:
        self._cancel = True

    def unload(self) -> None:
        self._model = None
        try:
            import gc
            import torch  # type: ignore
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    # ---- основной метод ----
    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: Optional[str] = None,
        use_vad: bool = True,
        word_timestamps: bool = True,
        initial_prompt: Optional[str] = None,
        beam_size: int = 5,
        progress: ProgressCallback = _noop,
    ) -> TranscriptionResult:
        self._cancel = False
        self.load(progress=lambda m, p, h: progress(m, p * 0.05, h))

        progress("Транскрипция…", 0.05, "Старт")

        vad_params = dict(
            min_silence_duration_ms=500,
            speech_pad_ms=200,
        )

        # faster-whisper 1.0.3 cannot detect a language after VAD removed
        # all audio. Check this case before inference instead of guessing text.
        if use_vad and language is None:
            from faster_whisper.audio import decode_audio
            from faster_whisper.vad import VadOptions, get_speech_timestamps
            audio = decode_audio(str(audio_path), sampling_rate=16000)
            if not get_speech_timestamps(audio, VadOptions(**vad_params)):
                progress("Речь не обнаружена", 0.95, "")
                return TranscriptionResult([], "", None, len(audio) / 16000,
                                           self.model_name, str(self.device))

        segments_iter, info = self._model.transcribe(
            str(audio_path),
            language=language,
            beam_size=beam_size,
            vad_filter=use_vad,
            vad_parameters=vad_params,
            word_timestamps=word_timestamps,
            condition_on_previous_text=True,
            initial_prompt=initial_prompt,
        )

        total_duration = float(info.duration) or 1.0
        collected: List[Segment] = []

        for seg in segments_iter:
            if self._cancel:
                raise RuntimeError("Транскрипция отменена пользователем")

            words = []
            if seg.words:
                words = [
                    {"start": w.start, "end": w.end, "word": w.word, "prob": w.probability}
                    for w in seg.words
                ]

            collected.append(
                Segment(
                    start=float(seg.start),
                    end=float(seg.end),
                    text=seg.text.strip(),
                    words=words,
                )
            )

            # прогресс 5% старт + 90% на транскрипцию (+5% на пост-обработку потом)
            p = 0.05 + 0.9 * min(seg.end / total_duration, 1.0)
            progress(
                "Транскрипция…",
                p,
                f"{_fmt(seg.end)} / {_fmt(total_duration)}",
            )

        progress("Транскрипция завершена", 0.95, "")

        return TranscriptionResult(
            segments=collected,
            language=info.language,
            language_probability=float(info.language_probability),
            duration=total_duration,
            model_name=self.model_name,
            device=str(self.device),
        )


def _fmt(sec: float) -> str:
    sec = int(max(0, sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
