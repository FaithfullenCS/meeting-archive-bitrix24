import json
import pytest

from meeting_archive.app import create_app, meeting_view
from meeting_archive.archive import clean_metadata
from meeting_archive.engine_catalog import installation_plan
from meeting_archive.modules import ModuleManager
from meeting_archive.settings import Settings
from meeting_archive.service import Service
from test_resource_reuse import resources as resource_fixture

resources = resource_fixture


def test_found_model_uses_existing_runtime_without_snapshot_or_download(resources, vault):
    r = resources
    manager = ModuleManager(r["target"].parent / "profile", vault)
    manager.discovered = {"sources": [{"source_id": "existing", "compatible": True,
        "environment_path": str(r["source"]), "models": ["tiny"], "versions": {}}], "cached_models": ["tiny"]}
    before = {p: p.read_bytes() for p in r["source"].rglob("*") if p.is_file()}
    model = manager.catalogue()["engines"][0]["models"][0]
    assert model["installed"] and model["origin"] == "computer"
    manager.require_engine(Settings(model="tiny"))
    python, env = manager.borrowed_environment("tiny")
    assert python == str(r["source"] / "Scripts/python.exe")
    assert env["HF_HUB_CACHE"] == str(r["cache"])
    assert env["HF_HUB_OFFLINE"] == "1" and env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert not manager.root.exists()
    assert before == {p: p.read_bytes() for p in r["source"].rglob("*") if p.is_file()}


def test_only_weights_are_not_reported_as_ready(tmp_path, vault):
    manager = ModuleManager(tmp_path, vault)
    manager.discovered = {"sources": [], "cached_models": ["tiny"]}
    tiny = manager.catalogue()["engines"][0]["models"][0]
    assert tiny["cached"] and not tiny["installed"]
    with pytest.raises(ValueError, match="Установите"):
        manager.require_engine(Settings(model="tiny"))


def test_plan_counts_only_missing_weights_with_existing_runtime(tmp_path):
    packages = [{"engine": "whisper", "model": "tiny"}, {"engine": "whisper", "model": "small"}]
    plan = installation_plan(tmp_path, "cuda", packages, cached_whisper=["tiny"], borrowed_runtime=True)
    assert plan["download_gb"] == plan["weights_gb"] == .5
    assert plan["common_ready"]


def test_availability_from_metadata_never_retains_download_url():
    item = {"callId": 42, "outcomes": ["transcription"], "tracks": [{"url": "https://private.test/secret"}]}
    meta = clean_metadata(item)
    assert meta["availability"] == {"audio": "available", "bitrix": "available"}
    assert "secret" not in json.dumps(meta)
    view = meeting_view({"metadata": json.dumps(meta), "portal": "synthetic", "source": "bitrix",
        "audio": "not_saved", "bitrix": "not_saved"})
    assert view["audio"] == view["bitrix"] == "available"
    assert clean_metadata({"callId": 42, "outcomes": [], "tracks": []})["availability"] == {
        "audio": "not_available", "bitrix": "waiting"}
    assert "availability" not in clean_metadata({"callId": 42})


@pytest.mark.asyncio
async def test_total_is_database_count_not_incremental_scan_count(tmp_path, vault):
    import httpx
    service = Service(tmp_path, vault=vault)
    service.catalogue["count"] = 14
    for call in range(414):
        service.db.upsert("synthetic", {"callId": call})
    app = create_app(service, launch_token="synthetic-launch", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        await client.get("/?launch=synthetic-launch")
        response = await client.get("/api/bootstrap")
        assert response.json()["catalogue"]["total"] == 414
        assert response.json()["catalogue"]["count"] == 14
    await service.stop()


@pytest.mark.asyncio
async def test_new_whisper_download_uses_borrowed_python_and_owned_cache(resources, vault, monkeypatch):
    manager = ModuleManager(resources["target"].parent / "profile", vault)
    manager.discovered = {"sources": [{"compatible": True, "environment_path": str(resources["source"]),
        "models": ["tiny"], "versions": {}}], "cached_models": ["tiny"]}
    calls = []
    async def command(args, progress, *, stage, environment):
        calls.append((args, environment))
    async def unexpected(*args, **kwargs):
        pytest.fail("Existing Python/libraries must not be installed again")
    monkeypatch.setattr(manager, "command", command)
    monkeypatch.setattr(manager, "ensure_runtime", unexpected)
    monkeypatch.setattr("meeting_archive.hardware.worker_probe", lambda *_: {"compatible": True})
    await manager.install_packages("cuda", [{"engine": "whisper", "model": "tiny"},
        {"engine": "whisper", "model": "small"}], lambda *_: None)
    assert len(calls) == 1 and calls[0][0][-1] == "small"
    assert calls[0][0][0] == str(resources["source"] / "Scripts/python.exe")
    assert calls[0][1]["HF_HUB_CACHE"] == str(manager.root / "models/hub")
    assert "HF_HUB_OFFLINE" not in calls[0][1]
    assert not (manager.root / "venv").exists()


def test_removed_found_environment_is_rejected_before_launch(resources, vault):
    manager = ModuleManager(resources["target"].parent / "profile", vault)
    manager.discovered = {"sources": [{"compatible": True, "environment_path": str(resources["source"]),
        "models": ["tiny"], "versions": {}}], "cached_models": ["tiny"]}
    (resources["site"] / "faster_whisper/__init__.py").unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        manager.borrowed_environment("tiny")


def test_diarization_plan_does_not_claim_missing_libraries_are_available(tmp_path, vault):
    manager = ModuleManager(tmp_path, vault)
    manager.discovered = {"sources": [{"compatible": True, "models": ["tiny"], "versions": {}}], "cached_models": ["tiny"]}
    assert not manager.catalogue()["features_ready"]
    plan = manager.installation_plan("cuda", [{"engine": "whisper", "model": "tiny"}], full_features=True)
    assert not plan["common_ready"] and plan["download_gb"] == 7
