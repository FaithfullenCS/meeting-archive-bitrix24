"""Prepare only the shipped .NET assemblies marked as Internet downloads."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys


CLR_DLLS = (
    "_internal/pythonnet/runtime/Python.Runtime.dll",
    "_internal/webview/lib/Microsoft.Web.WebView2.Core.dll",
    "_internal/webview/lib/Microsoft.Web.WebView2.WinForms.dll",
    "_internal/webview/lib/WebBrowserInterop.x64.dll",
)
_REINSTALL = ("Комплект библиотек окна Meeting Archive неполный или изменён. "
              "Скачайте официальный ZIP и заново распакуйте весь комплект в локальную папку.")


def unblock_verified_clr(root: Path) -> list[str]:
    """Remove Zone.Identifier after checking every known DLL against the bundle manifest.

    The manifest detects incomplete/changed extractions; it is not a digital signature.
    No recursive unblocking, profile writes, registry changes or remote-load policy changes.
    """
    root = Path(root).absolute()
    paths = [root / name for name in CLR_DLLS]
    try:
        for path in paths:
            parents = (path, *path.parents)
            if (not path.is_file() or any(p.is_symlink() or p.is_junction() for p in parents
                                         if p == root or p.is_relative_to(root))):
                raise ValueError(_REINSTALL)
        marked = [(name, Path(str(path) + ":Zone.Identifier")) for name, path in zip(CLR_DLLS, paths)
                  if Path(str(path) + ":Zone.Identifier").exists()]
        if not marked:
            return []
        data = json.loads((root / "update-manifest.json").read_text("utf-8"))
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, dict):
            raise ValueError(_REINSTALL)
        # Check all DLL bytes before removing even one mark.
        for name, path in zip(CLR_DLLS, paths):
            expected = files.get(name)
            if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
                raise ValueError(_REINSTALL)
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != expected:
                raise ValueError(_REINSTALL)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(_REINSTALL) from exc
    try:
        for _, stream in marked:
            stream.unlink()
    except OSError as exc:
        raise ValueError("Windows не позволила снять блокировку библиотек Meeting Archive. "
                         "Откройте свойства исходного ZIP, отметьте «Разблокировать», нажмите «Применить» "
                         "и заново распакуйте комплект в доступную для записи локальную папку.") from exc
    return [name for name, _ in marked]


def prepare_clr_runtime() -> None:
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        unblock_verified_clr(Path(sys.executable).parent)
