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
    def __init__(self, profile=None, executable=None):
        self.profile, self.executable = profile, executable
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
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "PostMessageW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.api.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.api.EnumWindows.restype = wintypes.BOOL

    def owned_processes(self):
        if not getattr(self, "profile", None):
            return None
        import psutil
        owned = set()
        profile = os.path.normcase(os.path.abspath(str(self.profile)))
        executable = os.path.normcase(os.path.abspath(str(self.executable)))
        for process in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                info = process.info
                if os.path.normcase(os.path.abspath(info.get("exe") or "")) != executable:
                    continue
                argv = info.get("cmdline") or []
                if any(arg.startswith("--user-data-dir=") and os.path.normcase(os.path.abspath(arg.split("=", 1)[1])) == profile for arg in argv):
                    owned.add(info["pid"])
                    owned.update(child.pid for child in process.children(recursive=True))
            except (psutil.Error, OSError):
                continue
        return owned

    def belongs_to(self, handle, processes):
        if processes is None:
            return True
        pid = wintypes.DWORD()
        self.api.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        return pid.value in processes

    def valid(self, handle, processes=None):
        # Properties disappear when a window closes, even if Windows reuses its handle.
        if processes is None:
            processes = self.owned_processes()
        return bool(handle and self.api.IsWindow(handle) and self.api.GetPropW(handle, WINDOW_PROPERTY) and self.belongs_to(handle, processes))

    def find(self, *, register=False):
        found = []
        processes = self.owned_processes()

        def visit(handle, _):
            if self.valid(handle, processes):
                found.append(handle)
                return False
            if register and self.api.IsWindowVisible(handle):
                title, kind = ctypes.create_unicode_buffer(512), ctypes.create_unicode_buffer(128)
                self.api.GetWindowTextW(handle, title, len(title))
                self.api.GetClassNameW(handle, kind, len(kind))
                # Profile ownership handles browser title suffixes safely.
                # Legacy registration still requires the exact application title.
                if kind.value == "Chrome_WidgetWin_1" and self.belongs_to(handle, processes) and (processes is not None or title.value == WINDOW_TITLE):
                    if self.api.SetPropW(handle, WINDOW_PROPERTY, 2 if processes is not None else 1):
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

    def close(self, handle):
        # Only windows of our isolated browser profile may be closed, never a shared browser.
        if getattr(self, "profile", None) and self.valid(handle):
            return bool(self.api.PostMessageW(handle, 0x0010, 0, 0))  # WM_CLOSE
        return False


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
    def __init__(self, native, executable, launch=None, clock=time.monotonic, profile=None):
        self.native, self.executable = native, executable
        self.launch = launch or subprocess.Popen
        self.clock = clock
        self.handle = None
        self.pending_until = 0
        self.lock = threading.Lock()
        self.profile = profile

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
            argv = [str(self.executable)]
            if self.profile:
                self.profile.mkdir(parents=True, exist_ok=True)
                argv.extend(["--user-data-dir=" + str(self.profile), "--no-first-run", "--no-default-browser-check"])
            argv.append("--app=" + url)
            self.launch(argv,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.pending_until = self.clock() + (15 if self.profile else .75)

    def close(self):
        with self.lock:
            # Multiple windows accidentally opened in our profile also close normally.
            for _ in range(16):
                handle = self.native.find(register=True)
                if not handle:
                    self.handle, self.pending_until = None, 0
                    return True
                if not self.native.close(handle):
                    return False
                deadline = time.monotonic() + 3
                while self.native.valid(handle) and time.monotonic() < deadline:
                    time.sleep(.05)
                if self.native.valid(handle):
                    return False
            return False


_window = None
_initialization_lock = threading.Lock()
_profile_home = None


def configure_window(home):
    global _profile_home
    _profile_home = Path(home) / "browser-window"


def retire_legacy_registered_windows():
    """One-time transition: close only app windows explicitly marked by old versions."""
    if os.name != "nt":
        return
    native = WindowsWindow()
    def visit(handle, _):
        if native.api.GetPropW(handle, WINDOW_PROPERTY) == 1:
            title, kind = ctypes.create_unicode_buffer(512), ctypes.create_unicode_buffer(128)
            native.api.GetWindowTextW(handle, title, len(title))
            native.api.GetClassNameW(handle, kind, len(kind))
            if title.value == WINDOW_TITLE and kind.value == "Chrome_WidgetWin_1":
                native.api.PostMessageW(handle, 0x0010, 0, 0)
        return True
    native.api.EnumWindows(native.callback_type(visit), 0)


def controller():
    global _window
    if os.name != "nt":
        return None
    with _initialization_lock:
        if _window is None and (executable := app_browser()):
            _window = ArchiveWindow(WindowsWindow(_profile_home, executable), executable, profile=_profile_home)
        return _window


def register_window():
    """Called once by the authenticated page after its app-window title is ready."""
    try:
        if window := controller():
            return window.register()
    except OSError:
        pass
    return False


def close_browser():
    return _window.close() if _window is not None else True


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
