"""Standalone optional worker. JSON on stdin, newline-delimited events on stdout."""
from __future__ import annotations

import contextlib
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

try:
    from .core.model_catalog import WHISPER_MODELS
except ImportError:
    from core.model_catalog import WHISPER_MODELS


def emit(kind: str, **data):
    sys.__stdout__.write(json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n")
    sys.__stdout__.flush()


def core():
    sys.path.insert(0, str(Path(__file__).parents[2]))
    sys.path.insert(0, str(Path(__file__).parent))
    modules = {name: importlib.import_module("core." + name) for name in
               ("utils", "transcriber", "audio_processor", "exporters", "batch", "diarizer")}
    external = os.environ.get("MEETING_ARCHIVE_EXTERNAL_ENGINE")
    if external:
        sys.path.insert(0, external)
        # Reuse inference only. Preparation/exports remain supported by this app,
        # including FFmpeg fallback and corrected subtitle timestamp rounding.
        for name in ("transcriber", "diarizer"):
            modules[name] = importlib.import_module("src." + name)
    return modules


def run(job: dict):
    modules = core()
    utils, transcriber = modules["utils"], modules["transcriber"]
    settings = job.get("settings", {})
    engine = settings.get("engine", "whisper")
    if engine not in {"whisper", "parakeet", "gigaam"}:
        raise ValueError("Неизвестный движок расшифровки")
    requested = settings.get("device", "cuda")
    device = (utils.DeviceInfo("cuda", "Native CUDA", 0, "float16")
              if engine == "parakeet" and requested == "cuda" else utils.detect_device())
    if requested == "cpu":
        if not settings.get("cpu_confirmed"):
            raise RuntimeError("Подтвердите медленную обработку на CPU в настройках")
        device = utils.DeviceInfo("cpu", "CPU", 0, "int8")
    elif requested != "cuda" or device.type != "cuda":
        raise RuntimeError("Совместимая CUDA-видеокарта недоступна. Автоматический переход на CPU отключён")
    if requested == "cuda" and engine != "parakeet":
        import torch
        torch.cuda.init()
        major, minor = torch.cuda.get_device_capability()
        if f"sm_{major}{minor}" not in torch.cuda.get_arch_list():
            raise RuntimeError("Установленный PyTorch не поддерживает архитектуру видеокарты")
    model = settings.get("model", "large-v3") if engine == "whisper" else settings.get(engine + "_model")
    if engine == "whisper":
        if model not in WHISPER_MODELS:
            raise ValueError("Неизвестная модель Whisper")
    else:
        transcriber = importlib.import_module("core." + engine)
    asr = transcriber.Transcriber(model, device)
    output = Path(job["output"])
    output.mkdir(parents=True, exist_ok=True)
    sources = [Path(p) for p in job["files"]]
    if not sources or any(not p.is_file() for p in sources):
        raise RuntimeError("Один из исходных файлов недоступен")
    started = time.monotonic()
    items = []
    diarizer = None
    if settings.get("diarization"):
        token = os.environ.get("HF_TOKEN", "")
        if not token:
            raise RuntimeError("Для диаризации добавьте токен Hugging Face и проверьте доступ")
        diarizer = modules["diarizer"].Diarizer(device, token)

    def progress(message, pct, hint=""):
        emit("progress", progress=pct, message=message + (" · " + hint if hint else ""))

    with tempfile.TemporaryDirectory(prefix="meeting-worker-") as temporary:
        work = Path(temporary)
        prepared = []
        for i, source in enumerate(sources):
            part = work / str(i)
            part.mkdir()
            wav = modules["audio_processor"].prepare_audio(source,
                use_noise_reduction=settings.get("noise_reduction", False),
                use_loudness_normalization=settings.get("normalize", False), workdir=part,
                progress=lambda m, p: progress(m, (i + p * .2) / len(sources)))
            prepared.append(wav)
        if job.get("mode") == "merged_wav":
            merged = output / "merged.wav"
            modules["audio_processor"].concat_wav_files(prepared, merged)
            pairs = [(sources[0], merged)]
        else:
            pairs = list(zip(sources, prepared))
        for i, (source, wav) in enumerate(pairs):
            if job.get("sample_seconds"):
                import soundfile as sf
                with sf.SoundFile(wav) as stream:
                    sample_rate = stream.samplerate
                    audio = stream.read(frames=int(sample_rate * job["sample_seconds"]))
                sample = work / "sample.wav"
                sf.write(sample, audio[:int(sample_rate * job["sample_seconds"])], sample_rate)
                wav = sample
            language = settings.get("language")
            result = asr.transcribe(wav, language=None if language in (None, "", "auto") else language,
                use_vad=settings.get("vad", True), word_timestamps=True,
                progress=lambda m, p, h: progress(m, (i + .2 + p * .7) / len(pairs), h))
            if diarizer:
                turns = diarizer.diarize(wav, min_speakers=settings.get("min_speakers") or None,
                    max_speakers=settings.get("max_speakers") or None, progress=progress)
                result = modules["diarizer"].merge_diarization_with_segments(result, turns)
            items.append((source, result))
        combined = modules["batch"].combine_transcription_results(items)
        # Public exports retain archive-relative references, not this PC's drive paths.
        source_files = job.get("source_files") or [p.name for p in sources]
        combined.source_files = source_files
        exports = modules["exporters"]
        exports.export_json(combined, output / "transcript.json")
        exports.export_txt(combined, output / "transcript.txt", with_timestamps=True)
        exports.export_md_ai(combined, output / "transcript.md", source_file="; ".join(p.name for p in sources))
        exports.export_srt(combined, output / "transcript.srt")
        exports.export_vtt(combined, output / "transcript.vtt")
        elapsed = time.monotonic() - started
        metadata = {"engine": engine, "model": model, "device": device.type, "diarization": bool(diarizer),
                    "seconds": elapsed, "audio_seconds": combined.duration, "sample": bool(job.get("sample_seconds")),
                    "source_files": source_files, "source_path_base": "meeting" if job.get("source_files") else "filename",
                    "mode": job.get("mode", "separate"), "quality": "На вашей записи не проверено"}
        (output / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), "utf-8")
        emit("result", output=str(output), **metadata)
    asr.unload()


def main():
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if len(sys.argv) > 2 and sys.argv[1] == "--download":
                from huggingface_hub import snapshot_download
                sys.path.insert(0, str(Path(__file__).parent))
                from core.model_catalog import whisper_repo
                snapshot_download(whisper_repo(sys.argv[2]), allow_patterns=["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"], token=False)
                emit("result", message="Модель загружена")
            else:
                job = json.loads(sys.stdin.readline())
                run(job)
    except Exception as exc:
        message = str(exc)
        token = os.environ.get("HF_TOKEN")
        if token:
            message = message.replace(token, "[скрыто]")
        if "out of memory" in message.lower():
            message = "Недостаточно памяти. Выберите меньшую модель или освободите память; CPU автоматически не включается"
        emit("error", message=message[:800])
        sys.exit(1)


if __name__ == "__main__":
    main()
