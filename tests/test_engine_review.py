"""Integration checks for package readiness and automatic scheduling boundaries."""
import asyncio
import json
import os

import httpx
import pytest

from meeting_archive.bitrix import BitrixClient
from meeting_archive.engine_catalog import catalogue, installation_plan
from meeting_archive.service import Service
from meeting_archive.worker.core import gigaam


def write_file(path, content=b"synthetic structural fixture"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def sparse_file(path, length):
    """A size-only fixture, never downloaded or usable as model weights."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes
            control = ctypes.WinDLL("kernel32", use_last_error=True).DeviceIoControl
            control.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
            control.restype = wintypes.BOOL
            returned = wintypes.DWORD()
            if not control(msvcrt.get_osfhandle(stream.fileno()), 0x900C4, None, 0, None, 0,
                           ctypes.byref(returned), None):
                pytest.skip("This filesystem does not support synthetic sparse model fixtures")
        stream.truncate(length)


def full_runtime(root, profile="cuda"):
    write_file(root / "ready.json", json.dumps({"profile": profile}).encode())
    write_file(root / "venv/Scripts/python.exe")
    write_file(root / "venv/Lib/site-packages/torch/__init__.py")


def gigaam_layout(root):
    full_runtime(root)
    engine = root / "engines/gigaam"
    write_file(engine / "ready.json", json.dumps({"model": gigaam.MODEL_NAME,
        "source_commit": gigaam.SOURCE_COMMIT, "model_revision": gigaam.MODEL_REVISION}).encode())
    for name, (size, _) in gigaam.MODEL_FILES.items():
        sparse_file(engine / "model" / name, size)
    for name in gigaam.SOURCE_FILES_SHA256:
        write_file(engine / "packages/gigaam" / name)
    for name in ("hydra", "omegaconf", "silero_vad", "sentencepiece"):
        write_file(engine / "addons" / name / "__init__.py")
    return engine


def installed(root, engine, profile="cuda"):
    return next(item["models"][0]["installed"] for item in catalogue(root, profile)["engines"] if item["id"] == engine)


@pytest.mark.parametrize("profile", ["cuda", "cpu"])
def test_complete_gigaam_layout_and_compatible_runtime_is_available_without_loading_weights(tmp_path, profile):
    gigaam_layout(tmp_path)
    assert installed(tmp_path, "gigaam", profile) is True


@pytest.mark.parametrize("missing", ["engines/gigaam/model/tokenizer.model", "engines/gigaam/packages/gigaam/model.py",
                                    "engines/gigaam/addons/silero_vad/__init__.py",
                                    "engines/gigaam/addons/omegaconf/__init__.py",
                                    "venv/Lib/site-packages/torch/__init__.py", "venv/Scripts/python.exe"])
def test_missing_required_gigaam_artifacts_or_runtime_make_it_unavailable(tmp_path, missing):
    gigaam_layout(tmp_path)
    (tmp_path / missing).unlink()
    assert installed(tmp_path, "gigaam") is False


def test_wrong_gigaam_weight_size_is_not_reported_as_installed(tmp_path):
    engine = gigaam_layout(tmp_path)
    (engine / "model/pytorch_model.bin").write_bytes(b"interrupted download")
    assert installed(tmp_path, "gigaam") is False


@pytest.mark.parametrize("relative", ["ready.json", "engines/gigaam/ready.json"])
@pytest.mark.parametrize("content", [b"{broken JSON", b"[]", b"null"])
def test_corrupt_markers_fail_closed_without_crashing_catalogue(tmp_path, relative, content):
    gigaam_layout(tmp_path)
    (tmp_path / relative).write_bytes(content)
    assert installed(tmp_path, "gigaam") is False


def test_cpu_to_cuda_upgrade_plan_counts_the_required_shared_runtime(tmp_path):
    full_runtime(tmp_path, "cpu")
    plan = installation_plan(tmp_path, "cuda", [{"engine": "gigaam", "model": gigaam.MODEL_NAME}])
    assert plan["common_ready"] is False
    assert plan["download_gb"] >= 7


def test_complete_unchanged_gigaam_weights_are_not_counted_twice_in_plan(tmp_path):
    gigaam_layout(tmp_path)
    plan = installation_plan(tmp_path, "cpu", [{"engine": "gigaam", "model": gigaam.MODEL_NAME}])
    assert plan["common_ready"] is True
    assert plan["weights_gb"] == 0
    assert plan["download_gb"] == 0


@pytest.mark.parametrize("engine,marker", [
    ("gigaam", "engines/gigaam/ready.json"),
    ("parakeet", "engines/parakeet/ready-cuda.json"),
])
def test_ready_marker_without_actual_engine_artifacts_is_not_installed(tmp_path, engine, marker):
    path = tmp_path / marker
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"model": "synthetic", "profile": "cuda"}), "utf-8")
    models = next(item["models"] for item in catalogue(tmp_path)["engines"] if item["id"] == engine)
    assert all(not model["installed"] for model in models)


@pytest.mark.parametrize("field", ["runtime_path", "model_path"])
@pytest.mark.parametrize("value", [None, [], {}, 123, ""])
def test_invalid_parakeet_paths_do_not_break_other_engines(tmp_path, field, value):
    from meeting_archive.worker.core.model_catalog import PARAKEET_MODEL
    from meeting_archive.worker.install_parakeet import MODEL_BYTES, MODEL_FILE, MODEL_REVISION, VERSION

    gigaam_layout(tmp_path)
    engine = tmp_path / "engines/parakeet"
    binary = write_file(engine / "runtime-cuda/synthetic.exe")
    model = engine / "models" / MODEL_FILE
    sparse_file(model, MODEL_BYTES)
    marker = engine / "ready-cuda.json"
    data = {"model": PARAKEET_MODEL, "profile": "cuda", "runtime_version": VERSION,
            "model_revision": MODEL_REVISION, "runtime_path": str(binary), "model_path": str(model)}
    write_file(marker, json.dumps(data).encode())
    assert installed(tmp_path, "parakeet") is True

    data[field] = value
    marker.write_text(json.dumps(data), "utf-8")
    assert installed(tmp_path, "parakeet") is False
    assert installed(tmp_path, "gigaam") is True
    plan = installation_plan(tmp_path, "cuda", [{"engine": "gigaam", "model": gigaam.MODEL_NAME}])
    assert plan["download_gb"] == 0


def test_whisper_partial_download_without_configuration_is_not_installed(tmp_path):
    folder = tmp_path / "models/hub/models--Systran--faster-whisper-tiny/snapshots/synthetic-revision"
    folder.mkdir(parents=True)
    (folder / "model.bin").write_bytes(b"interrupted synthetic checkpoint")
    whisper = next(engine["models"] for engine in catalogue(tmp_path)["engines"] if engine["id"] == "whisper")
    assert next(model for model in whisper if model["id"] == "tiny")["installed"] is False


@pytest.mark.asyncio
async def test_service_runs_manual_task_outside_automatic_window(monkeypatch, tmp_path, settings, vault):
    home = tmp_path / "home"
    settings.save(home)
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    service = Service(home, client=client, vault=vault)
    client.settings = service.settings
    automatic = service.db.enqueue("transcribe", 1, {"automatic": True})
    manual = service.db.enqueue("transcribe", 2, {"automatic": False})
    started = []

    async def perform(job, progress):
        started.append(job["id"])
        service.alive = False

    monkeypatch.setattr("meeting_archive.service.window_status", lambda _: {"allowed": False})
    monkeypatch.setattr(service, "perform", perform)
    service.alive = True
    try:
        await asyncio.wait_for(service.job_loop(("transcribe",)), timeout=2)
        assert started == [manual]
        assert service.db.rows("SELECT state FROM jobs WHERE id=?", (automatic,))[0]["state"] == "queued"
        assert service.db.rows("SELECT state FROM jobs WHERE id=?", (manual,))[0]["state"] == "done"
    finally:
        await client.close()
        service.db.close()
