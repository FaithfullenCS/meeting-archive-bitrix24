"""Independent native Parakeet adapter with bounded audio/GPU request sizes.

NVIDIA NeMo-Speech.cpp v0.1.0 directory mode loads the model once, runs with
concurrency=1 and writes a JSON for each PCM chunk. Explicit backend selection
and native doctor guard prohibit silently switching a CUDA request to CPU.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import wave
from pathlib import Path

from .transcriber import Segment, TranscriptionResult
from .utils import DeviceInfo

try:
    from ..install_parakeet import MODEL_ID, installed_paths, probe_parakeet
except ImportError:  # Standalone worker imports this package as core.parakeet.
    from install_parakeet import MODEL_ID, installed_paths, probe_parakeet

CHUNK_SECONDS = 30
OVERLAP_SECONDS = .5


def _noop(*_args):
    pass


def _stop(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
            timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def prepare_chunks(source: Path, destination: Path, cancelled=lambda: False) -> tuple[list[dict], float]:
    """Read at most 31s of PCM data, preserving original timeline and overlap."""
    destination.mkdir(parents=True)
    chunks = []
    with wave.open(str(source), "rb") as audio:
        rate, frames = audio.getframerate(), audio.getnframes()
        if rate <= 0 or audio.getcomptype() != "NONE" or audio.getsampwidth() != 2:
            raise ValueError("Parakeet ожидает подготовленный PCM16 WAV")
        block, overlap = CHUNK_SECONDS * rate, int(OVERLAP_SECONDS * rate)
        for index, start in enumerate(range(0, frames, block)):
            if cancelled():
                raise RuntimeError("Расшифровка Parakeet отменена")
            end = min(frames, start + block)
            input_start, input_end = max(0, start - overlap), min(frames, end + overlap)
            path = destination / (f"chunk-{index:06}.wav")
            audio.setpos(input_start)
            data = audio.readframes(input_end - input_start)
            with wave.open(str(path), "wb") as output:
                output.setparams(audio.getparams())
                output.writeframes(data)
            chunks.append({"input": path, "offset": input_start / rate, "start": start / rate,
                           "end": end / rate, "final": end == frames})
    return chunks, frames / rate


def parse_chunk(data: dict, chunk: dict, *, pauses: bool = True) -> list[Segment]:
    """Keep each native timed word in exactly one overlap ownership interval."""
    words = []
    for original in data.get("words", []):
        start = float(original["start"]) + chunk["offset"]
        end = float(original["end"]) + chunk["offset"]
        midpoint = (start + end) / 2
        if midpoint < chunk["start"] or midpoint > chunk["end"] or (midpoint == chunk["end"] and not chunk["final"]):
            continue
        if end < start:
            raise RuntimeError("Native Parakeet вернул некорректные таймкоды")
        words.append({"word": str(original["word"]), "start": max(0, start), "end": max(0, end)})
    if not words:
        if data.get("text", "").strip() and not data.get("words"):
            raise RuntimeError("Parakeet вернул текст без таймкодов; повторите проверку native runtime")
        return []
    segments, pending = [], []

    def flush():
        if pending:
            segments.append(Segment(pending[0]["start"], pending[-1]["end"],
                " ".join(w["word"].strip() for w in pending).strip(), words=list(pending)))
            pending.clear()

    for word in words:
        if pending and ((pauses and word["start"] - pending[-1]["end"] > .8)
                        or word["end"] - pending[0]["start"] > 12):
            flush()
        pending.append(word)
        if word["word"].rstrip().endswith((".", "?", "!", "…")):
            flush()
    flush()
    return segments


class ParakeetTranscriber:
    def __init__(self, model_name: str, device: DeviceInfo):
        if model_name not in {MODEL_ID, "tdt-0.6b-v3-q8"}:
            raise ValueError("Неизвестная модель Parakeet")
        if device.type not in {"cuda", "cpu"}:
            raise ValueError("Выберите CUDA или явно подтверждённый CPU")
        self.model_name, self.device = MODEL_ID, device
        self._cancelled = False
        self._process = None
        self._binary = self._model = None
        self.timestamp_quality = "native-word-timestamps"
        self.pause_detection = "alignment-gaps"

    def _root(self):
        home = os.environ.get("MEETING_ARCHIVE_PARAKEET_HOME")
        if home:
            engine = Path(home).expanduser().absolute()
            if engine.name != "parakeet" or engine.parent.name != "engines":
                raise ValueError("Некорректная папка установленного Parakeet")
            return engine.parent.parent
        home = Path(os.environ.get("MEETING_ARCHIVE_HOME") or
                    str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MeetingArchive"))
        return home / "module"

    def load(self, progress=_noop):
        if self._binary is not None:
            return
        progress("Проверка native Parakeet…", 0, "")
        root = self._root()
        report = probe_parakeet(root, self.device.type)
        if not report["compatible"]:
            raise RuntimeError(report["reason"])
        self._binary, self._model = installed_paths(root, self.device.type)

    def cancel(self):
        self._cancelled = True
        if self._process:
            _stop(self._process)

    def unload(self):
        if self._process:
            _stop(self._process)
        self._process = None
        self._binary = self._model = None

    def _run(self, inputs: Path, outputs: Path, progress, total):
        args = [str(self._binary), "transcribe", str(inputs), "--model", str(self._model),
                "--device", "cuda:0" if self.device.type == "cuda" else "cpu", "--format", "json",
                "--output-dir", str(outputs), "--concurrency", "1", "--quiet"]
        environment = os.environ.copy()
        environment["PATH"] = str(self._binary.parent) + os.pathsep + environment.get("PATH", "")
        environment["NEMO_SPEECH_MODEL_DIR"] = str(self._model.parent)
        outputs.mkdir()
        with tempfile.TemporaryFile() as errors:
            self._process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=errors, env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                last = 0.0
                while self._process.poll() is None:
                    if self._cancelled:
                        raise RuntimeError("Расшифровка Parakeet отменена")
                    if time.monotonic() - last > 1:
                        completed = len(list(outputs.glob("*.json")))
                        progress("Расшифровка Parakeet…", .1 + .8 * completed / max(total, 1),
                                 f"Фрагменты {completed}/{total} · {self.device.type}")
                        last = time.monotonic()
                    time.sleep(.15)
                if self._cancelled:
                    raise RuntimeError("Расшифровка Parakeet отменена")
                if self._process.returncode:
                    length = errors.tell()
                    errors.seek(max(0, length - 8192))
                    text = errors.read().decode("utf-8", "replace").lower()
                    if "out of memory" in text or "failed to allocate" in text:
                        raise RuntimeError("Недостаточно памяти для Parakeet. Освободите GPU; CPU автоматически не включается")
                    raise RuntimeError("Native Parakeet завершился с ошибкой. Проверьте runtime/драйвер; CPU автоматически не включается")
            finally:
                _stop(self._process)
                self._process = None

    def transcribe(self, audio_path: str | Path, *, language=None, use_vad=True, word_timestamps=True,
                   initial_prompt=None, beam_size=5, progress=_noop) -> TranscriptionResult:
        if initial_prompt:
            raise ValueError("Подсказка текста не поддерживается native Parakeet")
        self._cancelled = False
        self.load(progress)
        with tempfile.TemporaryDirectory(prefix="parakeet-worker-") as temporary:
            root = Path(temporary)
            chunks, duration = prepare_chunks(Path(audio_path), root / "inputs", lambda: self._cancelled)
            if not chunks:
                return TranscriptionResult([], "und", None, duration, MODEL_ID, self.device.type)
            self._run(root / "inputs", root / "outputs", progress, len(chunks))
            segments, languages = [], set()
            for index, chunk in enumerate(chunks):
                if self._cancelled:
                    raise RuntimeError("Расшифровка Parakeet отменена")
                output = root / "outputs" / (chunk["input"].stem + ".json")
                if not output.is_file() or output.stat().st_size > 16 * 1024**2:
                    raise RuntimeError("Parakeet не сохранил структурированный результат фрагмента")
                data = json.loads(output.read_text("utf-8"))
                segments.extend(parse_chunk(data, chunk, pauses=use_vad))
                languages.update(data.get("languages", []))
                progress("Сохранение таймкодов Parakeet…", .9 + .1 * (index + 1) / len(chunks), "")
            # Parakeet recognizes its supported languages automatically. This
            # pinned native implementation does not provide language-ID scores.
            detected = next(iter(languages)) if len(languages) == 1 else "mixed" if languages else "und"
            return TranscriptionResult(segments, detected, None, duration, MODEL_ID, self.device.type)
