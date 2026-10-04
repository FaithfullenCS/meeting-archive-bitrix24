import json
from types import SimpleNamespace

import pytest

from meeting_archive.hardware import runtime_plan, worker_probe
from meeting_archive.modules import ModuleManager


@pytest.mark.parametrize("capability,driver,lock", [
    ("12.0", "616.64", "cuda"), ("10.0", "570.65", "cuda"),
    ("8.6", "560.76", "cuda126"), ("8.9", "600.00", "cuda126"),
    ("6.1", "572.00", "cuda126"),
])
def test_gpu_runtime_selected_by_architecture_and_driver(capability, driver, lock):
    # GPU names, including the Laptop suffix, never select the package.
    plan = runtime_plan("cuda", {"architecture": "x64", "gpu": {
        "name": "Any NVIDIA Laptop GPU", "compute_capability": capability, "driver": driver}})
    assert plan["available"] and plan["lock_profile"] == lock


@pytest.mark.parametrize("hardware", [
    {"architecture": "arm64", "gpu": None},
    {"architecture": "x64", "gpu": None, "gpu_adapters": ["AMD Radeon"]},
    {"architecture": "x64", "gpu": {"compute_capability": "12.0", "driver": "560.76"}},
    {"architecture": "x64", "gpu": {"compute_capability": "8.0", "driver": "552.00"}},
    {"architecture": "x64", "gpu": {"compute_capability": "unknown", "driver": "600.00"}},
    {"architecture": "x64", "gpu": {"compute_capability": "3.0", "driver": "600.00"}},
])
def test_unsupported_gpu_or_host_never_falls_back_to_cpu(hardware):
    plan = runtime_plan("cuda", hardware)
    assert not plan["available"] and plan["reason"] and plan["lock_profile"] == ""


def test_amd_cpu_is_supported_but_native_windows_arm_is_not():
    assert runtime_plan("cpu", {"architecture": "x64", "cpu_name": "AMD Ryzen"})["lock_profile"] == "cpu"
    assert not runtime_plan("cpu", {"architecture": "arm64"})["available"]


def test_real_kernel_result_not_exact_arch_name_defines_gpu_compatibility(monkeypatch):
    # CUDA can execute compatible lower-minor cubins/PTX. Exact name matching
    # incorrectly rejected, for example, sm_89 hardware using sm_86 libraries.
    result = {"cuda": True, "capability": [8, 9], "arch": ["sm_86"], "torch": "synthetic"}
    monkeypatch.setattr("meeting_archive.hardware.subprocess.run", lambda *_, **__: SimpleNamespace(returncode=0, stdout=json.dumps(result)))
    assert worker_probe("synthetic", {})["compatible"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("capability,lock", [("8.6", "worker-cuda126.lock"), ("12.0", "worker-cuda.lock")])
async def test_installer_passes_selected_hashed_lock(monkeypatch, tmp_path, vault, capability, lock):
    monkeypatch.setattr("meeting_archive.hardware.detect", lambda: {"architecture": "x64", "gpu": {
        "compute_capability": capability, "driver": "600.00"}})
    manager = ModuleManager(tmp_path, vault)
    commands = []

    async def command(args, *_args, **_kwargs):
        commands.append(args)
        if args[1:3] == ["python", "install"]:
            runtime = manager.root / "python/cpython-3.12.15-synthetic/python.exe"
            runtime.parent.mkdir(parents=True)
            runtime.touch()
        if args[1] == "venv":
            python = manager.root / "venv/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()

    monkeypatch.setattr(manager, "uv", lambda: "synthetic-uv")
    monkeypatch.setattr(manager, "command", command)
    await manager.install("cuda", "tiny", lambda *_: None, download=False)
    sync = next(args for args in commands if args[1:3] == ["pip", "sync"])
    assert any(arg.endswith(lock) for arg in sync) and "--require-hashes" in sync
    assert not any("--download" in args for args in commands)


@pytest.mark.asyncio
async def test_arm_install_rejected_before_creating_module_or_downloading(monkeypatch, tmp_path, vault):
    monkeypatch.setattr("meeting_archive.hardware.native_architecture", lambda: "arm64")
    manager = ModuleManager(tmp_path, vault)
    with pytest.raises(ValueError, match="Windows ARM"):
        await manager.install("cpu", "tiny", lambda *_: None)
    assert not manager.root.exists()
