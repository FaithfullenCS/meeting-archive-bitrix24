"""CI acceptance for DLLs carrying Windows' Internet-download mark."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mark-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from meeting_archive.desktop_runtime import CLR_DLLS
    distribution = root / "dist" / "MeetingArchive"
    exe = distribution / "MeetingArchive.exe"
    assert exe.is_file(), "Build the Windows executable first"
    paths = [distribution / name for name in CLR_DLLS]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in [exe, *paths]}
    for path in paths:
        Path(str(path) + ":Zone.Identifier").write_text("[ZoneTransfer]\nZoneId=3\n", encoding="ascii")
    print("Marked only the four packaged desktop DLLs as downloaded from the Internet")
    if args.mark_only:
        return
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "DOTNET", "VIRTUAL_ENV"))}
    windows = Path(os.environ["SystemRoot"])
    # The self-test verifies Microsoft's bootstrapper with the system PowerShell.
    # Keep Windows tools available, while excluding all external Python paths.
    env["PATH"] = os.pathsep.join(map(str, (windows / "System32", windows,
                                           windows / "System32/WindowsPowerShell/v1.0")))
    code = ('import sys, clr_loader; runtime=clr_loader.get_netfx(); '
            'runtime.get_assembly(sys.argv[1]).get_function("Python.Runtime.Loader.Initialize")')
    blocked = subprocess.run([sys.executable, "-I", "-c", code, str(paths[0])], env=env,
                             capture_output=True, text=True, timeout=15)
    assert blocked.returncode != 0 and "Failed to resolve Python.Runtime.Loader.Initialize" in blocked.stderr
    print("Reproduced the exact .NET loader failure on the marked packaged Python.Runtime.dll")
    subprocess.run([str(exe), "--self-test"], env=env, check=True, timeout=45,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    assert all(not Path(str(path) + ":Zone.Identifier").exists() for path in paths)
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in before.items())
    print("Packaged CLR/WebView imports passed with no Python on PATH; all DLL/EXE bytes unchanged")


if __name__ == "__main__":
    main()
