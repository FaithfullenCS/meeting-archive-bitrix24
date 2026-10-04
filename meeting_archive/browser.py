"""Open our own browser app window, without inspecting or switching browser tabs."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit
import webbrowser

WINDOW_TITLE = "Meeting Archive"
WINDOW_PROPERTY = "MeetingArchive.DesktopWindow.8765"


class WindowsWindow:
    def __init__(self):
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        signatures = {
            "GetPropW": ([wintypes.HWND, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetPropW": ([wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE], wintypes.BOOL),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "ShowWindowAsync": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "SetForegroundWindow": ([wintypes.HWND], wintypes.BOOL),
            "FlashWindow": ([wintypes.HWND, wintypes.BOOL], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.api.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.api.EnumWindows.restype = wintypes.BOOL

    def valid(self, handle):
        # Properties disappear when a window closes, even if Windows reuses its handle.
        return bool(handle and self.api.IsWindow(handle) and self.api.GetPropW(handle, WINDOW_PROPERTY))

    def find(self, *, register=False):
        found = []

        def visit(handle, _):
            if self.valid(handle):
                found.append(handle)
                return False
            if register and self.api.IsWindowVisible(handle):
                title, kind = ctypes.create_unicode_buffer(512), ctypes.create_unicode_buffer(128)
                self.api.GetWindowTextW(handle, title, len(title))
                self.api.GetClassNameW(handle, kind, len(kind))
                # Normal browser windows append their browser name. Only our
                # exact app-window title is eligible; other windows never activate.
                if title.value == WINDOW_TITLE and kind.value == "Chrome_WidgetWin_1":
                    if self.api.SetPropW(handle, WINDOW_PROPERTY, 1):
                        found.append(handle)
                        return False
            return True

        self.api.EnumWindows(self.callback_type(visit), 0)
        return found[0] if found else None

    def activate(self, handle):
        if self.api.IsIconic(handle):
            self.api.ShowWindowAsync(handle, 9)  # SW_RESTORE; never sends keystrokes.
        if not self.api.SetForegroundWindow(handle):
            # Respect Windows foreground restrictions; indicate our taskbar window.
            self.api.FlashWindow(handle, True)


def default_app_browser() -> Path | None:
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice") as key:
            program = winreg.QueryValueEx(key, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, program + r"\shell\open\command") as key:
            command = os.path.expandvars(winreg.QueryValueEx(key, None)[0])
        quoted = re.match(r'^\s*"([^"]+)"', command)
        path = Path(quoted.group(1) if quoted else command.strip().split(" ", 1)[0])
        if path.name.lower() in {"msedge.exe", "chrome.exe", "browser.exe"} and path.is_file():
            return path
    except OSError:
        pass
    return None


def app_browser() -> Path | None:
    """Use an installed Chromium browser; never install one or change its profile."""
    # Prefer the user's existing default browser engine instead of starting a
    # second browser brand solely for this lightweight local interface.
    if preferred := default_app_browser():
        return preferred
    candidates = []
    for root in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")):
        if root:
            candidates.extend(Path(root) / name for name in (
                "Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe",
                "Yandex/YandexBrowser/Application/browser.exe"))
    return next((path for path in candidates if path.is_file()), None)


class ArchiveWindow:
    def __init__(self, native, executable, launch=None, clock=time.monotonic):
        self.native, self.executable = native, executable
        self.launch = launch or subprocess.Popen
        self.clock = clock
        self.handle = None
        self.pending_until = 0
        self.lock = threading.Lock()

    def register(self):
        with self.lock:
            self.handle = self.native.find(register=True)
            if self.handle:
                self.pending_until = 0
            return bool(self.handle)

    def open(self, url):
        with self.lock:
            if not self.native.valid(self.handle):
                self.handle = self.native.find(register=True)
            if self.handle:
                self.native.activate(self.handle)
                return
            if self.clock() < self.pending_until:
                return  # Rapid tray clicks during startup must not create several windows.
            self.launch([str(self.executable), "--app=" + url],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.pending_until = self.clock() + .75  # Debounce double-clicks, never silence tray actions for 15 seconds.


_window = None
_initialization_lock = threading.Lock()


def controller():
    global _window
    if os.name != "nt":
        return None
    with _initialization_lock:
        if _window is None and (executable := app_browser()):
            _window = ArchiveWindow(WindowsWindow(), executable)
        return _window


def register_window():
    """Called once by the authenticated page after its app-window title is ready."""
    try:
        if window := controller():
            return window.register()
    except OSError:
        pass
    return False


def open_browser(url: str):
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.port != 8765:
        raise ValueError("Archive window requires its local application address")
    try:
        if window := controller():
            window.open(url)
            return
    except OSError:
        pass
    # Without an installed app-window browser, the default browser owns tab
    # reuse. The fallback never searches tabs or simulates keyboard input.
    webbrowser.open(url, new=0, autoraise=True)
