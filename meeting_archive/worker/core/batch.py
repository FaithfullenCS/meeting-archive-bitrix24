"""Пакетная обработка и объединение результатов расшифровки."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable

from .transcriber import Segment, TranscriptionResult


def combine_transcription_results(
    items: Iterable[tuple[Path, TranscriptionResult]],
) -> TranscriptionResult:
    """Соединяет результаты в порядке очереди с непрерывными таймкодами.

    В сегментах остаётся имя исходника. Это позволяет текстовым экспортам
    вывести понятные разделители файлов, а SRT/VTT сохранить как единую
    временную шкалу без искусственных служебных субтитров.
    """
    collected = list(items)
    if not collected:
        raise ValueError("Нельзя объединить пустой список расшифровок")

    offset = 0.0
    segments: list[Segment] = []
    source_files: list[str] = []
    weighted_probability = 0.0
    total_weight = 0.0
    languages: set[str] = set()

    for source, result in collected:
        source_name = source.name
        source_files.append(str(source))
        languages.add(result.language)
        weight = max(result.duration, 0.0)
        if result.language_probability is not None:
            weighted_probability += result.language_probability * weight
            total_weight += weight

        for segment in result.segments:
            words = [
                {
                    **word,
                    "start": float(word.get("start", 0.0)) + offset,
                    "end": float(word.get("end", 0.0)) + offset,
                }
                for word in segment.words
            ]
            segments.append(
                replace(
                    segment,
                    start=segment.start + offset,
                    end=segment.end + offset,
                    words=words,
                    source_file=source_name,
                )
            )
        offset += weight

    first = collected[0][1]
    return TranscriptionResult(
        segments=segments,
        language=first.language if len(languages) == 1 else "mixed",
        language_probability=(weighted_probability / total_weight) if total_weight else None,
        duration=offset,
        model_name=first.model_name,
        device=first.device,
        source_files=source_files,
    )
