"""Regression for the existing Windows Run command, including exit before opening."""
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from meeting_archive import browser, launcher


@pytest.mark.parametrize("action", ["open", "exit"])
def test_windows_run_starts_only_tray_and_creates_window_on_request(tmp_path, monkeypatch, action):
    import uvicorn
    import pystray
    from meeting_archive import app, notifications, service, tray, webview_runtime

    calls = []
    monkeypatch.setattr(sys, "argv", ["archive", "--home", str(tmp_path), "--no-browser"])
    monkeypatch.setattr(browser, "_window", None)
    monkeypatch.setattr(launcher, "single_instance", lambda _: (
        SimpleNamespace(CloseHandle=lambda _: None), 1, False))
    listener = SimpleNamespace(setsockopt=lambda *a: None, bind=lambda *a: None,
                               listen=lambda *a: None, close=lambda: None)
    monkeypatch.setattr(launcher.socket, "socket", lambda *a: listener)
    monkeypatch.setattr(service, "Service", lambda _: SimpleNamespace(
        updates=SimpleNamespace(), notifications=SimpleNamespace()))
    monkeypatch.setattr(notifications, "register_windows", lambda _: None)
    monkeypatch.setattr(app, "create_app", lambda *a, **k: SimpleNamespace(
        state=SimpleNamespace(launch_token="synthetic")))
    monkeypatch.setattr(launcher, "notice", lambda message: pytest.fail(message))

    class Server:
        started = False
        should_exit = False

        def __init__(self, config):
            pass

        def run(self, **kwargs):
            self.started = True
            while not self.should_exit:
                time.sleep(.01)

    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: SimpleNamespace())
    tray_finished = threading.Event()

    class Icon:
        def __init__(self, *a, menu, **kwargs):
            self.menu = menu

        def run(self):
            try:
                # Before a user action no window/runtime preparation took place.
                assert browser._window.window is None
                assert not browser._window.open_requested.is_set()
                assert calls == []
                calls.append("tray-only")
                if action == "open":
                    self.open_archive()
                    self.open_archive()
                else:
                    list(self.menu.items)[-1](self)
            finally:
                tray_finished.set()

        def stop(self):
            pass

    monkeypatch.setattr(pystray, "Icon", Icon)
    monkeypatch.setattr(tray, "bind_open_action", lambda icon, callback: setattr(icon, "open_archive", callback))

    def prepare(**kwargs):
        assert browser._window.open_requested.is_set()
        calls.append("runtime")

    def run(window, url, **kwargs):
        calls.append("window")
        assert json.loads((tmp_path / "runtime.json").read_text("utf-8"))["desktop_required"]
        assert window.pending_open

    monkeypatch.setattr(webview_runtime, "prepare_runtime", prepare)
    monkeypatch.setattr(browser.ArchiveWindow, "run", run)
    launcher.main()
    assert tray_finished.wait(1)
    assert calls == (["tray-only", "runtime", "window"] if action == "open" else ["tray-only"])
    assert not (tmp_path / "runtime.json").exists()
