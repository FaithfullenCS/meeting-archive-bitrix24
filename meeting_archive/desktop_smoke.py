"""Explicit packaged-window acceptance on an isolated synthetic profile only."""
from pathlib import Path
import subprocess
import sys
import time

from .settings import atomic_json


def exercise(window, home, url, shutdown, tray):
    report = {"checks": [], "ok": False}
    def wait(predicate, label, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                report["checks"].append(label)
                return
            time.sleep(.1)
        raise AssertionError(label)
    try:
        wait(lambda: window.ready, "native-window-and-frontend-ready", 40)
        native = window.window.native
        handle = int(native.Handle.ToInt64())
        wait(lambda: tray is not None and tray.visible, "archive-tray-visible")
        assert window.window.evaluate_js("document.title") == "Meeting Archive"
        assert window.window.evaluate_js("Boolean(document.querySelector('#settings-form'))")
        report["checks"].append("real-interface-loaded")
        # Destroy routes through the actual native FormClosing handler. The
        # ordinary close is cancelled and hides the same live WinForms window.
        window.window.destroy()
        wait(lambda: not native.Visible and not window.closed.is_set(), "cross-hides-without-exit")
        import httpx
        from urllib.parse import parse_qs, urlsplit
        token = parse_qs(urlsplit(url).query)["launch"][0]
        with httpx.Client(trust_env=False, follow_redirects=True) as client:
            for _ in range(5):
                client.post("http://127.0.0.1:8765/api/desktop/open", headers={"x-desktop-token": token}).raise_for_status()
            wait(lambda: native.Visible, "repeated-open-restores")
            native_count = window.window.evaluate_js("Boolean(window.pywebview)")
            assert native_count and int(native.Handle.ToInt64()) == handle
            report["checks"].append("same-native-handle")
            window.window.minimize()
            wait(lambda: window.minimized, "minimized")
            tray.open_archive()
            wait(lambda: not window.minimized and native.Visible, "tray-restores-window")
            command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "meeting_archive.launcher"]
            subprocess.run(command + ["--home", str(home), "--activate", "meetingarchive:settings"],
                           creationflags=subprocess.CREATE_NO_WINDOW, check=True, timeout=25)
            wait(lambda: window.window.evaluate_js("!document.querySelector('#page-settings').hidden"), "notification-activation-settings")
            assert int(native.Handle.ToInt64()) == handle
            client.get(url).raise_for_status()
            boot = client.get("http://localhost:8765/api/bootstrap").json()
            assert "csrf" in boot
        report["ok"] = True
        time.sleep(5)  # Let an update helper independently observe GUI readiness.
    except Exception as exc:
        report["error"] = str(exc)
        report["loaded"] = window.loaded
        report["frontend_ready"] = window.frontend_ready
        report["shown"] = window.shown.is_set()
    finally:
        atomic_json(Path(home) / "desktop-smoke.json", report)
        shutdown()
