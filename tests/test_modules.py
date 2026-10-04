from __future__ import annotations

import os
import asyncio
import json
import sys
import subprocess
from pathlib import Path

import pytest
import psutil

from meeting_archive.modules import ModuleManager


def test_cached_whisper_uses_full_runtime_for_diarization(tmp_path, vault, settings):
    module = ModuleManager(tmp_path, vault)
    module.discovered = {
        "cached_models": ["large-v3"],
        "sources": [{"compatible": True, "models": ["large-v3"], "versions": {}}],
    }
    settings.diarization = True
    with pytest.raises(ValueError, match="диаризац"):
        module.require_engine(settings)
    for name, content in {
        "ready.json": '{"profile":"cuda"}',
        "venv/Scripts/python.exe": "python",
        "venv/Lib/site-packages/torch/__init__.py": "torch",
        "venv/Lib/site-packages/pyannote/audio/__init__.py": "pyannote",
    }.items():
        file = module.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    assert module.processing_support(settings)["requires_install"] is False
    module.require_engine(settings)  # A borrowed Whisper environment without pyannote must not override a complete one.


def test_diarization_accepts_normalized_package_metadata_and_prefers_capable_source(tmp_path, vault, settings):
    module = ModuleManager(tmp_path, vault)
    basic = {"compatible": True, "models": ["large-v3"], "versions": {}}
    complete = {"compatible": True, "models": ["large-v3"], "versions": {"pyannote-audio": "3.3.2"}}
    module.discovered = {"cached_models": ["large-v3"], "sources": [basic, complete]}
    settings.diarization = True
    assert module.processing_support(settings)["requires_install"] is False
    module.require_engine(settings)
    assert module.catalogue()["features_ready"] is True
    assert module.borrowed_source("large-v3", diarization=True) is complete
    plan = module.installation_plan("cuda", [{"engine": "whisper", "model": "large-v3"}], full_features=True)
    assert plan["download_gb"] == 0 and plan["common_ready"] is True


def test_borrowed_diarization_can_download_its_models_without_reinstalling_libraries(tmp_path, vault, monkeypatch):
    module = ModuleManager(tmp_path, vault)
    source = {"compatible": True, "models": ["large-v3"], "versions": {"pyannote-audio": "3.3.2"},
              "environment_path": str(tmp_path / "existing")}
    module.discovered = {"cached_models": ["large-v3"], "sources": [source]}
    monkeypatch.setattr("meeting_archive.resource_reuse._probe_environment", lambda _: {
        "environment_path": source["environment_path"], "site_packages": str(tmp_path / "site")})
    monkeypatch.setattr("meeting_archive.resource_reuse._worker_distributions", lambda _: {})
    monkeypatch.setattr("meeting_archive.resource_reuse._site_files", lambda *_: [])
    monkeypatch.setattr("meeting_archive.resource_reuse._safe_file", lambda *args: args[0])
    monkeypatch.setattr("meeting_archive.resource_reuse._ffmpeg", lambda _: tmp_path / "tools/ffmpeg.exe")
    monkeypatch.setattr("meeting_archive.resource_reuse._models", lambda _: ["large-v3"])
    _, ordinary = module.borrowed_environment("large-v3")
    assert ordinary["HF_HUB_OFFLINE"] == "1"
    _, diarization = module.borrowed_environment("large-v3", diarization=True)
    assert "HF_HUB_OFFLINE" not in diarization and "TRANSFORMERS_OFFLINE" not in diarization


def test_remove_selected_owned_models_preserves_libraries_external_cache_and_other_models(tmp_path, vault):
    module = ModuleManager(tmp_path, vault)
    root = module.root
    root.mkdir()
    (root / ".meeting-archive-owned").write_text("owned")
    external = tmp_path / "external"
    external.mkdir()
    (external / "model.bin").write_bytes(b"external")
    for engine, model in [
        ("whisper", "large-v3"),
        ("whisper", "tiny"),
        ("parakeet", "parakeet-tdt-0.6b-v3-q8"),
        ("gigaam", "gigaam-v3-e2e-rnnt"),
    ]:
        names = module.model_targets(engine, model)
        file = root / names[0]
        if engine == "parakeet":
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"model")
        else:
            file.mkdir(parents=True, exist_ok=True)
            (file / "weight.bin").write_bytes(b"model")
        for marker in names[1:]:
            (root / marker).write_text('{"synthetic":true}')
    runtime = root / "venv/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"keep runtime")
    packages = [{"engine": "whisper", "model": "large-v3"}, {"engine": "parakeet", "model": "parakeet-tdt-0.6b-v3-q8"}]
    plan = module.model_removal_plan(packages)
    assert plan["files"] == 4
    module.remove_models(packages, plan["token"])
    assert not (root / module.model_targets("whisper", "large-v3")[0]).exists()
    assert not (root / module.model_targets("parakeet", "parakeet-tdt-0.6b-v3-q8")[0]).exists()
    assert (root / module.model_targets("whisper", "tiny")[0]).is_dir()
    assert (root / "engines/gigaam/model").is_dir()
    assert runtime.read_bytes() == b"keep runtime" and (external / "model.bin").read_bytes() == b"external"


def test_model_removal_requires_ownership_and_fresh_inventory(tmp_path, vault):
    module = ModuleManager(tmp_path, vault)
    root = module.root
    root.mkdir()
    packages = [{"engine": "whisper", "model": "tiny"}]
    with pytest.raises(ValueError, match="принадлежат"):
        module.model_removal_plan(packages)
    (root / ".meeting-archive-owned").write_text("owned")
    directory = root / module.model_targets("whisper", "tiny")[0]
    directory.mkdir(parents=True)
    file = directory / "weight"
    file.write_bytes(b"a")
    plan = module.model_removal_plan(packages)
    file.write_bytes(b"changed")
    with pytest.raises(ValueError, match="изменился"):
        module.remove_models(packages, plan["token"])
    assert file.read_bytes() == b"changed"


def test_cleanup_rejects_external_link_before_any_delete(tmp_path):
    from meeting_archive.cleanup import selection_plan

    root = tmp_path / "archive"
    root.mkdir()
    external = tmp_path / "private"
    external.mkdir()
    (external / "keep").write_text("keep")
    (root / "audio").mkdir()
    (root / "audio/first.wav").write_bytes(b"keep")
    link = root / "audio/linked"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)], capture_output=True)
        if result.returncode:
            pytest.skip("Junction creation unavailable")
    else:
        link.symlink_to(external, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="ссылк"):
            selection_plan(root, ["audio"])
        with pytest.raises(ValueError, match="ссылк"):
            selection_plan(root, ["audio"], internal_links=True)
        assert (external / "keep").read_text() == "keep" and (root / "audio/first.wav").exists()
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


@pytest.mark.asyncio
async def test_discovery_shutdown_stops_and_joins_read_only_scan(tmp_path, vault, monkeypatch):
    import threading

    started, stopped = threading.Event(), threading.Event()

    def slow_discovery(*, cancelled):
        started.set()
        while not cancelled():
            stopped.wait(.01)
        stopped.set()
        from meeting_archive.resource_reuse import ResourceReuseCancelled
        raise ResourceReuseCancelled("stopped")

    monkeypatch.setattr("meeting_archive.resource_reuse.discover_resources", slow_discovery)
    module = ModuleManager(tmp_path, vault)
    task = asyncio.create_task(module.discover())
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(.01)
    assert started.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert stopped.is_set()
    assert module.discovery_at == 0


@pytest.mark.asyncio
async def test_remove_only_owned_module_leaves_archive_and_external_application(tmp_path, vault):
    home = tmp_path / "home"
    module = home / "module"
    module.mkdir(parents=True)
    (module / ".meeting-archive-owned").write_text("owned", encoding="utf-8")
    (module / "worker.bin").write_bytes(b"synthetic worker")
    archive = home / "archive"
    archive.mkdir()
    (archive / "voice.wav").write_bytes(b"synthetic original")
    external = tmp_path / "external"
    external.mkdir()
    (external / "settings.json").write_text("synthetic external settings", encoding="utf-8")
    await ModuleManager(home, vault).remove()
    assert not module.exists()
    assert (archive / "voice.wav").read_bytes() == b"synthetic original"
    assert (external / "settings.json").read_text("utf-8") == "synthetic external settings"


@pytest.mark.asyncio
async def test_remove_unowned_directory_fails_without_touching_contents(tmp_path, vault):
    module = tmp_path / "module"
    module.mkdir()
    (module / "private.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError):
        await ModuleManager(tmp_path, vault).remove()
    assert (module / "private.txt").read_text("utf-8") == "preserve"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Native Windows junction regression")
@pytest.mark.parametrize("root_link", [True, False])
async def test_remove_rejects_root_and_nested_junctions_before_deleting(tmp_path, vault, root_link):
    import _winapi
    home = tmp_path / "home"
    home.mkdir()
    external = home / "external-application"
    external.mkdir()
    (external / "private.txt").write_text("preserve", encoding="utf-8")
    (external / ".meeting-archive-owned").write_text("owned marker present does not authorize junction", encoding="utf-8")
    module = home / "module"
    if root_link:
        _winapi.CreateJunction(str(external), str(module))
        link = module
    else:
        module.mkdir()
        (module / ".meeting-archive-owned").write_text("owned", encoding="utf-8")
        link = module / "cache-link"
        _winapi.CreateJunction(str(external), str(link))
    try:
        with pytest.raises(ValueError):
            await ModuleManager(home, vault).remove()
        assert (external / "private.txt").read_text("utf-8") == "preserve"
    finally:
        if link.exists():
            os.rmdir(link)  # Remove junction itself without following its target.


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Native Windows junction regression")
async def test_install_rejects_module_root_junction_before_writing(tmp_path, vault, monkeypatch):
    import _winapi
    home = tmp_path / "home"
    home.mkdir()
    external = home / "external-application"
    external.mkdir()
    module = home / "module"
    _winapi.CreateJunction(str(external), str(module))
    manager = ModuleManager(home, vault)
    monkeypatch.setattr(manager, "uv", lambda: Path("unused.exe"))

    async def unexpected_command(*_, **__):
        pytest.fail("Installer followed root junction before safety check")

    monkeypatch.setattr(manager, "command", unexpected_command)
    try:
        with pytest.raises(ValueError):
            await manager.install("cpu", "tiny", lambda *_: None)
        assert not (external / ".meeting-archive-owned").exists()
    finally:
        os.rmdir(module)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Native Windows junction regression")
async def test_remove_rejects_junction_in_module_home_ancestor(tmp_path, vault):
    import _winapi
    external = tmp_path / "external"
    external.mkdir()
    module = external / "module"
    module.mkdir()
    (module / ".meeting-archive-owned").write_text("owned", encoding="utf-8")
    (module / "preserve.txt").write_text("preserve", encoding="utf-8")
    alias = tmp_path / "linked-home"
    _winapi.CreateJunction(str(external), str(alias))
    try:
        with pytest.raises(ValueError):
            await ModuleManager(alias, vault).remove()
        assert (module / "preserve.txt").read_text("utf-8") == "preserve"
    finally:
        os.rmdir(alias)


@pytest.mark.asyncio
async def test_installer_uses_owned_runtime_cache_and_hashed_locks_then_can_reinstall(tmp_path, vault, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    manager = ModuleManager(home, vault)
    monkeypatch.setattr(manager, "uv", lambda: Path("synthetic-uv.exe"))
    calls = []

    async def command(args, *_args, **_kwargs):
        calls.append(args)
        if args[1:3] == ["python", "install"]:
            runtime = manager.root / "python/cpython-3.12.15-windows-x86_64-none/python.exe"
            runtime.parent.mkdir(parents=True, exist_ok=True)
            runtime.write_bytes(b"synthetic runtime")
        elif args[1] == "venv":
            python = manager.root / "venv/Scripts/python.exe"
            assert "--allow-existing" in args
            assert "cpython-3.12.15-" in args[args.index("--python") + 1]
            if python.exists() and "--allow-existing" not in args:
                raise RuntimeError("A virtual environment already exists")
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"synthetic worker")
        elif "--download" in args:
            model = manager.root / "models/tiny.bin"
            model.parent.mkdir(parents=True, exist_ok=True)
            model.write_bytes(b"synthetic model")

    monkeypatch.setattr(manager, "command", command)
    env = manager.env()
    for name in ("HF_HOME", "HF_HUB_CACHE", "UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "UV_PYTHON_BIN_DIR"):
        assert Path(env[name]).is_relative_to(manager.root)
    await manager.install("cpu", "tiny", lambda *_: None)
    assert manager.status()["installed"]
    assert json.loads((manager.root / "ready.json").read_text("utf-8"))["model"] == "tiny"
    sync = next(args for args in calls if args[1:3] == ["pip", "sync"])
    assert "--require-hashes" in sync
    assert sync[sync.index("--index-strategy") + 1] == "unsafe-best-match"
    assert any(str(arg).endswith("worker-cpu.lock") for arg in sync)
    existing_environment_file = manager.root / "venv/preserve-on-reinstall.txt"
    existing_environment_file.write_text("synthetic existing environment", encoding="utf-8")
    await manager.install("cpu", "tiny", lambda *_: None)
    assert manager.status()["installed"]
    assert existing_environment_file.read_text("utf-8") == "synthetic existing environment"
    assert len([args for args in calls if args[1] == "venv"]) == 2
    await manager.remove()
    assert not manager.root.exists()
    await manager.install("cpu", "tiny", lambda *_: None)
    assert manager.status()["installed"]


@pytest.mark.asyncio
async def test_installer_does_not_use_stale_runtime_when_requested_python_is_missing(tmp_path, vault, monkeypatch):
    manager = ModuleManager(tmp_path, vault)
    monkeypatch.setattr(manager, "uv", lambda: Path("synthetic-uv.exe"))
    stages = []

    async def command(args, *_args, **kwargs):
        stages.append(kwargs["stage"])
        if args[1:3] == ["python", "install"]:
            stale = manager.root / "python/cpython-3.12.9-windows-x86_64-none/python.exe"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale synthetic runtime")

    monkeypatch.setattr(manager, "command", command)
    with pytest.raises(RuntimeError, match="Не найден управляемый Python"):
        await manager.install("cpu", "tiny", lambda *_: None)
    assert stages == ["Python"]
    assert not (manager.root / "venv").exists()
    assert not manager.status()["installed"]


@pytest.mark.asyncio
async def test_cancel_install_terminates_silent_subprocess_and_clears_running_state(tmp_path, vault):
    manager = ModuleManager(tmp_path, vault)
    seen = asyncio.Event()

    def progress(*_):
        seen.set()

    task = asyncio.create_task(manager.command([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
                                               progress, stage="Synthetic installation"))
    started = asyncio.create_task(seen.wait())
    completed, _ = await asyncio.wait({task, started}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    if task in completed:
        started.cancel()
        error = task.exception()
        if isinstance(error, PermissionError):
            pytest.skip("Restricted Windows sandbox denies asyncio named pipes; real subprocess case runs in CI")
        await task
        pytest.fail("Installer exited before cancellation")
    if started not in completed:
        started.cancel()
        task.cancel()
        await asyncio.gather(task, started, return_exceptions=True)
        pytest.fail("Synthetic installer did not start")
    await manager.stop_install()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert manager.process is None


@pytest.mark.asyncio
async def test_install_does_not_claim_or_modify_preexisting_unowned_directory(tmp_path, vault, monkeypatch):
    manager = ModuleManager(tmp_path, vault)
    manager.root.mkdir()
    sentinel = manager.root / "private-existing-environment.txt"
    sentinel.write_text("preserve", encoding="utf-8")

    async def unexpected_command(*_, **__):
        pytest.fail("Installer modified or adopted an unowned existing environment")

    monkeypatch.setattr(manager, "command", unexpected_command)
    with pytest.raises(ValueError):
        await manager.install("cpu", "tiny", lambda *_: None)
    assert not (manager.root / ".meeting-archive-owned").exists()
    assert sentinel.read_text("utf-8") == "preserve"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows venv process-tree cancellation regression")
async def test_stop_install_kills_owned_worker_grandchildren(tmp_path, vault):
    manager = ModuleManager(tmp_path, vault)
    started = asyncio.Event()
    child_ids = []

    def progress(_, message):
        if "{" in message:
            identity = json.loads(message[message.index("{"):])
            child_ids.append(identity["child"])
            started.set()

    script = ("import json,os,subprocess,sys,time; "
              "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
              "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
              "print(json.dumps({'parent':os.getpid(),'child':child.pid}),flush=True); time.sleep(30)")
    task = asyncio.create_task(manager.command([sys.executable, "-c", script], progress, stage="Synthetic child tree"))
    observed = asyncio.create_task(started.wait())
    completed, _ = await asyncio.wait({task, observed}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    owned_children = []
    if task in completed:
        observed.cancel()
        error = task.exception()
        if isinstance(error, PermissionError):
            pytest.skip("Restricted Windows sandbox denies asyncio named pipes; process tree case runs in CI")
        await task
        pytest.fail("Synthetic worker exited before cancellation")
    try:
        assert observed in completed, "Synthetic worker did not start"
        owned_children = psutil.Process(manager.process.pid).children(recursive=True)
        assert child_ids[0] in {process.pid for process in owned_children}
        await manager.stop_install()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        _, alive = await asyncio.to_thread(psutil.wait_procs, owned_children, timeout=5)
        assert not alive, "Cancelling the installer left its owned descendant running"
        assert not psutil.pid_exists(child_ids[0])
        assert manager.process is None
    finally:
        observed.cancel()
        if not task.done():
            await manager.stop_install()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for process in owned_children:
            if process.is_running():
                process.kill()  # Only captured descendants of this exact test task.
