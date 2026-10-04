"""Defer tray activation to the next native message, after its popup menu closes."""
import ctypes
from ctypes import wintypes

WM_OPEN_ARCHIVE = 0x8000 + 101


def bind_open_action(icon, callback, *, post=None):
    # pystray 0.19.5 uses this Win32 message map (dependency is pinned).
    if post is None:
        post = ctypes.WinDLL("user32", use_last_error=True).PostMessageW
        post.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        post.restype = wintypes.BOOL
    pending = False

    def activate(_wparam, _lparam):
        nonlocal pending
        pending = False
        callback()

    def request():
        nonlocal pending
        if pending:
            return
        pending = True
        # Microsoft's notification-menu pattern posts WM_NULL after tracking
        # the menu. Our actual activation follows it on the tray message loop.
        post(icon._menu_hwnd, 0, 0, 0)
        if not post(icon._hwnd, WM_OPEN_ARCHIVE, 0, 0):
            pending = False
            callback()

    icon._message_handlers[WM_OPEN_ARCHIVE] = activate
    icon.open_archive = request
