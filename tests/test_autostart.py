"""Regression for the existing Windows Run command, including exit before opening."""
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from meeting_archive import browser, launcher


@pytest.mark.parametrize("action", ["open", "exit"])
@pytest.mark.parametrize("startup_delay", [0, 30, 120])
def test_windows_run_starts_only_tray_and_creates_window_on_request(tmp_path, monkeypatch, action, startup_delay):
    import uvicorn
    import pystray
    from meeting_archive import app, notifications, service, tray, webview_runtime

    calls = []
    clock = SimpleNamespace(elapsed=0)
    startup_due = threading.Event()
    startup_finished = threading.Event()

    def sleep(seconds):
        clock.elapsed += seconds
        if clock.elapsed >= startup_delay:
            startup_due.set()
            assert startup_finished.wait(1)

    monkeypatch.setattr(launcher, "time", SimpleNamespace(monotonic=lambda: clock.elapsed, sleep=sleep))
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
            while not startup_due.wait(.01):
                if self.should_exit:
                    return
            self.started = True
            startup_finished.set()
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


@pytest.mark.parametrize("failure", ["timeout", "thread_exception", "lifespan", "early_exit"])
def test_failed_server_start_is_bounded_and_records_redacted_cause(tmp_path, monkeypatch, vault, failure):
    import uvicorn
    from meeting_archive import app, service
    from meeting_archive.settings import Vault

    monkeypatch.setattr(vault, "redact", lambda value: Vault.redact(vault, value))

    messages, closed = [], []
    clock = SimpleNamespace(elapsed=0)
    running = threading.Event()

    def sleep(seconds):
        assert running.wait(1)
        clock.elapsed += seconds
        time.sleep(.0001)  # Let an exiting server thread finish; no real 180-second delay.

    monkeypatch.setattr(launcher, "time", SimpleNamespace(monotonic=lambda: clock.elapsed, sleep=sleep))
    monkeypatch.setattr(sys, "argv", ["archive", "--home", str(tmp_path), "--no-browser", "--no-tray"])
    monkeypatch.setattr(launcher, "single_instance", lambda _: (
        SimpleNamespace(CloseHandle=lambda _: closed.append("mutex")), 1, False))
    monkeypatch.setattr(launcher.socket, "socket", lambda *a: SimpleNamespace(
        setsockopt=lambda *a: None, bind=lambda *a: None, listen=lambda *a: None,
        close=lambda: closed.append("socket")))
    monkeypatch.setattr(service, "Service", lambda _: SimpleNamespace(
        updates=SimpleNamespace(), notifications=SimpleNamespace(), vault=vault,
        recovery_status={"running": True, "found": 12, "restored": 2, "errors": 0}))
    detail = "ValueError: synthetic-token https://private.test/secret" if failure == "lifespan" else ""
    monkeypatch.setattr(app, "create_app", lambda *a, **k: SimpleNamespace(
        state=SimpleNamespace(launch_token="synthetic", startup_error=detail)))
    monkeypatch.setattr(launcher, "notice", messages.append)

    class Server:
        started = False
        should_exit = False

        def __init__(self, config):
            pass

        def run(self, **kwargs):
            running.set()
            if failure == "thread_exception":
                raise OSError("synthetic-token https://private.test/secret")
            if failure == "timeout":
                while not self.should_exit:
                    time.sleep(.001)

    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: SimpleNamespace())
    with pytest.raises(SystemExit) as error:
        launcher.main()
    assert error.value.code == 1
    assert len(messages) == 1 and "Локальный интерфейс не запустился" in messages[0]
    report_text = (tmp_path / "startup-error.json").read_text("utf-8")
    report = json.loads(report_text)
    assert "synthetic-token" not in report_text + messages[0]
    assert "private.test" not in report_text + messages[0]
    assert report["stage"] == "start_local_server"
    assert report["recovery"]["found"] == 12
    if failure == "timeout":
        assert 180 <= clock.elapsed < 181
        assert "180" in report["error"]
    elif failure in {"thread_exception", "lifespan"}:
        assert ("OSError" if failure == "thread_exception" else "ValueError") in report["error"]
        assert clock.elapsed < 180
    else:
        assert "завершился до готовности" in report["error"]
        assert clock.elapsed < 180
    assert closed == ["socket", "mutex"]
    assert not (tmp_path / "runtime.json").exists()


async def test_lifespan_exposes_startup_error_when_uvicorn_handles_exception(tmp_path, monkeypatch, vault):
    from meeting_archive.app import create_app
    from meeting_archive.service import Service

    service = Service(tmp_path, vault=vault)

    async def fail_start():
        raise ValueError("synthetic-token https://private.test/secret")

    monkeypatch.setattr(service, "start", fail_start)
    try:
        app = create_app(service)
        with pytest.raises(ValueError):
            async with app.router.lifespan_context(app):
                pytest.fail("A failed startup must not serve requests")
        assert "ValueError" in app.state.startup_error
        assert "synthetic-token" not in app.state.startup_error
    finally:
        await service.stop()


def test_startup_diagnostic_write_failure_preserves_original_error(tmp_path, monkeypatch, vault):
    def unavailable(*args):
        raise PermissionError("diagnostic unavailable")

    monkeypatch.setattr(launcher, "atomic_json", unavailable)
    message = launcher.record_startup_failure(tmp_path, ValueError("synthetic-token"), 2, "start_local_server",
                                              SimpleNamespace(vault=vault))
    assert message == "[hidden]"


def test_repeated_launch_waits_for_local_server_without_proxy(tmp_path, monkeypatch):
    import httpx
    (tmp_path / "runtime.json").write_text(json.dumps({"url": "http://localhost:8765/?launch=synthetic"}))
    monkeypatch.setattr(sys, "argv", ["archive", "--home", str(tmp_path)])
    kernel = SimpleNamespace(CloseHandle=lambda _: None)
    monkeypatch.setattr(launcher, "single_instance", lambda _: (kernel, 1, True))
    clock = SimpleNamespace(elapsed=0)
    monkeypatch.setattr(launcher, "time", SimpleNamespace(monotonic=lambda: clock.elapsed,
        sleep=lambda seconds: setattr(clock, "elapsed", clock.elapsed + seconds)))
    attempts = []
    def post(url, **kwargs):
        attempts.append((url, kwargs))
        if len(attempts) == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(launcher, "notice", lambda message: pytest.fail(message))
    launcher.main()
    assert len(attempts) == 2
    assert all(url.startswith("http://127.0.0.1:8765/") and kwargs.get("trust_env") is False for url, kwargs in attempts)
    assert not (tmp_path / "startup-error.json").exists()


def test_repeated_launch_takes_over_only_after_old_mutex_is_released(tmp_path, monkeypatch):
    from meeting_archive import webview_runtime
    monkeypatch.setattr(sys, "argv", ["archive", "--home", str(tmp_path)])
    kernel = SimpleNamespace(CloseHandle=lambda _: None)
    claims, messages = [], []
    def claim(_):
        claims.append(1)
        return kernel, len(claims), len(claims) == 1
    monkeypatch.setattr(launcher, "single_instance", claim)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    def prepared(**kwargs):
        assert len(claims) == 2
        raise RuntimeError("synthetic reached new server preparation")
    monkeypatch.setattr(webview_runtime, "prepare_runtime", prepared)
    monkeypatch.setattr(launcher, "notice", messages.append)
    with pytest.raises(SystemExit):
        launcher.main()
    assert len(messages) == 1 and "synthetic reached new server preparation" in messages[0]


def test_repeated_launch_has_bounded_wait_and_keeps_existing_runtime(tmp_path, monkeypatch):
    import httpx
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"url": "http://localhost:8765/?launch=synthetic"}))
    monkeypatch.setattr(sys, "argv", ["archive", "--home", str(tmp_path)])
    kernel = SimpleNamespace(CloseHandle=lambda _: None)
    monkeypatch.setattr(launcher, "single_instance", lambda _: (kernel, 1, True))
    clock = SimpleNamespace(elapsed=0)
    monkeypatch.setattr(launcher, "time", SimpleNamespace(monotonic=lambda: clock.elapsed,
        sleep=lambda seconds: setattr(clock, "elapsed", clock.elapsed + seconds)))
    def refused(*args, **kwargs):
        raise httpx.ConnectError("private-network-details")
    monkeypatch.setattr(httpx, "post", refused)
    messages = []
    monkeypatch.setattr(launcher, "notice", messages.append)
    with pytest.raises(SystemExit):
        launcher.main()
    assert 180 <= clock.elapsed < 181
    assert runtime.exists()
    assert "180" in messages[0] and "private-network-details" not in messages[0]
