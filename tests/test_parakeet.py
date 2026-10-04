from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import wave
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from meeting_archive.worker import install_parakeet as installer
from meeting_archive.worker.core import exporters, parakeet, transcriber
from meeting_archive.worker.core.batch import combine_transcription_results
from meeting_archive.worker.core.utils import DeviceInfo


def wav(path, seconds, rate=16000):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * int(seconds * rate))
    return path


@pytest.fixture
def native_resources(tmp_path, monkeypatch):
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("native/bin/nemo-speech.exe", b"synthetic native executable")
        archive.writestr("native/bin/ggml-cuda.dll", b"synthetic CUDA library")
        archive.writestr("native/LICENSE", b"Apache-2.0 synthetic license fixture")
    archive_data = bundle.getvalue()
    model_data = b"GGUF synthetic public model"
    monkeypatch.setattr(installer, "MODEL_BYTES", len(model_data))
    monkeypatch.setattr(installer, "MODEL_SHA256", hashlib.sha256(model_data).hexdigest())
    monkeypatch.setattr(installer, "RUNTIMES", {profile: {"size": len(archive_data),
        "sha256": hashlib.sha256(archive_data).hexdigest()} for profile in ("cuda", "cpu")})
    calls = []

    def download(url, path, size, digest, progress, cancelled):
        installer._cancel(cancelled)
        calls.append(url)
        path.write_bytes(model_data if url == installer.MODEL_URL else archive_data)
        assert path.stat().st_size == size and installer._hash(path) == digest
        progress(1, "synthetic fixture download complete")

    monkeypatch.setattr(installer, "_download", download)
    return tmp_path / "owned-module", calls


def test_native_install_is_owned_pinned_atomic_and_reuses_one_model_between_profiles(native_resources):
    root, calls = native_resources
    ready = installer.install_parakeet(root, "cuda", lambda *_: None, lambda: False)
    assert ready["model"] == installer.MODEL_ID and ready["model_revision"] == installer.MODEL_REVISION
    assert ready["runtime_version"] == "0.1.0" and not ready["gpu_verified"]
    assert ready["pause_detection"] == "alignment-gaps" and not ready["vad_preprocessing"]
    assert Path(ready["runtime_path"]).is_relative_to(root / "engines/parakeet/runtime-cuda")
    assert Path(ready["model_path"]).is_relative_to(root / "engines/parakeet/models")
    assert (root / "engines/parakeet/ready-cuda.json").is_file()
    installer.install_parakeet(root, "cuda", lambda *_: None, lambda: False)
    assert len(calls) == 2
    cpu = installer.install_parakeet(root, "cpu", lambda *_: None, lambda: False)
    assert cpu["model_path"] == ready["model_path"]
    assert calls.count(installer.MODEL_URL) == 1
    assert not list((root / "engines/parakeet").glob(".install-*"))


def test_native_cancel_never_publishes_ready_and_preserves_other_engines(native_resources):
    root, _ = native_resources
    (root / "engines/whisper").mkdir(parents=True)
    (root / ".meeting-archive-owned").write_text("owned", encoding="utf-8")
    sentinel = root / "engines/whisper/preserve.bin"
    sentinel.write_bytes(b"synthetic other engine")
    cancellation = False

    def progress(value, _message):
        nonlocal cancellation
        cancellation = value > .1

    with pytest.raises(installer.ParakeetInstallCancelled):
        installer.install_parakeet(root, "cuda", progress, lambda: cancellation)
    assert not (root / "engines/parakeet/ready-cuda.json").exists()
    assert sentinel.read_bytes() == b"synthetic other engine"
    assert not list((root / "engines/parakeet").glob(".install-*"))


def test_native_install_refuses_unowned_files(native_resources):
    root, calls = native_resources
    root.mkdir()
    (root / "private.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="не принадлежит"):
        installer.install_parakeet(root, "cuda", lambda *_: None, lambda: False)
    assert not calls and (root / "private.txt").read_text("utf-8") == "preserve"


@pytest.mark.parametrize("entry", ["../outside.exe", "/absolute.exe", "C:/outside.exe", "..\\outside.exe"])
def test_native_zip_slip_never_writes_outside_owned_stage(tmp_path, entry):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(entry, b"synthetic untrusted archive")
    with pytest.raises(ValueError, match="небезопасный"):
        installer._extract(path, tmp_path / "stage", lambda: False)
    assert not (tmp_path / "outside.exe").exists()


def test_download_checks_checksum_and_cleans_partial_on_bad_network_content(tmp_path, monkeypatch):
    client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"wrong bytes"))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=transport, **kwargs))
    destination = tmp_path / "model.gguf"
    with pytest.raises(ValueError, match="SHA-256"):
        installer._download("https://huggingface.co/public/model", destination, len(b"wrong bytes"),
                            hashlib.sha256(b"good bytes!").hexdigest(), lambda *_: None, lambda: False)
    assert not destination.exists() and not destination.with_suffix(".gguf.part").exists()


def test_download_refuses_http_downgrade(tmp_path, monkeypatch):
    def response(request):
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"Location": "http://download.invalid/public.gguf"})
        return httpx.Response(200, content=b"GGUF")

    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(response), **kwargs))
    with pytest.raises(ValueError, match="небезопасный"):
        installer._download("https://huggingface.co/public/model", tmp_path / "model.gguf", 4,
                            hashlib.sha256(b"GGUF").hexdigest(), lambda *_: None, lambda: False)


def test_chunks_bound_memory_include_context_and_preserve_absolute_timeline(tmp_path):
    path = wav(tmp_path / "synthetic.wav", 62)
    chunks, duration = parakeet.prepare_chunks(path, tmp_path / "chunks")
    assert duration == 62 and len(chunks) == 3
    assert chunks[1]["start"] == 30 and chunks[1]["offset"] == 29.5
    assert chunks[2]["end"] == 62 and chunks[2]["final"]
    for chunk in chunks:
        with wave.open(str(chunk["input"]), "rb") as audio:
            assert audio.getnframes() / audio.getframerate() <= 31


def test_native_word_offsets_remove_overlap_duplicates_and_keep_pauses():
    chunk = {"offset": 29.5, "start": 30, "end": 60, "final": False}
    data = {"text": "synthetic words", "words": [
        {"word": "Контекст", "start": .1, "end": .3},
        {"word": "Первая", "start": .6, "end": .9},
        {"word": "фраза.", "start": 1, "end": 1.4},
        {"word": "Позже", "start": 3, "end": 3.5},
        {"word": "Следующий", "start": 30.5, "end": 30.8}]}
    segments = parakeet.parse_chunk(data, chunk)
    assert len(segments) == 2
    assert segments[0].text == "Первая фраза." and segments[0].start == 30.1
    assert segments[1].start == 32.5
    assert all(w["word"] not in {"Контекст", "Следующий"} for s in segments for w in s.words)


def test_gpu_probe_failure_never_starts_native_cpu_fallback(monkeypatch):
    monkeypatch.setattr(parakeet, "probe_parakeet", lambda *_: {"compatible": False,
                        "reason": "Native CUDA недоступна; CPU автоматически не включается"})
    monkeypatch.setattr(parakeet.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("CPU fallback started"))
    engine = parakeet.ParakeetTranscriber(installer.MODEL_ID, DeviceInfo("cuda", "GPU", 12, "float16"))
    with pytest.raises(RuntimeError, match="CPU автоматически не включается"):
        engine.load()


def test_native_command_uses_explicit_gpu_local_model_and_bounded_concurrency(tmp_path, monkeypatch):
    engine = parakeet.ParakeetTranscriber(installer.MODEL_ID, DeviceInfo("cuda", "GPU", 12, "float16"))
    engine._binary, engine._model = tmp_path / "nemo-speech.exe", tmp_path / "model.gguf"
    seen = []

    class Process:
        returncode = 0

        def __init__(self, args, **kwargs):
            seen.append(args)

        def poll(self):
            return self.returncode

    monkeypatch.setattr(parakeet.subprocess, "Popen", Process)
    engine._run(tmp_path / "inputs", tmp_path / "outputs", lambda *_: None, 2)
    args = seen[0]
    assert args[args.index("--device") + 1] == "cuda:0"
    assert args[args.index("--model") + 1] == str(engine._model)
    assert args[args.index("--concurrency") + 1] == "1"
    assert "auto" not in args and "--stream" not in args and len(seen) == 1


def test_doctor_reports_cpu_incompatibility_for_cuda_without_exposing_paths(native_resources, monkeypatch):
    root, _ = native_resources
    installer.install_parakeet(root, "cuda", lambda *_: None, lambda: False)

    def run(args, **_kwargs):
        assert args[1:] == ["doctor", "--json"]
        return subprocess.CompletedProcess(args, 1, json.dumps({"version": "0.1.0",
            "features": {"backend_cuda": True}, "devices": [{"name": "CPU", "type": "cpu",
            "description": "synthetic CPU"}], "model_download": {"executable": "private system path"}}), "")

    monkeypatch.setattr(installer.subprocess, "run", run)
    report = installer.probe_parakeet(root, "cuda")
    assert not report["cuda"] and not report["compatible"]
    assert "CPU отключён" in report["reason"] and "private system path" not in json.dumps(report)


def test_unknown_language_probability_stays_null_through_batch_and_exports(tmp_path):
    result = transcriber.TranscriptionResult([transcriber.Segment(0, 1, "Синтетический текст")],
                "und", None, 1, installer.MODEL_ID, "cuda")
    combined = combine_transcription_results([(Path("synthetic.wav"), result), (Path("second.wav"), result)])
    assert combined.language_probability is None
    exporters.export_json(combined, tmp_path / "result.json")
    exporters.export_md_ai(combined, tmp_path / "result.md", source_file="synthetic.wav")
    assert json.loads((tmp_path / "result.json").read_text("utf-8"))["metadata"]["language_probability"] is None
    assert "вероятность не предоставлена движком" in (tmp_path / "result.md").read_text("utf-8")


@pytest.mark.parametrize("model,repo", [("large-v3-turbo", "dropbox-dash/faster-whisper-large-v3-turbo"),
                                      ("tiny.en", "Systran/faster-whisper-tiny.en")])
@pytest.mark.parametrize("vocabulary", ["vocabulary.json", "vocabulary.txt"])
def test_new_whisper_models_use_local_directory_without_implicit_download(tmp_path, monkeypatch, model, repo, vocabulary):
    cache = tmp_path / "hub"
    directory = cache / ("models--" + repo.replace("/", "--"))
    snapshot = directory / "snapshots/synthetic-commit"
    snapshot.mkdir(parents=True)
    (directory / "refs").mkdir()
    (directory / "refs/main").write_text("synthetic-commit", encoding="utf-8")
    for name in ("model.bin", "config.json", "tokenizer.json", vocabulary):
        (snapshot / name).write_bytes(b"synthetic public model")
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    seen = []

    def whisper(path, **kwargs):
        seen.append((path, kwargs))
        return object()

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=whisper))
    engine = transcriber.Transcriber(model, DeviceInfo("cpu", "CPU", 0, "int8"))
    engine.load()
    assert seen == [(str(snapshot), {"device": "cpu", "compute_type": "int8", "local_files_only": True})]


def test_whisper_without_selected_model_fails_locally(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "no-models"))
    with pytest.raises(RuntimeError, match="не установлена"):
        transcriber.installed_model_path("large-v3")
