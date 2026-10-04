from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_archive.worker import install_gigaam as installer
from meeting_archive.worker.core import gigaam
from meeting_archive.worker.core.utils import DeviceInfo


def configuration(**overrides):
    return {"cfg": {"model": {"cfg": {"model_name": "v3_e2e_rnnt", "sample_rate": 16000,
        "preprocessor": {"_target_": "modeling_gigaam.FeatureExtractor"},
        "encoder": {"_target_": "modeling_gigaam.ConformerEncoder", "flash_attn": True},
        "head": {"_target_": "modeling_gigaam.RNNTHead"},
        "decoding": {"_target_": "modeling_gigaam.RNNTGreedyDecoding", "model_path": "tokenizer.model"},
        **overrides}}}}


def test_only_known_configuration_targets_are_mapped_without_remote_code(tmp_path):
    raw = configuration()
    converted = gigaam.model_config(raw, tmp_path / "tokenizer.model")
    assert converted["preprocessor"]["_target_"] == "gigaam.preprocess.FeatureExtractor"
    assert converted["decoding"]["model_path"] == str(tmp_path / "tokenizer.model")
    assert converted["encoder"]["flash_attn"] is False
    assert raw["cfg"]["model"]["cfg"]["encoder"]["flash_attn"] is True
    with pytest.raises(ValueError, match="неизвестный класс"):
        gigaam.model_config(configuration(head={"_target_": "os.system"}), tmp_path / "tokenizer.model")


@pytest.mark.parametrize("raw", [configuration(model_name="v3_ssl"), configuration(sample_rate=8000)])
def test_model_config_rejects_unrecognised_family(raw, tmp_path):
    with pytest.raises(ValueError):
        gigaam.model_config(raw, tmp_path / "tokenizer.model")


@pytest.mark.parametrize("state", [{}, [], {"encoder.weight": 1}, {"model.encoder.weight": 1, "unknown": 2}])
def test_checkpoint_requires_the_official_hf_wrapper_prefix(state):
    with pytest.raises(ValueError):
        gigaam.unpack_state(state)


def test_checkpoint_strips_the_known_prefix_without_guessing():
    assert gigaam.unpack_state({"model.encoder.weight": 1}) == {"encoder.weight": 1}


def test_chunk_bounds_are_clipped_and_never_overlap_or_exceed_25_seconds():
    second = gigaam.SAMPLE_RATE
    spans = [{"start": -1, "end": 70 * second}, {"start": 69 * second, "end": 120 * second}]
    chunks = list(gigaam.split_spans(spans, 100 * second))
    assert chunks == [(0, 25 * second), (25 * second, 50 * second), (50 * second, 70 * second),
                      (70 * second, 95 * second), (95 * second, 100 * second)]
    assert all(end - start <= 25 * second for start, end in chunks)


class FakeTensor:
    def __init__(self, frames):
        self.frames = frames

    def __len__(self):
        return self.frames

    def __getitem__(self, selection):
        return FakeTensor(selection.stop - selection.start)

    def to(self, device):
        self.device = device
        return self

    def unsqueeze(self, _):
        return self


def fake_inference(monkeypatch, *, duration=61, rate=16000, channels=1, spans=None):
    calls, cursor = [], {"frames": 0}

    class SoundFile:
        samplerate = rate

        def __init__(self, _):
            self.channels = channels

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def __len__(self):
            return int(duration * rate)

        def read(self, frames, dtype):
            assert dtype == "float32"
            cursor["frames"] += frames
            return FakeTensor(frames)

    class Model:
        def forward(self, chunk, lengths):
            calls.append((chunk.frames, chunk.device))
            assert lengths == [chunk.frames]
            return None, None

        def _decode(self, encoded, encoded_length, lengths, word_timestamps):
            assert word_timestamps is False
            return [("Синтетический текст", None)]

    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(SoundFile=SoundFile))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(from_numpy=lambda x: x,
        tensor=lambda x, device: x, inference_mode=contextlib.nullcontext))
    monkeypatch.setattr(gigaam.Transcriber, "load", lambda self, **_: setattr(self, "_model", Model()))

    def speech(waveform, model, **kwargs):
        assert kwargs["max_speech_duration_s"] == 24.5
        return spans(waveform.frames) if callable(spans) else spans

    monkeypatch.setitem(sys.modules, "silero_vad", SimpleNamespace(load_silero_vad=lambda onnx: object(),
        get_speech_timestamps=speech))
    return calls, cursor


def test_inference_streams_bounded_chunks_preserves_real_duration_and_coarse_times(monkeypatch):
    calls, cursor = fake_inference(monkeypatch, duration=61)
    asr = gigaam.Transcriber(gigaam.MODEL_NAME, DeviceInfo("cpu", "CPU", 0, "float32"))
    result = asr.transcribe(Path("synthetic.wav"), language="auto", use_vad=False)
    assert calls == [(400000, "cpu"), (400000, "cpu"), (176000, "cpu")]
    assert cursor["frames"] == 61 * 16000
    assert [(s.start, s.end) for s in result.segments] == [(0, 25), (25, 50), (50, 61)]
    assert all(s.words == [] for s in result.segments)
    assert result.language == "ru" and result.language_probability is None
    assert result.duration == 61
    assert result.model_name == gigaam.MODEL_NAME


def test_vad_silence_produces_no_hallucinated_segments(monkeypatch):
    calls, _ = fake_inference(monkeypatch, duration=121, spans=[])
    result = gigaam.Transcriber("gigaam-v3", DeviceInfo("cuda", "GPU", 12, "float16")).transcribe("synthetic.wav")
    assert calls == []
    assert result.segments == []
    assert result.duration == 121


def test_vad_offsets_each_window_to_original_audio(monkeypatch):
    calls, _ = fake_inference(monkeypatch, duration=61, spans=lambda frames: [{"start": 0, "end": frames}])
    result = gigaam.Transcriber(gigaam.MODEL_NAME, DeviceInfo("cuda", "GPU", 12, "float16")).transcribe("synthetic.wav")
    assert [(s.start, s.end) for s in result.segments] == [(0, 25), (25, 50), (50, 60), (60, 61)]
    assert all(frames <= 400000 and device == "cuda" for frames, device in calls)


def test_cancellation_stops_before_the_next_chunk(monkeypatch):
    calls, _ = fake_inference(monkeypatch)
    asr = gigaam.Transcriber(gigaam.MODEL_NAME, DeviceInfo("cpu", "CPU", 0, "float32"))
    with pytest.raises(RuntimeError, match="отменена"):
        asr.transcribe("synthetic.wav", use_vad=False, progress=lambda *_: asr.cancel())
    assert len(calls) == 1


@pytest.mark.parametrize("rate,channels", [(8000, 1), (16000, 2)])
def test_inference_requires_prepared_mono_16k_audio(monkeypatch, rate, channels):
    fake_inference(monkeypatch, rate=rate, channels=channels)
    with pytest.raises(ValueError, match="16 кГц, моно"):
        gigaam.Transcriber(gigaam.MODEL_NAME, DeviceInfo("cpu", "CPU", 0, "float32")).transcribe("synthetic.wav")


def test_explicit_english_is_rejected_before_loading_or_downloading(monkeypatch):
    monkeypatch.setattr(gigaam.Transcriber, "load", lambda *_a, **_k: pytest.fail("Unexpected model load"))
    with pytest.raises(ValueError, match="русской речи"):
        gigaam.Transcriber(gigaam.MODEL_NAME, DeviceInfo("cpu", "CPU", 0, "float32")).transcribe("synthetic.wav", language="en")


def test_model_files_require_pinned_sizes_and_sha256(monkeypatch, tmp_path):
    content = b"synthetic pinned checkpoint"
    monkeypatch.setattr(gigaam, "MODEL_FILES", {"model.bin": (len(content), hashlib.sha256(content).hexdigest())})
    (tmp_path / "model.bin").write_bytes(content)
    gigaam.verify_models(tmp_path)
    (tmp_path / "model.bin").write_bytes(b"other model of same size!!")
    with pytest.raises(RuntimeError, match="изменены"):
        gigaam.verify_models(tmp_path)


def test_source_files_are_verified_before_importing_pinned_package(monkeypatch, tmp_path):
    content = b"synthetic pinned code"
    monkeypatch.setattr(gigaam, "SOURCE_FILES_SHA256", {"model.py": hashlib.sha256(content).hexdigest()})
    (tmp_path / "model.py").write_bytes(content)
    gigaam.verify_sources(tmp_path)
    (tmp_path / "model.py").write_bytes(b"modified code")
    with pytest.raises(RuntimeError, match="Исходники"):
        gigaam.verify_sources(tmp_path)


def test_gpu_failure_stops_without_model_creation_or_cpu_fallback(monkeypatch, tmp_path):
    (tmp_path / "ready.json").write_text(json.dumps({"source_commit": gigaam.SOURCE_COMMIT,
        "model_revision": gigaam.MODEL_REVISION}), "utf-8")
    monkeypatch.setenv("MEETING_ARCHIVE_GIGAAM_HOME", str(tmp_path))
    monkeypatch.setattr(gigaam, "verify_models", lambda _: None)
    monkeypatch.setattr(gigaam, "verify_sources", lambda _: None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "gigaam", SimpleNamespace(GigaAMASR=lambda _: pytest.fail("Model must not be created")))
    monkeypatch.setitem(sys.modules, "omegaconf", SimpleNamespace(OmegaConf=SimpleNamespace(create=lambda x: x)))
    with pytest.raises(RuntimeError, match="переход на CPU отключён"):
        gigaam.Transcriber(gigaam.MODEL_NAME, DeviceInfo("cuda", "GPU", 12, "float16")).load()


def test_explicit_installer_does_not_touch_shared_runtime_or_external_app(monkeypatch, tmp_path):
    downloads = []

    def downloaded(url, target, size, digest, progress):
        downloads.append((url, target))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic artifact")
        progress(1, "Проверено")

    monkeypatch.setattr(installer, "download", downloaded)
    monkeypatch.setattr(installer, "extract_source", lambda *_: {"model.py": "synthetic-checksum"})
    root = tmp_path / "owned/engines/gigaam"
    ready = installer.install(root, progress=lambda *_: None)
    assert len(downloads) == 4
    assert all(target.is_relative_to(root) for _, target in downloads)
    assert ready["source_commit"] == gigaam.SOURCE_COMMIT
    assert json.loads((root / "ready.json").read_text("utf-8"))["files"] == ready["files"]
    assert ready["timestamps"] == "chunk-boundaries"
    assert not (root / "source.zip").exists()


@pytest.mark.parametrize("content", [b"correct", b"wrong checksum", b"too long artifact"])
def test_download_is_atomic_and_removes_failed_partial_files(monkeypatch, tmp_path, content):
    target = tmp_path / "model.bin"
    target.write_bytes(b"original file")

    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(installer.urllib.request, "build_opener", lambda *_: SimpleNamespace(
        open=lambda *_a, **_k: Response(content)))
    if content == b"correct":
        installer.download("https://huggingface.co/fixed-model", target, 7, hashlib.sha256(b"correct").hexdigest(), lambda *_: None)
        assert target.read_bytes() == content
    else:
        with pytest.raises(ValueError):
            installer.download("https://huggingface.co/fixed-model", target, 7, hashlib.sha256(b"correct").hexdigest(), lambda *_: None)
        assert target.read_bytes() == b"original file"
    assert not (tmp_path / "model.bin.part").exists()


def test_source_extraction_rejects_traversal_even_from_a_synthetic_verified_zip(monkeypatch, tmp_path):
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("GigaAM-" + gigaam.SOURCE_COMMIT + "/gigaam/../../escape.py", "unsafe")
    monkeypatch.setattr(installer, "SOURCE_SIZE", archive.stat().st_size)
    monkeypatch.setattr(installer, "SOURCE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match="содержимое"):
        installer.extract_source(archive, tmp_path / "owned")
    assert not (tmp_path / "escape.py").exists()


def test_https_download_redirect_cannot_downgrade_transport():
    with pytest.raises(ValueError, match="небезопасный адрес"):
        installer.HTTPSRedirect().redirect_request(None, None, 302, "", {}, "http://unsafe.test/")
