"""Internet-marked .NET assemblies must work without changing unrelated files."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from meeting_archive import desktop_runtime as runtime


def bundle(tmp_path, payload=b"synthetic DLL"):
    files = {}
    for relative in runtime.CLR_DLLS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        files[relative] = hashlib.sha256(payload).hexdigest()
    (tmp_path / "update-manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")
    return [tmp_path / name for name in runtime.CLR_DLLS]


def mark(path):
    Path(str(path) + ":Zone.Identifier").write_text("[ZoneTransfer]\nZoneId=3\n", encoding="ascii")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows alternate data streams")
def test_unblocks_only_verified_desktop_dlls(tmp_path):
    dlls = bundle(tmp_path)
    for path in dlls:
        mark(path)
    recording = tmp_path / "Данные" / "recording.dll"
    recording.parent.mkdir()
    recording.write_bytes(b"user file")
    mark(recording)
    assert runtime.unblock_verified_clr(tmp_path) == list(runtime.CLR_DLLS)
    assert all(not Path(str(path) + ":Zone.Identifier").exists() for path in dlls)
    assert Path(str(recording) + ":Zone.Identifier").exists()
    assert runtime.unblock_verified_clr(tmp_path) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows alternate data streams")
@pytest.mark.parametrize("defect", ["changed", "missing", "manifest", "invalid-hash", "unlisted"])
def test_incomplete_or_changed_bundle_is_never_unblocked(tmp_path, defect):
    dlls = bundle(tmp_path)
    for path in dlls:
        mark(path)
    if defect == "changed":
        dlls[-1].write_bytes(b"changed DLL")
    elif defect == "missing":
        dlls[-1].unlink()
    elif defect == "manifest":
        (tmp_path / "update-manifest.json").write_text("invalid JSON")
    else:
        manifest = json.loads((tmp_path / "update-manifest.json").read_text())
        if defect == "invalid-hash":
            manifest["files"][runtime.CLR_DLLS[-1]] = "not a hash"
        else:
            del manifest["files"][runtime.CLR_DLLS[-1]]
        (tmp_path / "update-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="распакуйте"):
        runtime.unblock_verified_clr(tmp_path)
    assert Path(str(dlls[0]) + ":Zone.Identifier").exists()


def test_source_run_does_not_change_installed_files(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(runtime, "unblock_verified_clr", lambda _: pytest.fail("Source launch touched a bundle"))
    runtime.prepare_clr_runtime()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows packaged entrypoint")
def test_packaged_launch_prepares_its_own_bundle(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "MeetingArchive.exe"))
    monkeypatch.setattr(runtime, "unblock_verified_clr", lambda root: calls.append(root))
    runtime.prepare_clr_runtime()
    assert calls == [tmp_path]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows alternate data streams")
def test_read_only_stream_failure_has_actionable_message(tmp_path, monkeypatch):
    dlls = bundle(tmp_path)
    mark(dlls[0])
    original = Path.unlink
    def deny_stream(path, *args, **kwargs):
        if str(path).endswith(":Zone.Identifier"):
            raise PermissionError("synthetic read-only folder")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", deny_stream)
    with pytest.raises(ValueError, match="свойства исходного ZIP"):
        runtime.unblock_verified_clr(tmp_path)


def test_symlink_is_never_unblocked(tmp_path, monkeypatch):
    dlls = bundle(tmp_path)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == dlls[0] or original(path))
    with pytest.raises(ValueError, match="распакуйте"):
        runtime.unblock_verified_clr(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Actual Windows .NET loader")
def test_actual_loader_resolves_downloaded_assembly_after_preparation(tmp_path):
    import pythonnet
    original = Path(pythonnet.__file__).parent / "runtime" / "Python.Runtime.dll"
    dlls = bundle(tmp_path, original.read_bytes())
    assembly = dlls[0]
    mark(assembly)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "DOTNET", "VIRTUAL_ENV"))}
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    code = ('import sys, clr_loader; runtime=clr_loader.get_netfx(); '
            'runtime.get_assembly(sys.argv[1]).get_function("Python.Runtime.Loader.Initialize"); '
            'print("Loader.Initialize resolved")')
    def resolve():
        return subprocess.run([sys.executable, "-I", "-c", code, str(assembly)], env=env,
                              capture_output=True, text=True, timeout=15)
    blocked = resolve()
    assert blocked.returncode != 0
    assert "Failed to resolve Python.Runtime.Loader.Initialize" in blocked.stderr
    runtime.unblock_verified_clr(tmp_path)
    loaded = resolve()
    assert loaded.returncode == 0, loaded.stderr
    assert "Loader.Initialize resolved" in loaded.stdout
