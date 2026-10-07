"""Build the independent, lightweight Windows application (never install ASR deps)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

PUBLIC_DOCS = (
    "AI_ARCHIVE_GUIDE.md", "CHAT_ARCHIVE.md", "architecture.md", "bitrix24.md", "diarization.md",
    "gigaam.md", "parakeet.md", "reset-profile.md", "windows-hardware.md",
)

def source_hashes(root: Path) -> dict[str, str]:
    paths = [root / "pyproject.toml", root / "requirements.lock", root / "requirements-dev.lock"]
    paths.extend(path for path in (root / "meeting_archive").rglob("*")
                 if path.is_file() and path.suffix in {".py", ".js", ".css", ".html", ".lock", ".png", ".ico", ".svg", ".ps1"})
    paths.extend(path for path in (root / "skills").rglob("*")
                 if path.is_file() and path.suffix in {".md", ".yaml"})
    paths.extend([root / "uninstall.cmd", root / "scripts/reset-profile.ps1", root / "scripts/uninstall-ui.ps1"])
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def main() -> None:
    if sys.platform != "win32" or sys.maxsize < 2**32:
        raise SystemExit("Build on Windows x64 with Python 3.12.")
    if sys.version_info[:3] != (3, 12, 15):
        raise SystemExit("The release build requires the pinned Python 3.12.15 runtime; see README.")
    import uv

    root = Path(__file__).resolve().parents[1]
    package = root / "meeting_archive"
    for relative in ("launcher.py", "static/index.html", "static/app.js", "static/styles.css", "static/chat-archive.js", "static/chat-archive.css", "static/archive-controls.js", "static/archive-controls.css", "worker/entry.py",
                     "resources/worker-cpu.lock", "resources/worker-cuda.lock", "resources/worker-cuda126.lock"):
        if not (package / relative).is_file():
            raise SystemExit(f"Missing build input: {relative}")
    inputs = source_hashes(root)
    staging = root / "build" / "bundle"
    staging.mkdir(parents=True, exist_ok=True)
    for name in ("static", "worker", "resources"):
        destination = staging / name
        if destination.exists():
            # The exact resolved target is confined to this repository's build directory.
            if not destination.resolve().is_relative_to((root / "build").resolve()):
                raise SystemExit("Unsafe staging path.")
            shutil.rmtree(destination)
        shutil.copytree(package / name, destination,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.exe"))
    sys.path.insert(0, str(root))
    from meeting_archive.webview_runtime import BOOTSTRAPPER_URL, verify_bootstrapper
    import urllib.request
    bootstrapper = staging / "resources/MicrosoftEdgeWebview2Setup.exe"
    with urllib.request.urlopen(BOOTSTRAPPER_URL, timeout=60) as response:
        installer = response.read(10 * 1024 * 1024 + 1)
    if len(installer) > 10 * 1024 * 1024:
        raise SystemExit("Unexpected WebView2 bootstrapper size")
    bootstrapper.write_bytes(installer)
    verify_bootstrapper(bootstrapper)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--onedir", "--windowed", "--name", "MeetingArchive", "--icon", str(package / "static/favicon.ico"),
               "--paths", str(root), "--distpath", str(root / "dist"),
               "--workpath", str(root / "build" / "pyinstaller"),
               "--specpath", str(root / "build")]
    for name in ("static", "worker", "resources"):
        command.extend(["--add-data", f"{staging / name}{os.pathsep}meeting_archive/{name}"])
    command.extend(["--add-binary", f"{uv.find_uv_bin()}{os.pathsep}meeting_archive/resources"])
    for hidden in ("pystray._win32", "uvicorn.logging", "uvicorn.loops.asyncio",
                   "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on", "webview.platforms.winforms", "webview.platforms.edgechromium"):
        command.extend(["--hidden-import", hidden])
    for heavy in ("torch", "torchaudio", "pyannote", "faster_whisper", "ctranslate2",
                  "numpy", "scipy", "librosa", "noisereduce", "soundfile", "av"):
        command.extend(["--exclude-module", heavy])
    command.append(str(root / "scripts" / "windows_entry.py"))
    subprocess.run(command, cwd=root, check=True)
    if source_hashes(root) != inputs:
        raise SystemExit("Source files changed during the build. Build again from stable inputs.")
    distribution = root / "dist" / "MeetingArchive"
    from meeting_archive.desktop_runtime import CLR_DLLS
    for relative in CLR_DLLS:
        if not (distribution / relative).is_file():
            raise SystemExit(f"Packaged desktop DLL missing: {relative}")
    for name in ("Совещания", "Входящие записи"):
        (distribution / "Данные" / name).mkdir(parents=True, exist_ok=True)
    for relative in ("meeting_archive/resources/uv.exe", "meeting_archive/worker/entry.py",
                     "meeting_archive/static/index.html", "meeting_archive/static/app.js",
                     "meeting_archive/static/styles.css", "meeting_archive/static/chat-archive.js", "meeting_archive/static/chat-archive.css", "meeting_archive/static/archive-controls.js", "meeting_archive/static/archive-controls.css", "meeting_archive/resources/worker-cuda.lock",
                     "meeting_archive/resources/worker-cuda126.lock", "meeting_archive/resources/MicrosoftEdgeWebview2Setup.exe"):
        if not (distribution / "_internal" / relative).is_file():
            raise SystemExit(f"Packaged input missing: {relative}")
    for source in ("README.md", "skills"):
        if (root / source).is_dir():
            shutil.copytree(root / source, distribution / source, dirs_exist_ok=True)
        else:
            shutil.copy2(root / source, distribution / source)
    (distribution / "docs").mkdir(exist_ok=True)
    for name in PUBLIC_DOCS:
        shutil.copy2(root / "docs" / name, distribution / "docs" / name)
    (distribution / "scripts").mkdir(exist_ok=True)
    shutil.copy2(root / "scripts/reset-profile.ps1", distribution / "scripts/reset-profile.ps1")
    shutil.copy2(root / "scripts/uninstall-ui.ps1", distribution / "scripts/uninstall-ui.ps1")
    shutil.copy2(root / "uninstall.cmd", distribution / "uninstall.cmd")
    (distribution / "build-info.json").write_text(json.dumps(
        {"version": tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
         "python": sys.version, "source_sha256": inputs}, ensure_ascii=False, indent=2), encoding="utf-8")
    files = {p.relative_to(distribution).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in distribution.rglob("*") if p.is_file()
             and p.relative_to(distribution).parts[0] != "Данные" and p.name != "update-manifest.json"}
    (distribution / "update-manifest.json").write_text(json.dumps(
        {"version": tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"],
         "files": files}, ensure_ascii=False, indent=2), encoding="utf-8")
    zip_file = Path(shutil.make_archive(str(root / "dist" / "MeetingArchive-Windows-x64"),
                                       "zip", root / "dist", "MeetingArchive"))
    with zip_file.open("rb") as archive:
        digest = hashlib.file_digest(archive, "sha256").hexdigest()
    (zip_file.parent / (zip_file.name + ".sha256")).write_text(
        f"{digest}  {zip_file.name}\n", encoding="ascii")
    print(f"Windows build: {zip_file}\nSHA-256: {digest}")


if __name__ == "__main__":
    main()
