"""Offline GigaAM-v3 inference using a fixed official model and bounded chunks."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path

from .transcriber import Segment, TranscriptionResult, _noop
from .utils import DeviceInfo

MODEL_NAME = "gigaam-v3-e2e-rnnt"
MODEL_ALIASES = {MODEL_NAME, "gigaam-v3"}
SOURCE_COMMIT = "7447938d791c4f3e643386ee22c33777004293a5"
SOURCE_SHA256 = "17c9a57a8c76659feb112b4a6299391757d137fc50e5375d5613c46c379f3653"
SOURCE_FILES_SHA256 = {
    "__init__.py": "9d772461e35b9b3caf2f94fd5343261f732395c409f7a7957fa2bf03704b5a5c",
    "decoder.py": "fe6b82f8ea48e5a4628414601a160cb62398216ed3d9a9703bb06c0d721795a4",
    "decoding.py": "0f77d20748cf27a4fb5f60bd8d0b30a219fd40faf19144788b09d9b76c32bd84",
    "encoder.py": "1eac8600faf1b842899c0944b8ebb9d60b41dd6f35d826c41514765dbf18cd3d",
    "model.py": "38b79ada59e2a8764f3bf64fe10cd44198ab95a2168915f4c521416ea6331236",
    "onnx_utils.py": "5830d6d5bfc34675b9a97a9ee90ed15c63d4996c43bf352167f78c1ea74acd51",
    "preprocess.py": "d4cd47b7c07664c0aab682148155a1fb2eab4829a91730cb14b1c75dddfcc22f",
    "timestamps_utils.py": "9c2f590107508c36c11ae98d45c1a4f757decd7e19c0329509f29a7e6c527c70",
    "types.py": "ea6e9f83993f927fce7f73fd7d97fb78d0735ef6429954f7d1502b7a051af765",
    "utils.py": "a6366d63c304b162202bf97a3c2da05a453741c475ab3bf160fc0428957282f9",
    "vad_utils.py": "b5fdb0589eceda795161bee9721b841d41354608057e4a2718fe2debb513cbf5",
}
MODEL_REVISION = "7655ad717f8122257385bb4b2f373db3697e8680"
MODEL_FILES = {
    "config.json": (1867, "02361ba9cafd6c3ec66fcdd73494c3b562a60eb2a2d1b13f3cb04ae440d93e52"),
    "pytorch_model.bin": (448928167, "afc6dcbae8320ea56f2cddebc0f13fbf62c9d59b6ddcad899782623c8610826a"),
    "tokenizer.model": (255336, "828c12c991019eef952a960661f25a92d6ad279591e2ea466b4aeddf1d20a18a"),
}
SAMPLE_RATE = 16000
MAX_CHUNK_SECONDS = 25
VAD_WINDOW_SECONDS = 60
TARGETS = {
    "modeling_gigaam.FeatureExtractor": "gigaam.preprocess.FeatureExtractor",
    "modeling_gigaam.ConformerEncoder": "gigaam.encoder.ConformerEncoder",
    "modeling_gigaam.RNNTHead": "gigaam.decoder.RNNTHead",
    "modeling_gigaam.RNNTGreedyDecoding": "gigaam.decoding.RNNTGreedyDecoding",
}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_models(directory: Path):
    for name, (size, digest) in MODEL_FILES.items():
        path = directory / name
        if (not path.is_file() or path.is_symlink() or path.stat().st_size != size or sha256(path) != digest):
            raise RuntimeError("Файлы GigaAM отсутствуют или изменены. Повторите установку этой модели")


def verify_sources(directory: Path):
    for name, digest in SOURCE_FILES_SHA256.items():
        path = directory / name
        if not path.is_file() or path.is_symlink() or sha256(path) != digest:
            raise RuntimeError("Исходники GigaAM отсутствуют или изменены. Повторите установку этой модели")


def home() -> Path:
    value = os.environ.get("MEETING_ARCHIVE_GIGAAM_HOME", "")
    path = Path(value)
    if not value or not path.is_absolute() or not (path / "ready.json").is_file():
        raise RuntimeError("GigaAM не установлен. Выберите GigaAM при установке локального модуля")
    return path.resolve()


def model_config(raw: dict, tokenizer: Path) -> dict:
    """Map only known classes; never execute Hugging Face modeling_gigaam.py."""
    cfg = copy.deepcopy(raw["cfg"]["model"]["cfg"])
    if cfg.get("model_name") != "v3_e2e_rnnt" or cfg.get("sample_rate") != SAMPLE_RATE:
        raise ValueError("Неизвестная конфигурация GigaAM")

    def convert(item):
        if isinstance(item, dict):
            for key, value in list(item.items()):
                if key == "_target_":
                    if value not in TARGETS:
                        raise ValueError("Конфигурация GigaAM содержит неизвестный класс")
                    item[key] = TARGETS[value]
                else:
                    convert(value)
        elif isinstance(item, list):
            for value in item:
                convert(value)

    convert(cfg)
    cfg["encoder"]["flash_attn"] = False
    cfg["decoding"]["model_path"] = str(tokenizer)
    return cfg


def unpack_state(state: dict) -> dict:
    if not isinstance(state, dict) or not state or any(not isinstance(key, str) or not key.startswith("model.") for key in state):
        raise ValueError("Неизвестный формат весов GigaAM")
    return {key.removeprefix("model."): value for key, value in state.items()}


def split_spans(spans, frames: int, maximum: int = MAX_CHUNK_SECONDS * SAMPLE_RATE):
    """Clip detector boundaries to the source, prevent overlap, cap inference size."""
    if maximum <= 0:
        raise ValueError("Размер фрагмента должен быть положительным")
    last_end = 0
    for item in spans:
        start, end = int(item["start"]), int(item["end"])
        start, end = max(last_end, 0, start), min(frames, end)
        if end <= start:
            continue
        while start < end:
            stop = min(start + maximum, end)
            yield start, stop
            start = stop
        last_end = end


class Transcriber:
    def __init__(self, model_name: str, device: DeviceInfo):
        if model_name not in MODEL_ALIASES:
            raise ValueError("Неизвестная модель GigaAM")
        if device.type not in {"cuda", "cpu"}:
            raise ValueError("GigaAM поддерживает явно выбранные CPU или CUDA")
        self.model_name, self.device = MODEL_NAME, device
        self._model = None
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def unload(self):
        self._model = None
        import gc
        gc.collect()
        if self.device.type == "cuda":
            import torch
            torch.cuda.empty_cache()

    def load(self, progress=_noop):
        if self._model is not None:
            return
        directory = home()
        ready = json.loads((directory / "ready.json").read_text("utf-8"))
        if ready.get("source_commit") != SOURCE_COMMIT or ready.get("model_revision") != MODEL_REVISION:
            raise RuntimeError("Версия GigaAM не соответствует сборке. Повторите установку модели")
        progress("Проверка GigaAM…", 0, "Файлы и SHA-256")
        verify_models(directory / "model")
        verify_sources(directory / "packages/gigaam")
        for name in ("addons", "packages"):
            path = directory / name
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        import torch
        from gigaam import GigaAMASR
        from omegaconf import OmegaConf
        if self.device.type == "cuda":
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA недоступна для GigaAM. Автоматический переход на CPU отключён")
            major, minor = torch.cuda.get_device_capability()
            if f"sm_{major}{minor}" not in torch.cuda.get_arch_list():
                raise RuntimeError("PyTorch не поддерживает архитектуру GPU. Автоматический переход на CPU отключён")
        raw = json.loads((directory / "model/config.json").read_text("utf-8"))
        cfg = OmegaConf.create(model_config(raw, directory / "model/tokenizer.model"))
        model = GigaAMASR(cfg)
        state = torch.load(directory / "model/pytorch_model.bin", map_location="cpu", weights_only=True)
        model.load_state_dict(unpack_state(state), strict=True)
        model.eval()
        if self.device.type == "cuda":
            model.encoder = model.encoder.half()
        self._model = model.to(self.device.type)
        progress("GigaAM загружен", 1, "Русская речь; CPU float32 / GPU float16 encoder")

    def transcribe(self, audio_path: str | Path, *, language=None, use_vad=True, word_timestamps=True,
                   initial_prompt=None, beam_size=5, progress=_noop):
        if language not in {None, "", "auto", "ru"}:
            raise ValueError("GigaAM-v3 специализируется на русской речи. Для другого языка выберите Whisper")
        self._cancel = False
        self.load(progress=lambda message, pct, hint: progress(message, pct * .05, hint))
        import soundfile as sf
        import torch
        detector = detect_speech = None
        if use_vad:
            from silero_vad import get_speech_timestamps, load_silero_vad
            detector, detect_speech = load_silero_vad(onnx=False), get_speech_timestamps
        collected = []
        with sf.SoundFile(audio_path) as source:
            if source.samplerate != SAMPLE_RATE or source.channels != 1:
                raise ValueError("Для GigaAM подготовьте WAV 16 кГц, моно")
            total_frames = len(source)
            duration = total_frames / SAMPLE_RATE
            if not total_frames:
                raise ValueError("Аудиофайл пуст")
            offset = 0
            window = VAD_WINDOW_SECONDS * SAMPLE_RATE if use_vad else MAX_CHUNK_SECONDS * SAMPLE_RATE
            while offset < total_frames:
                if self._cancel:
                    raise RuntimeError("Транскрипция отменена пользователем")
                samples = source.read(frames=min(window, total_frames - offset), dtype="float32")
                count = len(samples)
                if not count:
                    raise RuntimeError("Аудиофайл неожиданно закончился")
                waveform = torch.from_numpy(samples)
                spans = detect_speech(waveform, detector, sampling_rate=SAMPLE_RATE, min_silence_duration_ms=500,
                    speech_pad_ms=200, max_speech_duration_s=24.5) if use_vad else [{"start": 0, "end": count}]
                for start, end in split_spans(spans, count):
                    if self._cancel:
                        raise RuntimeError("Транскрипция отменена пользователем")
                    chunk = waveform[start:end].to(self.device.type).unsqueeze(0)
                    lengths = torch.tensor([end - start], device=self.device.type)
                    with torch.inference_mode():
                        encoded, encoded_length = self._model.forward(chunk, lengths)
                        decoded = self._model._decode(encoded, encoded_length, lengths, word_timestamps=False)
                    text = decoded[0][0].strip()
                    if text:
                        collected.append(Segment(start=(offset + start) / SAMPLE_RATE,
                            end=(offset + end) / SAMPLE_RATE, text=text, words=[]))
                    progress("GigaAM: распознавание…", .05 + .9 * (offset + end) / total_frames,
                             "Таймкоды границ фрагмента; выравнивание отдельных слов не выполняется")
                offset += count
                progress("GigaAM: обработка записи…", .05 + .9 * offset / total_frames, "")
        return TranscriptionResult(segments=collected, language="ru", language_probability=None,
            duration=duration, model_name=self.model_name, device=self.device.type)
