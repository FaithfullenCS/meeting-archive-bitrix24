"""Экспорт результата транскрипции в разные форматы.

Форматы:
  * txt        — простой текст (параграфы)
  * srt / vtt  — субтитры
  * json       — полный дамп (все поля, слова, вероятности)
  * md-ai      — Markdown оптимизированный для подачи в LLM
                 (чистая структура, метаданные сверху, блоки по спикерам)
  * txt-ai     — компактная версия для AI, без лишних маркеров
"""
from __future__ import annotations

import json
import textwrap
from datetime import datetime
from pathlib import Path

from .transcriber import TranscriptionResult
from .utils import format_timestamp_srt, format_timestamp_vtt, format_duration


def _source_label(result: TranscriptionResult, source_file: str) -> str:
    if len(result.source_files) > 1:
        return f"Пакет из {len(result.source_files)} файлов"
    if result.source_files:
        return Path(result.source_files[0]).name
    return Path(source_file).name if source_file else "не указан"


def _header_ai(result: TranscriptionResult, source_file: str) -> str:
    probability = (f"уверенность {result.language_probability:.2f}" if result.language_probability is not None
                   else "вероятность не предоставлена движком")
    return textwrap.dedent(f"""\
        # Транскрипция аудио

        **Источник:** {_source_label(result, source_file)}
        **Длительность:** {format_duration(result.duration)}
        **Модель:** {result.model_name}
        **Устройство:** {result.device}
        **Язык:** {result.language} ({probability})
        **Дата транскрипции:** {datetime.now().strftime("%Y-%m-%d %H:%M")}

        ---

        """)


# ---------- TXT ----------
def export_txt(result: TranscriptionResult, path: str | Path, *, with_timestamps: bool = False) -> None:
    """Простой текст: абзацы по спикерам или по паузам."""
    lines: list[str] = []
    current_speaker: str | None = None
    current_source: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        text = " ".join(buffer).strip()
        if current_speaker:
            lines.append(f"[{current_speaker}] {text}")
        else:
            lines.append(text)
        buffer.clear()

    for seg in result.segments:
        if seg.source_file != current_source:
            flush()
            current_source = seg.source_file
            current_speaker = None
            if current_source:
                lines.append(f"=== {current_source} ===")
        if seg.speaker != current_speaker:
            flush()
            current_speaker = seg.speaker
        ts = f"[{format_duration(seg.start)}] " if with_timestamps else ""
        buffer.append(ts + seg.text.strip())
    flush()

    Path(path).write_text("\n\n".join(lines), encoding="utf-8")


# ---------- SRT ----------
def export_srt(result: TranscriptionResult, path: str | Path) -> None:
    out: list[str] = []
    for i, seg in enumerate(result.segments, start=1):
        text = seg.text.strip()
        if seg.speaker:
            text = f"[{seg.speaker}] {text}"
        out.append(
            f"{i}\n"
            f"{format_timestamp_srt(seg.start)} --> {format_timestamp_srt(seg.end)}\n"
            f"{text}\n"
        )
    Path(path).write_text("\n".join(out), encoding="utf-8")


# ---------- VTT ----------
def export_vtt(result: TranscriptionResult, path: str | Path) -> None:
    out: list[str] = ["WEBVTT", ""]
    for seg in result.segments:
        text = seg.text.strip()
        if seg.speaker:
            text = f"<v {seg.speaker}>{text}"
        out.append(
            f"{format_timestamp_vtt(seg.start)} --> {format_timestamp_vtt(seg.end)}\n"
            f"{text}\n"
        )
    Path(path).write_text("\n".join(out), encoding="utf-8")


# ---------- JSON ----------
def export_json(result: TranscriptionResult, path: str | Path) -> None:
    payload = {
        "metadata": {
            "language": result.language,
            "language_probability": result.language_probability,
            "duration": result.duration,
            "model": result.model_name,
            "device": result.device,
            "source_files": result.source_files,
            "exported_at": datetime.now().isoformat(),
        },
        "segments": [s.to_dict() for s in result.segments],
        "full_text": result.full_text(),
    }
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------- Markdown для AI ----------
def export_md_ai(result: TranscriptionResult, path: str | Path, source_file: str = "") -> None:
    """Markdown с чистой структурой, удобной для подачи в LLM.

    Формат:
        # Заголовок + метаданные
        ---
        ## [00:00 – 00:45] Спикер 1
        текст…

        ## [00:45 – 01:22] Спикер 2
        текст…
    """
    out: list[str] = [_header_ai(result, source_file)]

    current_speaker: str | None = None
    current_source: str | None = None
    block_start: float | None = None
    block_end: float | None = None
    block_text: list[str] = []

    def flush() -> None:
        if not block_text:
            return
        header = f"## [{format_duration(block_start)} – {format_duration(block_end)}]"
        if current_speaker:
            header += f" {current_speaker}"
        out.append(header)
        out.append("")
        out.append(" ".join(block_text).strip())
        out.append("")

    for seg in result.segments:
        if seg.source_file != current_source:
            flush()
            current_source = seg.source_file
            current_speaker = None
            block_start = None
            block_end = None
            block_text = []
            if current_source:
                out.append(f"# {current_source}")
                out.append("")
        # новый блок если сменился спикер или прошла большая пауза
        gap = (seg.start - (block_end or seg.start)) if block_end is not None else 0
        if (
            block_start is None
            or current_speaker != seg.speaker
            or gap > 5.0
            or (block_end is not None and seg.end - (block_start or 0) > 120)
        ):
            flush()
            current_speaker = seg.speaker
            block_start = seg.start
            block_text = []

        block_end = seg.end
        block_text.append(seg.text.strip())

    flush()
    Path(path).write_text("\n".join(out), encoding="utf-8")


# ---------- компактный TXT для AI ----------
def export_txt_ai(result: TranscriptionResult, path: str | Path, source_file: str = "") -> None:
    """Плоский текст с минимальной разметкой для LLM.

    Пример:
        [Источник: lecture.mp3 · 58:42 · large-v3]
        Спикер 1 (00:00–00:45): текст…
        Спикер 2 (00:45–01:22): текст…
    """
    meta = (
        f"[Источник: {_source_label(result, source_file)} · "
        f"{format_duration(result.duration)} · {result.model_name}]"
    )
    out: list[str] = [meta, ""]

    current_speaker: str | None = None
    current_source: str | None = None
    block_start: float | None = None
    block_end: float | None = None
    block_text: list[str] = []

    def flush() -> None:
        if not block_text:
            return
        prefix = (
            f"{current_speaker} ({format_duration(block_start)}–{format_duration(block_end)}): "
            if current_speaker
            else f"({format_duration(block_start)}–{format_duration(block_end)}) "
        )
        out.append(prefix + " ".join(block_text).strip())

    for seg in result.segments:
        if seg.source_file != current_source:
            flush()
            current_source = seg.source_file
            current_speaker = None
            block_start = None
            block_end = None
            block_text = []
            if current_source:
                out.extend(["", f"=== {current_source} ==="])
        if block_start is None or current_speaker != seg.speaker:
            flush()
            current_speaker = seg.speaker
            block_start = seg.start
            block_text = []
        block_end = seg.end
        block_text.append(seg.text.strip())
    flush()

    Path(path).write_text("\n".join(out), encoding="utf-8")


# ---------- реестр ----------
EXPORTERS = {
    "txt":      ("Текст (.txt)",                              "txt",  export_txt),
    "txt_ts":   ("Текст с таймкодами (.txt)",                 "txt",  lambda r, p, **_: export_txt(r, p, with_timestamps=True)),
    "srt":      ("Субтитры SRT (.srt)",                       "srt",  export_srt),
    "vtt":      ("Субтитры WebVTT (.vtt)",                    "vtt",  export_vtt),
    "json":     ("JSON со всеми данными (.json)",             "json", export_json),
    "md_ai":    ("Markdown для AI (.md)",                     "md",   export_md_ai),
    "txt_ai":   ("Компактный текст для AI (.txt)",            "txt",  export_txt_ai),
}


def export(kind: str, result: TranscriptionResult, path: str | Path, source_file: str = "") -> None:
    if kind not in EXPORTERS:
        raise ValueError(f"Неизвестный формат экспорта: {kind}")
    _, _, func = EXPORTERS[kind]
    try:
        func(result, path, source_file=source_file)     # type: ignore[call-arg]
    except TypeError:
        func(result, path)
