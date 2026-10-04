"""Диаризация спикеров через pyannote.audio 3.x и объединение с сегментами Whisper."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional

from .transcriber import Segment, TranscriptionResult
from .utils import DeviceInfo


ProgressCallback = Callable[[str, float, str], None]


def _noop(*_a, **_k) -> None:
    pass


class DiarizationError(RuntimeError):
    pass


class Diarizer:
    """Держит загруженный pyannote pipeline. Требует HuggingFace токен."""

    def __init__(self, device: DeviceInfo, hf_token: str):
        self.device = device
        self.hf_token = hf_token
        self._pipeline = None

    def load(self, progress: ProgressCallback = _noop) -> None:
        if self._pipeline is not None:
            return
        if not self.hf_token:
            raise DiarizationError(
                "Для диаризации нужен токен HuggingFace.\n"
                "Получите его на https://huggingface.co/settings/tokens\n"
                "и примите условия использования моделей:\n"
                "  • https://huggingface.co/pyannote/speaker-diarization-3.1\n"
                "  • https://huggingface.co/pyannote/segmentation-3.0"
            )

        progress("Загрузка модели диаризации…", 0.0, "pyannote 3.1")
        try:
            import torchaudio
            import torch

            # Compat-патч для torchaudio nightly (≥2.5): list_audio_backends удалена
            if not hasattr(torchaudio, "list_audio_backends"):
                def _list_audio_backends():
                    backends: list[str] = []
                    try:
                        import soundfile  # noqa: F401
                        backends.append("soundfile")
                    except ImportError:
                        pass
                    try:
                        torchaudio.utils.ffmpeg_utils.get_audio_decoders()
                        backends.append("ffmpeg")
                    except Exception:
                        pass
                    return backends if backends else ["soundfile"]
                torchaudio.list_audio_backends = _list_audio_backends  # type: ignore[attr-defined]

            # Compat-патч для huggingface_hub ≥0.25: use_auth_token → token
            import huggingface_hub as _hf_hub
            _orig_download = _hf_hub.hf_hub_download
            def _patched_download(*args, **kwargs):
                if "use_auth_token" in kwargs:
                    kwargs.setdefault("token", kwargs.pop("use_auth_token"))
                return _orig_download(*args, **kwargs)
            _hf_hub.hf_hub_download = _patched_download  # type: ignore[attr-defined]

            # То же для cached_download (если используется старым кодом)
            if hasattr(_hf_hub, "cached_download"):
                _orig_cached = _hf_hub.cached_download
                def _patched_cached(*args, **kwargs):
                    kwargs.pop("use_auth_token", None)
                    return _orig_cached(*args, **kwargs)
                _hf_hub.cached_download = _patched_cached  # type: ignore[attr-defined]

            # Compat-патч для PyTorch ≥2.6: weights_only по умолчанию True,
            # что ломает загрузку чекпоинтов pyannote. Принудительно
            # выставляем False (веса берутся с официального HF, доверяем).
            # Одновременно добавляем известные безопасные глобалы.
            try:
                import torch.serialization as _tser
                _safe = []
                try:
                    from torch.torch_version import TorchVersion
                    _safe.append(TorchVersion)
                except Exception:
                    pass
                if _safe and hasattr(_tser, "add_safe_globals"):
                    try:
                        _tser.add_safe_globals(_safe)
                    except Exception:
                        pass
            except Exception:
                pass

            _orig_torch_load = torch.load
            def _patched_torch_load(*args, **kwargs):
                # Форсируем weights_only=False для совместимости с pyannote
                kwargs["weights_only"] = False
                return _orig_torch_load(*args, **kwargs)
            torch.load = _patched_torch_load  # type: ignore[assignment]

            from pyannote.audio import Pipeline
        except Exception as e:
            raise DiarizationError(
                f"Не удалось загрузить pyannote.audio: {e}"
            ) from e

        try:
            # pyannote.audio 3.x использует use_auth_token; на случай будущих
            # версий, где параметр переименуют в token, пробуем оба варианта.
            try:
                pipeline = Pipeline.from_pretrained(
                    "pyannote/speaker-diarization-3.1",
                    use_auth_token=self.hf_token,
                )
            except TypeError:
                pipeline = Pipeline.from_pretrained(
                    "pyannote/speaker-diarization-3.1",
                    token=self.hf_token,
                )
        except Exception as e:
            raise DiarizationError(
                "Не удалось загрузить pyannote/speaker-diarization-3.1.\n"
                "Убедитесь что:\n"
                "  1) токен HF валиден\n"
                "  2) приняты условия использования моделей в личном кабинете HF\n"
                f"Исходная ошибка: {e}"
            ) from e
        finally:
            # Возвращаем оригинальный torch.load, чтобы патч weights_only=False
            # не протекал на другие части приложения (faster-whisper и т.д.)
            try:
                torch.load = _orig_torch_load  # type: ignore[assignment]
            except Exception:
                pass

        if self.device.type == "cuda":
            pipeline.to(torch.device("cuda"))
        self._pipeline = pipeline
        progress("Модель диаризации загружена", 1.0, "")

    def diarize(
        self,
        audio_path: str | Path,
        *,
        min_speakers: Optional[int] = None,
        max_speakers: Optional[int] = None,
        progress: ProgressCallback = _noop,
    ) -> List[dict]:
        """Возвращает список {start, end, speaker}."""
        if self._pipeline is None:
            self.load(progress=progress)

        progress("Диаризация…", 0.1, "")
        kwargs = {}
        if min_speakers and min_speakers > 0:
            kwargs["min_speakers"] = int(min_speakers)
        if max_speakers and max_speakers > 0:
            kwargs["max_speakers"] = int(max_speakers)

        diarization = self._pipeline(str(audio_path), **kwargs)
        turns = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            turns.append({
                "start": float(turn.start),
                "end": float(turn.end),
                "speaker": str(speaker),
            })
        progress("Диаризация завершена", 1.0, "")
        return turns


def merge_diarization_with_segments(
    result: TranscriptionResult,
    diarization: List[dict],
) -> TranscriptionResult:
    """Назначает каждому сегменту Whisper наиболее подходящего спикера.

    Алгоритм: для каждого сегмента считаем перекрытие с каждой репликой
    диаризации и берём спикера с максимальным перекрытием.
    Если сегмент длинный и содержит смену спикера, разбиваем его по словам.
    """
    if not diarization:
        return result

    new_segments: List[Segment] = []
    for seg in result.segments:
        # если есть пословные таймкоды и сегмент длинный, режем по словам
        if seg.words and (seg.end - seg.start) > 8.0:
            current_words: List[dict] = []
            current_speaker: Optional[str] = None
            for w in seg.words:
                spk = _best_speaker(w["start"], w["end"], diarization)
                if current_speaker is None:
                    current_speaker = spk
                if spk != current_speaker and current_words:
                    new_segments.append(_words_to_segment(current_words, current_speaker))
                    current_words = []
                    current_speaker = spk
                current_words.append(w)
            if current_words:
                new_segments.append(_words_to_segment(current_words, current_speaker))
        else:
            seg.speaker = _best_speaker(seg.start, seg.end, diarization)
            new_segments.append(seg)

    # переименовываем SPEAKER_00 → Спикер 1 для читаемости
    mapping: dict[str, str] = {}
    for s in new_segments:
        if s.speaker and s.speaker not in mapping:
            mapping[s.speaker] = f"Спикер {len(mapping) + 1}"
    for s in new_segments:
        if s.speaker:
            s.speaker = mapping[s.speaker]

    result.segments = new_segments
    return result


def _best_speaker(start: float, end: float, diarization: List[dict]) -> Optional[str]:
    """Возвращает спикера с максимальным перекрытием интервала."""
    best_speaker = None
    best_overlap = 0.0
    for turn in diarization:
        overlap = max(0.0, min(end, turn["end"]) - max(start, turn["start"]))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = turn["speaker"]
    return best_speaker


def _words_to_segment(words: List[dict], speaker: Optional[str]) -> Segment:
    return Segment(
        start=float(words[0]["start"]),
        end=float(words[-1]["end"]),
        text="".join(w["word"] for w in words).strip(),
        speaker=speaker,
        words=words,
    )
