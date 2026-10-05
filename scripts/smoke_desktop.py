"""Launch the actual packaged WebView2/tray on a fresh synthetic profile."""
import json
from pathlib import Path
import subprocess
import time
import uuid


def main():
    root = Path(__file__).resolve().parents[1]
    home = root / ".work" / ("desktop-smoke-" + uuid.uuid4().hex)
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"auto_update": False, "autostart": False,
        "archive_root": str(home / "archive"), "watch_folder": str(home / "incoming")}), encoding="utf-8")
    exe = root / "dist/MeetingArchive/MeetingArchive.exe"
    process = subprocess.Popen([str(exe), "--home", str(home), "--desktop-smoke"],
                               creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + 100
    report_file = home / "desktop-smoke.json"
    while time.monotonic() < deadline and not report_file.exists() and process.poll() is None:
        time.sleep(.2)
    assert report_file.exists(), f"Desktop acceptance did not return a result; profile {home}"
    report = json.loads(report_file.read_text("utf-8"))
    process.wait(timeout=30)
    assert report["ok"], report
    assert not (home / "runtime.json").exists(), "Application did not exit"
    print(json.dumps(report, ensure_ascii=False))
    print("Native packaged desktop acceptance passed; synthetic profile:", home)


if __name__ == "__main__":
    main()
