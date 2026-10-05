from types import SimpleNamespace
import subprocess

import pytest

from meeting_archive.browser import ArchiveWindow
from meeting_archive import webview_runtime as runtime


def shell(tmp_path):
    calls = []
    window = ArchiveWindow(tmp_path, on_ready=lambda: calls.append("ready"))
    window.window = SimpleNamespace(show=lambda: calls.append("show"), hide=lambda: calls.append("hide"),
                                    restore=lambda: calls.append("restore"), destroy=window.closed.set,
                                    get_current_url=lambda: "http://localhost:8765/", evaluate_js=lambda _: True)
    return window, calls


def test_cold_start_open_is_queued_then_same_window_restored(tmp_path):
    window, calls = shell(tmp_path)
    for _ in range(10):
        window.open()
    assert calls == []
    window.window_shown()
    assert calls == ["show"]
    assert window.closing() is False
    window.minimized = True
    window.open()
    assert calls == ["show", "hide", "restore", "show"]
    assert window.close() and window.closing() is True
    window.open()
    assert calls[-1] == "show"


def test_readiness_requires_both_native_and_frontend(tmp_path):
    window, calls = shell(tmp_path)
    assert not window.mark_ready()
    window.page_loaded()
    assert window.ready and calls == ["ready"]
    assert window.close()
    assert not window.ready


def test_ready_callback_survives_inverse_event_order(tmp_path):
    window, calls = shell(tmp_path)
    window.page_loaded()
    assert not window.ready
    assert window.mark_ready() and calls == ["ready"]


def test_api_ack_cannot_make_wrong_native_page_ready(tmp_path):
    window, calls = shell(tmp_path)
    window.window.get_current_url = lambda: "http://localhost:8765/missing-page"
    window.mark_ready()
    window.page_loaded()
    assert not window.ready and not calls


def test_external_address_cannot_replace_archive_window(tmp_path):
    window, _ = shell(tmp_path)
    with pytest.raises(ValueError):
        window.open("https://untrusted.test")


def test_installed_runtime_does_not_run_installer(monkeypatch):
    monkeypatch.setattr(runtime, "runtime_available", lambda: True)
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: pytest.fail("Unexpected installer"))
    runtime.ensure_runtime()


def test_missing_runtime_requires_signature_then_silent_install_and_detection(monkeypatch):
    values = iter([False, True])
    calls = []
    monkeypatch.setattr(runtime, "runtime_available", lambda: next(values))
    monkeypatch.setattr(runtime, "verify_bootstrapper", lambda path: calls.append("signature"))
    monkeypatch.setattr(runtime.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    runtime.ensure_runtime()
    assert calls[0] == "signature"
    assert calls[1][0][1:] == ["/silent", "/install"]
    assert calls[1][1]["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert calls[1][1]["timeout"] == 180


def test_invalid_signature_never_executes_installer(monkeypatch):
    monkeypatch.setattr(runtime, "runtime_available", lambda: False)
    def reject(path):
        raise ValueError("invalid signature")
    monkeypatch.setattr(runtime, "verify_bootstrapper", reject)
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: pytest.fail("Untrusted executable"))
    with pytest.raises(ValueError, match="signature"):
        runtime.ensure_runtime()


def test_signature_verification_policy_failure_has_no_shell_code_in_message(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "technical PowerShell command")
    monkeypatch.setattr(runtime.subprocess, "run", fail)
    with pytest.raises(ValueError, match="Windows не позволила проверить") as error:
        runtime.verify_bootstrapper(tmp_path / "installer.exe")
    assert "PowerShell" not in str(error.value)


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("installer", 180), subprocess.CalledProcessError(1, "installer")])
def test_install_failure_is_actionable(monkeypatch, failure):
    monkeypatch.setattr(runtime, "runtime_available", lambda: False)
    monkeypatch.setattr(runtime, "verify_bootstrapper", lambda _: None)
    def fail(*a, **k):
        raise failure
    monkeypatch.setattr(runtime.subprocess, "run", fail)
    with pytest.raises(ValueError, match="Проверьте интернет"):
        runtime.ensure_runtime()


def test_cleanup_preserves_active_legacy_profile_and_unknown_sibling(tmp_path, monkeypatch):
    import psutil
    profile = tmp_path / "browser-window"
    profile.mkdir()
    (profile / "cache").write_text("old cache")
    sibling = tmp_path / "recording.wav"
    sibling.write_bytes(b"private")
    monkeypatch.setattr(psutil, "process_iter", lambda _: [SimpleNamespace(info={"cmdline": ["browser", "--user-data-dir=" + str(profile)]})])
    runtime.cleanup_legacy_profile(tmp_path)
    assert profile.exists()
    monkeypatch.setattr(psutil, "process_iter", lambda _: [])
    runtime.cleanup_legacy_profile(tmp_path)
    assert not profile.exists() and sibling.read_bytes() == b"private"


def test_handled_startup_failure_exits_without_pyinstaller_traceback_dialog(tmp_path, monkeypatch):
    import json
    import sys
    from meeting_archive import launcher
    home = tmp_path / "desktop-smoke-startup-failure"
    monkeypatch.setattr(sys, "argv", ["archive", "--home", str(home), "--desktop-smoke", "--no-browser", "--no-tray"])
    def close_handle(_):
        pass
    monkeypatch.setattr(launcher, "single_instance", lambda _: (SimpleNamespace(CloseHandle=close_handle), 1, False))
    def bind(_):
        raise OSError("synthetic occupied port")
    monkeypatch.setattr(launcher.socket, "socket", lambda *a: SimpleNamespace(setsockopt=lambda *a: None, bind=bind))
    monkeypatch.setattr(launcher, "notice", lambda _: pytest.fail("Unexpected error dialog"))
    with pytest.raises(SystemExit) as error:
        launcher.main()
    assert error.value.code == 1
    assert json.loads((home / "desktop-smoke.json").read_text("utf-8"))["error"] == "synthetic occupied port"
