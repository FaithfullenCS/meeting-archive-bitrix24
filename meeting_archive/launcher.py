from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import sys
import threading
import time
from .browser import open_browser
from pathlib import Path

from .settings import app_home, atomic_json


def self_test():
    from .modules import ModuleManager
    package = Path(__file__).parent
    required = ["static/index.html", "static/app.js", "static/styles.css", "worker/entry.py",
                "static/icon.svg", "static/icon.png", "static/favicon.ico",
                "worker/core/transcriber.py", "worker/core/model_catalog.py", "worker/core/parakeet.py",
                "worker/core/gigaam.py", "worker/install_parakeet.py", "worker/install_gigaam.py",
                "resources/worker-cuda.lock", "resources/worker-cuda126.lock", "resources/worker-cpu.lock", "resources/worker-native.lock",
                "resources/worker-gigaam-addons.lock"]
    missing = [name for name in required if not (package / name).is_file()]
    if missing:
        raise RuntimeError("Missing packaged resources: " + ", ".join(missing))
    manager = ModuleManager(Path.cwd(), None)
    if not manager.uv().is_file():
        raise RuntimeError("Missing managed installer")
    import tkinter
    import pystray
    from PIL import Image
    assert tkinter and pystray and Image
    from winrt.windows.data.xml.dom import XmlDocument
    from winrt.windows.ui.notifications import ToastNotification, ToastNotificationManager, ToastNotifier
    from .notifications import APP_ID
    assert XmlDocument and ToastNotification
    assert isinstance(ToastNotificationManager.create_toast_notifier_with_id(APP_ID), ToastNotifier)
    if sys.stdout:
        print("Meeting Archive resource/import self-test passed")


def notice(message: str):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "Meeting Archive", 0x10)
    elif sys.stderr:
        print(message, file=sys.stderr)


def single_instance(home: Path):
    import hashlib
    name = "Local\\MeetingArchive-" + hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:24]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise OSError("Не удалось создать блокировку приложения")
    existing = ctypes.get_last_error() == 183
    return kernel, handle, existing


def main():
    parser = argparse.ArgumentParser(description="Meeting Archive")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--activate")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    home = (args.home or app_home()).resolve()
    if args.activate:
        from .notifications import parse_activation
        parse_activation(args.activate)
    home.mkdir(parents=True, exist_ok=True)
    kernel, mutex, existing = single_instance(home)
    runtime_file = home / "runtime.json"
    try:
        if existing:
            if runtime_file.exists() and (not args.no_browser or args.activate):
                runtime = json.loads(runtime_file.read_text("utf-8"))
                url = runtime.get("url", "")
                if url.startswith(("http://127.0.0.1:8765/?launch=", "http://localhost:8765/?launch=")):
                    from urllib.parse import parse_qs, urlsplit
                    import httpx
                    token = parse_qs(urlsplit(url).query)["launch"][0]
                    if args.activate:
                        response = httpx.post("http://localhost:8765/api/desktop/activate",
                                              json={"uri": args.activate}, headers={"x-desktop-token": token}, timeout=10)
                        response.raise_for_status()
                    if not args.no_browser:
                        httpx.post("http://localhost:8765/api/desktop/open", json={},
                                   headers={"x-desktop-token": token}, timeout=20).raise_for_status()
            return
        # A repeated EXE launch only needs the mutex/runtime and our window.
        # Load the server stack after that fast path, not before activating it.
        import uvicorn
        from .app import create_app
        from .service import Service
        # Reserve the expected callback port; never take over another program's listener.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 8765))
        listener.listen(128)
        service = Service(home)
        from .browser import configure_window, close_browser, retire_legacy_registered_windows
        configure_window(home)
        from .notifications import register_windows
        try:
            if not args.no_tray:
                register_windows(home)
        except OSError as exc:
            service.notifications.error = str(exc)
        if args.activate:
            service.notifications.activate(args.activate)
        app = create_app(service)
        url = "http://localhost:8765/?launch=" + app.state.launch_token
        from . import __version__
        atomic_json(runtime_file, {"url": url, "pid": os.getpid(), "version": __version__})
        config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_config=None, access_log=False, log_level="critical")
        server = uvicorn.Server(config)
        tray_icon = None

        def exit_requested():
            if not close_browser():
                raise ValueError("Не удалось закрыть окно Meeting Archive. Закройте его и повторите действие.")
            server.should_exit = True
            if tray_icon:
                tray_icon.stop()
        service.updates.shutdown = exit_requested
        # A protected endpoint supports graceful headless QA; normal users exit through the tray.
        app = create_app(service, app.state.launch_token, shutdown_callback=exit_requested)
        config.app = app
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), name="MeetingArchiveServer", daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.05)
        if not server.started:
            raise RuntimeError("Локальный интерфейс не запустился")
        if not args.no_browser:
            retire_legacy_registered_windows()
            open_browser(url)
        if args.no_tray:
            try:
                while thread.is_alive():
                    thread.join(1)
            except KeyboardInterrupt:
                server.should_exit = True
        else:
            import pystray
            from PIL import Image
            icon_image = Image.open(Path(__file__).parent / "static/favicon.ico")

            def open_ui(icon, item):
                icon.open_archive()

            def pause(icon, item):
                def apply():
                    service.settings.paused = not service.settings.paused
                    service.settings.save(home)
                    icon.update_menu()
                service.event_loop.call_soon_threadsafe(apply)

            def exit_app(icon, item):
                exit_requested()

            icon = pystray.Icon("MeetingArchive", icon_image, "Meeting Archive", menu=pystray.Menu(
                pystray.MenuItem("Открыть архив", open_ui, default=True),
                pystray.MenuItem("Пауза автоматизации", pause, checked=lambda item: service.settings.paused),
                pystray.MenuItem("Выход", exit_app)))
            tray_icon = icon
            from .tray import bind_open_action
            bind_open_action(icon, lambda: open_browser(url))
            try:
                icon.run()
            finally:
                server.should_exit = True
        thread.join(timeout=20)
        listener.close()
    except Exception as exc:
        notice(str(exc))
        raise
    finally:
        if not existing:
            runtime_file.unlink(missing_ok=True)
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    main()
