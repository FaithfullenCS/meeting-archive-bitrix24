"""Detect and install the signed Microsoft Evergreen Runtime, without elevation."""
import json
import ctypes
from pathlib import Path
import re
import subprocess

RUNTIME_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
BOOTSTRAPPER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def execution_level(path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "LoadLibraryExW": ([ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint], ctypes.c_void_p),
        "FindResourceW": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
        "SizeofResource": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_uint),
        "LoadResource": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
        "LockResource": ([ctypes.c_void_p], ctypes.c_void_p),
        "FreeLibrary": ([ctypes.c_void_p], ctypes.c_int),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    library = kernel.LoadLibraryExW(str(Path(path).resolve()), None, 0x22)
    if not library:
        raise ValueError("Не удалось проверить режим запуска установщика")
    try:
        resource = kernel.FindResourceW(library, 1, 24)  # RT_MANIFEST, read as data only.
        if not resource:
            raise ValueError("В установщике отсутствует манифест режима запуска")
        pointer = kernel.LockResource(kernel.LoadResource(library, resource))
        if not pointer:
            raise ValueError("Не удалось прочитать манифест установщика")
        xml = ctypes.string_at(pointer, kernel.SizeofResource(library, resource)).decode("utf-8")
        match = re.search(r'requestedExecutionLevel[^>]*level=["\x27]([^"\x27]+)', xml)
        return match.group(1) if match else ""
    finally:
        kernel.FreeLibrary(library)


def runtime_available():
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\EdgeUpdate\Clients" + "\\" + RUNTIME_ID,
                                    0, winreg.KEY_READ | view) as key:
                    version = winreg.QueryValueEx(key, "pv")[0]
                    if version and version != "0.0.0.0":
                        return True
            except OSError:
                continue
    return False


def verify_bootstrapper(path):
    script = "$ErrorActionPreference='Stop'; [Console]::InputEncoding=[Text.UTF8Encoding]::new(); [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Import-Module ($env:SystemRoot+'\\System32\\WindowsPowerShell\\v1.0\\Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1'); $p=[Console]::In.ReadToEnd(); $s=Get-AuthenticodeSignature -LiteralPath $p; @{status=[string]$s.Status;subject=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                input=str(Path(path).resolve()), text=True, encoding="utf-8", capture_output=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        signature = json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise ValueError("Windows не позволила проверить установщик WebView2. Повторите запуск; на управляемом компьютере проверьте ограничения установки.") from exc
    if signature.get("status") != "Valid" or "O=Microsoft Corporation" not in (signature.get("subject") or ""):
        raise ValueError("Не удалось проверить подпись установщика WebView2 Microsoft")
    if execution_level(path) != "asInvoker":
        raise ValueError("Установщик WebView2 требует повышенных прав; запуск запрещён")


def ensure_runtime(status=lambda text: None):
    if runtime_available():
        return
    path = Path(__file__).parent / "resources/MicrosoftEdgeWebview2Setup.exe"
    verify_bootstrapper(path)
    status("Подготовка окна Meeting Archive: установка WebView2 Microsoft…")
    try:
        subprocess.run([str(path), "/silent", "/install"], timeout=180, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    except (subprocess.SubprocessError, OSError) as exc:
        raise ValueError("Не удалось установить WebView2. Проверьте интернет и разрешение установки, затем повторите запуск.") from exc
    if not runtime_available():
        raise ValueError("WebView2 не обнаружен после установки. Повторите запуск или обратитесь к администратору Windows.")


def prepare_runtime(*, allow_retry=True):
    if runtime_available():
        return
    import threading
    import tkinter as tk
    from tkinter import messagebox, ttk
    while True:
        root = tk.Tk()
        root.title("Meeting Archive")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        ttk.Label(root, text="Подготовка окна приложения…\nУстановка компонента Microsoft WebView2.", padding=24).pack()
        progress = ttk.Progressbar(root, mode="indeterminate", length=350)
        progress.pack(padx=24, pady=(0, 24))
        progress.start()
        result = []
        def install():
            try:
                ensure_runtime()
                result.append(None)
            except Exception as exc:
                result.append(str(exc))
        threading.Thread(target=install, daemon=True).start()
        def poll():
            if result:
                root.quit()
            else:
                root.after(100, poll)
        root.after(100, poll)
        root.mainloop()
        root.withdraw()
        if result[0] is None:
            root.destroy()
            return
        if not allow_retry:
            root.destroy()
            raise ValueError(result[0])
        retry = messagebox.askretrycancel("Meeting Archive", result[0], parent=root)
        root.destroy()
        if not retry:
            raise ValueError("Подготовка окна приложения отменена")


def cleanup_legacy_profile(home):
    """Delete only the old app-owned profile, after readiness and when not in use."""
    import os
    import psutil
    import shutil
    home = Path(home).resolve()
    profile = home / "browser-window"
    if not profile.is_dir() or profile.is_symlink() or profile.is_junction():
        return
    expected = os.path.normcase(str(profile))
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            if (process.info.get("name") or "").lower() in {"chrome.exe", "msedge.exe", "browser.exe"} and process.info.get("cmdline") is None:
                return
            if any(arg.startswith("--user-data-dir=") and os.path.normcase(os.path.abspath(arg.split("=", 1)[1])) == expected
                   for arg in (process.info.get("cmdline") or [])):
                return
        except psutil.Error:
            return
    if any(p.is_symlink() or p.is_junction() for p in profile.rglob("*")):
        return
    try:
        shutil.rmtree(profile)
    except OSError:
        pass
