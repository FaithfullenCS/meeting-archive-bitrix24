"""One embedded WebView2 window; external links use the user's regular browser."""
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit


class ArchiveWindow:
    def __init__(self, home, *, on_ready=None, hide_on_close=True):
        self.home = Path(home)
        self.window = None
        self.shown = threading.Event()
        self.loaded = False
        self.frontend_ready = False
        self.closed = threading.Event()
        self.stopping = False
        self.pending_open = False
        self.open_requested = threading.Event()
        self.minimized = False
        self.hide_on_close = hide_on_close
        self.on_ready = on_ready or (lambda: None)
        self.lock = threading.RLock()

    @property
    def ready(self):
        return self.loaded and self.frontend_ready and not self.closed.is_set()

    def mark_ready(self):
        self.frontend_ready = True
        if self.ready:
            self.on_ready()
        return self.ready

    def page_loaded(self):
        try:
            page = urlsplit(self.window.get_current_url() or "")
            self.loaded = (page.scheme == "http" and page.hostname in {"localhost", "127.0.0.1"}
                           and page.port == 8765 and page.path == "/"
                           and bool(self.window.evaluate_js("Boolean(document.getElementById('settings-form'))")))
        except Exception:
            self.loaded = False
        if self.ready:
            self.on_ready()

    def window_shown(self):
        self.shown.set()
        if self.pending_open and not self.stopping:
            self.open()

    def closing(self):
        if self.stopping or not self.hide_on_close:
            return True
        self.window.hide()
        return False

    def open(self, url=None):
        if url:
            parsed = urlsplit(url)
            if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.port != 8765:
                raise ValueError("Archive window requires its local application address")
        with self.lock:
            if self.stopping:
                return
            self.open_requested.set()
            if not self.shown.is_set():
                self.pending_open = True
                return
            self.pending_open = False
        if self.minimized:
            self.window.restore()
        self.window.show()

    def close(self):
        with self.lock:
            self.stopping = True
            self.open_requested.set()  # Wake the launcher when exiting before first open.
            window = self.window
        if window and not self.closed.is_set():
            try:
                window.destroy()
                success = self.closed.wait(10)
            except Exception as exc:
                self.stopping = False
                raise ValueError("Не удалось закрыть окно Meeting Archive. Закройте открытые диалоги и повторите действие.") from exc
            if not success:
                self.stopping = False
            return success
        return True

    def run(self, url, *, started=None, webview=None):
        if webview is None:
            from .desktop_runtime import prepare_clr_runtime
            prepare_clr_runtime()
            import webview
        webview.settings.update(ALLOW_DOWNLOADS=True, ALLOW_FILE_URLS=False,
                                OPEN_EXTERNAL_LINKS_IN_BROWSER=True, OPEN_DEVTOOLS_IN_DEBUG=False)
        self.window = webview.create_window("Meeting Archive", url, width=1200, height=800,
                                           min_size=(800, 560), text_select=True)
        self.window.events.closing += self.closing
        self.window.events.closed += self.closed.set
        self.window.events.shown += self.window_shown
        self.window.events.loaded += self.page_loaded
        self.window.events.minimized += lambda: setattr(self, "minimized", True)
        self.window.events.restored += lambda: setattr(self, "minimized", False)

        def monitor():
            if started:
                started()
            deadline = time.monotonic() + 45
            while not self.ready and not self.closed.is_set() and time.monotonic() < deadline:
                time.sleep(.1)
            if not self.ready and not self.closed.is_set():
                self.stopping = True
                self.window.destroy()
        webview.start(monitor, gui="edgechromium", debug=False, private_mode=False,
                      storage_path=str(self.home / "webview-data"),
                      icon=str(Path(__file__).parent / "static/favicon.ico"))
        if not self.loaded or not self.frontend_ready:
            raise ValueError("Не удалось загрузить интерфейс Meeting Archive. Проверьте WebView2 и повторите запуск.")


_window = None


def configure_window(home, **kwargs):
    global _window
    _window = ArchiveWindow(home, **kwargs)
    return _window


def open_browser(url):
    if _window:
        _window.open(url)


def close_browser():
    return _window.close() if _window else True


def mark_ready():
    return _window.mark_ready() if _window else False
