from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_archive.worker import entry
from meeting_archive.worker.core import exporters
from meeting_archive.worker.core.batch import combine_transcription_results
from meeting_archive.worker.core.transcriber import Segment, TranscriptionResult
from meeting_archive.worker.core.utils import DeviceInfo, format_timestamp_srt, format_timestamp_vtt


def result(segments, duration=5):
    return TranscriptionResult(segments, "ru", .99, duration, "tiny", "synthetic device")


def test_subtitle_rounding_carries_seconds_and_minutes():
    assert format_timestamp_srt(1.9999) == "00:00:02,000"
    assert format_timestamp_vtt(59.9999) == "00:01:00.000"
    assert format_timestamp_srt(3599.9999) == "01:00:00,000"


@pytest.mark.parametrize("mode", ["separate", "merged_wav"])
def test_worker_exports_relative_sources_and_run_metadata_without_real_inference(monkeypatch, tmp_path, mode):
    # Python 3.12.15 sets a private ACL for mkdtemp on Windows. Use this
    # project's inherited test ACL rather than the user's temporary folder.
    import uuid
    def workspace_temp(suffix=None, prefix=None, dir=None):
        folder = tmp_path / ((prefix or "") + uuid.uuid4().hex + (suffix or ""))
        folder.mkdir()
        return str(folder)
    monkeypatch.setattr(entry.tempfile, "mkdtemp", workspace_temp)
    from meeting_archive.worker.core.utils import DeviceInfo
    class ASR:
        def __init__(self, *args):
            pass
        def transcribe(self, *args, **kwargs):
            return result([Segment(0, 2, "Synthetic speech")])
        def unload(self):
            pass
    monkeypatch.setattr(entry, "core", lambda: {
        "utils": SimpleNamespace(DeviceInfo=DeviceInfo,detect_device=lambda:DeviceInfo("cpu","CPU",0,"int8")),
        "transcriber":SimpleNamespace(Transcriber=ASR),
        "audio_processor":SimpleNamespace(prepare_audio=lambda source,**kwargs:source,concat_wav_files=lambda *args:None),
        "batch":SimpleNamespace(combine_transcription_results=combine_transcription_results),"exporters":exporters})
    monkeypatch.setattr(entry,"emit",lambda *args,**kwargs:None)
    sources = [tmp_path / name for name in ("one.wav","two.wav")]
    for source in sources:
        source.write_bytes(b"synthetic audio")
    refs = ["audio/" + source.name for source in sources]
    output = tmp_path / "output"
    entry.run({"files":[str(p) for p in sources],"source_files":refs,"output":str(output),"mode":mode,
               "settings":{"device":"cpu","cpu_confirmed":True,"model":"tiny"}})
    run = json.loads((output / "run.json").read_text("utf-8"))
    exported = json.loads((output / "transcript.json").read_text("utf-8"))
    assert run["source_files"] == exported["metadata"]["source_files"] == refs
    assert run["source_path_base"] == "meeting" and run["mode"] == mode
    assert str(tmp_path) not in (output / "transcript.json").read_text("utf-8")


def test_batch_offsets_segments_and_words_preserving_input_results():
    a = result([Segment(0, 1, "Первая часть", words=[{"start": 0, "end": 1, "word": "Первая"}])], duration=5)
    b = result([Segment(1, 2, "Вторая часть", words=[{"start": 1, "end": 2, "word": "Вторая"}])], duration=3)
    combined = combine_transcription_results([(Path("first.wav"), a), (Path("second.wav"), b)])
    assert combined.duration == 8
    assert combined.segments[1].start == 6
    assert combined.segments[1].end == 7
    assert combined.segments[1].words[0]["start"] == 6
    assert combined.segments[1].source_file == "second.wav"
    assert b.segments[0].start == 1
    assert b.segments[0].words[0]["start"] == 1


def test_all_text_exports_cover_undiarized_start_timestamp_and_sources(tmp_path):
    transcription = result([Segment(1.25, 4.5, "Синтетический текст", source_file="voice.wav")])
    paths = {ext: tmp_path / ("transcript." + ext) for ext in ("json", "txt", "md", "srt", "vtt")}
    exporters.export_json(transcription, paths["json"])
    exporters.export_txt(transcription, paths["txt"], with_timestamps=True)
    exporters.export_md_ai(transcription, paths["md"], source_file="voice.wav")
    exporters.export_srt(transcription, paths["srt"])
    exporters.export_vtt(transcription, paths["vtt"])
    for path in paths.values():
        assert "Синтетический текст" in path.read_text("utf-8")
    assert "## [00:01" in paths["md"].read_text("utf-8")
    assert "--:--:--" not in paths["md"].read_text("utf-8")
    assert "00:00:01,250 --> 00:00:04,500" in paths["srt"].read_text("utf-8")
    assert paths["vtt"].read_text("utf-8").startswith("WEBVTT")


def fake_core(monkeypatch, detected):
    attempts = []

    def transcriber(model, device):
        attempts.append(device)
        raise RuntimeError("synthetic transcriber reached")

    monkeypatch.setattr(entry, "core", lambda: {"utils": SimpleNamespace(detect_device=lambda: detected, DeviceInfo=DeviceInfo),
                                              "transcriber": SimpleNamespace(Transcriber=transcriber)})
    return attempts


def test_gpu_unavailable_does_not_implicitly_start_cpu_transcription(monkeypatch):
    attempts = fake_core(monkeypatch, DeviceInfo("cpu", "CPU", 0, "int8"))
    with pytest.raises(RuntimeError, match="Автоматический переход на CPU отключён"):
        entry.run({"settings": {"device": "cuda"}})
    assert attempts == []


def test_cpu_profile_requires_confirmation_and_uses_explicit_cpu_device(monkeypatch):
    attempts = fake_core(monkeypatch, DeviceInfo("cuda", "Synthetic GPU", 12, "float16"))
    with pytest.raises(RuntimeError, match="Подтвердите"):
        entry.run({"settings": {"device": "cpu", "cpu_confirmed": False}})
    assert attempts == []
    with pytest.raises(RuntimeError, match="synthetic transcriber reached"):
        entry.run({"settings": {"device": "cpu", "cpu_confirmed": True}})
    assert attempts[0].type == "cpu" and attempts[0].compute_type == "int8"


def test_unsupported_gpu_architecture_stops_before_inference(monkeypatch):
    attempts = fake_core(monkeypatch, DeviceInfo("cuda", "Synthetic GPU", 12, "float16"))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(
        init=lambda: None, get_device_capability=lambda: (12, 0), get_arch_list=lambda: ["sm_86"])))
    with pytest.raises(RuntimeError, match="не поддерживает архитектуру"):
        entry.run({"settings": {"device": "cuda"}})
    assert attempts == []


def test_out_of_memory_emits_actionable_failure_without_cpu_fallback(monkeypatch):
    messages = []
    monkeypatch.setattr(entry, "emit", lambda kind, **data: messages.append({"type": kind, **data}))
    monkeypatch.setattr(sys, "argv", ["entry.py"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}\n"))

    def run(_):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(entry, "run", run)
    with pytest.raises(SystemExit) as exit_info:
        entry.main()
    assert exit_info.value.code == 1
    assert messages[0]["type"] == "error"
    assert "CPU автоматически не включается" in messages[0]["message"]


def test_external_adapter_reuses_inference_and_keeps_own_preparation_exports(monkeypatch, tmp_path):
    names = ("utils", "transcriber", "audio_processor", "exporters", "batch", "diarizer")
    imports = {"core." + name: SimpleNamespace(identity="own:" + name) for name in names}
    imports.update({"src." + name: SimpleNamespace(identity="external:" + name)
                    for name in ("transcriber", "diarizer")})
    seen = []

    def load(name):
        seen.append(name)
        return imports[name]

    monkeypatch.setattr(entry.importlib, "import_module", load)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("MEETING_ARCHIVE_EXTERNAL_ENGINE", str(tmp_path / "synthetic-external-engine"))
    loaded = entry.core()
    for name in ("utils", "audio_processor", "batch", "exporters"):
        assert loaded[name] is imports["core." + name]
    for name in ("transcriber", "diarizer"):
        assert loaded[name] is imports["src." + name]
    assert {name for name in seen if name.startswith("src.")} == {"src.transcriber", "src.diarizer"}



def test_whisper_without_speech_returns_empty_result_before_language_detection(monkeypatch):
    from meeting_archive.worker.core.transcriber import Transcriber
    from types import ModuleType
    audio_module = ModuleType("faster_whisper.audio")
    audio_module.decode_audio = lambda *args, **kwargs: [0] * 16000
    vad_module = ModuleType("faster_whisper.vad")
    vad_module.VadOptions = lambda **kwargs: kwargs
    vad_module.get_speech_timestamps = lambda *args: []
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", audio_module)
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", vad_module)
    asr = Transcriber("tiny", DeviceInfo("cpu", "CPU", 0, "int8"))
    asr._model = SimpleNamespace(transcribe=lambda *args, **kwargs: pytest.fail("No speech must not reach language detection"))
    output = asr.transcribe("synthetic.wav", language=None, use_vad=True)
    assert output.segments == [] and output.duration == 1
    assert output.language == "" and output.language_probability is None


def test_incomplete_own_cache_falls_back_to_complete_shared_weights(tmp_path, monkeypatch):
    from meeting_archive.worker.core.transcriber import installed_model_path
    repo = "models--Systran--faster-whisper-tiny"
    own = tmp_path / "own"
    shared = tmp_path / "shared"
    (own / repo / "snapshots/partial").mkdir(parents=True)
    snapshot = shared / repo / "snapshots/complete"
    snapshot.mkdir(parents=True)
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json"):
        (snapshot / name).write_bytes(b"synthetic fixture")
    monkeypatch.setenv("HF_HUB_CACHE", str(own))
    monkeypatch.setenv("MEETING_ARCHIVE_MODEL_CACHE", str(shared))
    assert installed_model_path("tiny") == snapshot
