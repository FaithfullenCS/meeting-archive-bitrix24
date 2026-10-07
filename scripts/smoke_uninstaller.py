"""Native uninstaller preview acceptance in Windows CI, without real profiles."""
from pathlib import Path
import json
import os
import subprocess


def main():
    if os.name != "nt" or os.environ.get("CI", "").lower() != "true":
        raise SystemExit("Native uninstaller smoke runs only in Windows CI")
    root = Path(__file__).resolve().parents[1]
    application = root / "dist/MeetingArchive"
    home = root / ".work/uninstaller-gui-profile"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".meeting-archive-profile.json").write_text(json.dumps({"product": "MeetingArchive", "schemaVersion": 1}))
    (home / "settings.json").write_text(json.dumps({"archive_root": str(home.parent / "synthetic-meetings"), "chat_archive_root": str(home.parent / "synthetic-chats")}))
    result = home.parent / "uninstaller-gui-result.json"
    subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(application / "scripts/reset-profile.ps1"),
                    "-ProfileHome", str(home), "-ApplicationRoot", str(application), "-Gui", "-UiSmoke", "-ReportPath", str(result)], check=True, timeout=90)
    proof = json.loads(result.read_text("utf-8-sig"))
    assert proof["smokePassed"] and proof["previewOnly"] and proof["defaultArchivesPreserved"]
    assert (home / "settings.json").exists() and (application / "MeetingArchive.exe").exists()
    print("Native uninstaller preview passed; program and synthetic profile preserved")


if __name__ == "__main__":
    main()
