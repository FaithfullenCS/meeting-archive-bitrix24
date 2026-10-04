import json

import pytest

from meeting_archive.db import Database
from meeting_archive.engine_catalog import installation_plan
from meeting_archive.modules import ModuleManager


def test_cpu_runtime_is_not_reported_as_cuda_ready(tmp_path):
    python = tmp_path / "venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"synthetic python")
    torch = tmp_path / "venv/Lib/site-packages/torch/__init__.py"
    torch.parent.mkdir(parents=True)
    torch.write_text("# synthetic", "utf-8")
    (tmp_path / "ready.json").write_text(json.dumps({"profile": "cpu"}), "utf-8")
    packages = [{"engine": "whisper", "model": "tiny"}]
    assert installation_plan(tmp_path, "cpu", packages)["common_ready"] is True
    cuda = installation_plan(tmp_path, "cuda", packages)
    assert cuda["common_ready"] is False
    assert cuda["download_gb"] > 7


def test_parakeet_optional_diarization_explicitly_counts_full_runtime(tmp_path):
    packages = [{"engine": "parakeet", "model": "parakeet-tdt-0.6b-v3-q8"}]
    minimal = installation_plan(tmp_path, "cuda", packages)
    full = installation_plan(tmp_path, "cuda", packages, full_features=True)
    assert minimal["download_gb"] < 1
    assert full["download_gb"] > 7
    assert full["weights_gb"] == minimal["weights_gb"]


@pytest.mark.asyncio
async def test_multiple_whisper_packages_share_runtime_and_do_not_redownload(monkeypatch, tmp_path, vault):
    manager = ModuleManager(tmp_path, vault)
    installed = set()
    runtimes, commands = [], []

    async def ensure_runtime(profile, progress, *, minimal=False):
        runtimes.append((profile, minimal))
        return "own-python"

    async def command(args, progress, *, stage):
        commands.append(args)
        installed.add(args[-1])

    monkeypatch.setattr(manager, "ensure_runtime", ensure_runtime)
    monkeypatch.setattr(manager, "command", command)
    monkeypatch.setattr("meeting_archive.hardware.worker_probe", lambda *_: {"compatible": True})
    monkeypatch.setattr("meeting_archive.engine_catalog.whisper_installed", lambda root, model: model in installed)
    packages = [{"engine": "whisper", "model": "tiny"}, {"engine": "whisper", "model": "base"}]
    await manager.install_packages("cuda", packages, lambda *_: None)
    assert runtimes == [("cuda", False)]
    assert [command[-1] for command in commands] == ["tiny", "base"]
    await manager.install_packages("cuda", packages + [{"engine": "whisper", "model": "small"}], lambda *_: None)
    assert [command[-1] for command in commands] == ["tiny", "base", "small"]


def test_manual_request_promotes_identical_waiting_local_task_without_duplicate(tmp_path):
    database = Database(tmp_path / "queue.sqlite")
    try:
        options = {"settings": {"engine": "whisper", "model": "base"}, "mode": "combined"}
        original = database.enqueue("transcribe", 12, {**options, "automatic": True}, next_at=9000000000)
        manual = database.enqueue("transcribe", 12, {**options, "automatic": False})
        assert manual == original
        row = database.rows("SELECT * FROM jobs WHERE id=?", (manual,))[0]
        assert json.loads(row["payload"])["automatic"] is False
        assert row["next_at"] == 0
        assert database.claim(("transcribe",), blocked_automatic=("transcribe",))["id"] == original
        alternative = database.enqueue("transcribe", 12, {**options, "settings": {"model": "small"}})
        assert alternative != original
    finally:
        database.close()


@pytest.mark.asyncio
async def test_cuda_incompatible_runtime_stops_before_downloading_models(monkeypatch, tmp_path, vault):
    manager = ModuleManager(tmp_path, vault)

    async def runtime(*_, **__):
        return "synthetic-python"

    async def download(*_, **__):
        pytest.fail("Models must not download after a failed GPU compatibility check")

    monkeypatch.setattr(manager, "ensure_runtime", runtime)
    monkeypatch.setattr(manager, "command", download)
    monkeypatch.setattr("meeting_archive.hardware.worker_probe", lambda *_: {"cuda": True, "compatible": False})
    with pytest.raises(ValueError, match="несовместимы"):
        await manager.install_packages("cuda", [{"engine": "whisper", "model": "tiny"}], lambda *_: None)
