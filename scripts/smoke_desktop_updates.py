"""Real GUI readiness gates installation; a failed page load rolls back safely."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import httpx


def main():
    root = Path(__file__).resolve().parents[1]
    qa = root / ".work" / ("desktop-smoke-upgrade-" + uuid.uuid4().hex)
    home, target, staged = qa / "desktop-smoke-profile", qa / "program", qa / "staged"
    home.mkdir(parents=True)
    shutil.copytree(root / "dist/MeetingArchive", target)
    shutil.copytree(root / "dist/MeetingArchive", staged)
    manifest = json.loads((target / "update-manifest.json").read_text("utf-8"))
    settings = home / "settings.json"
    settings.write_text(json.dumps({"auto_update": False, "autostart": False,
        "archive_root": str(home / "archive"), "watch_folder": str(home / "incoming")}), encoding="utf-8")
    sentinels = [target / "user-note.txt", target / "Данные/Совещания/voice.wav", home / "module/model.bin", settings]
    for p in sentinels[:-1]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"synthetic preservation sentinel")
    def hashes():
        return {str(p.relative_to(qa)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sentinels}
    old = subprocess.Popen([str(target / "MeetingArchive.exe"), "--home", str(home), "--no-browser", "--no-tray"],
                           creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + 30
    while not (home / "runtime.json").exists() and time.monotonic() < deadline:
        time.sleep(.1)
    runtime = json.loads((home / "runtime.json").read_text("utf-8"))
    with httpx.Client(trust_env=False, follow_redirects=True) as client:
        client.get(runtime["url"]).raise_for_status()
        boot = client.get("http://localhost:8765/api/bootstrap").json()
        client.post("http://localhost:8765/api/shutdown", headers={"x-csrf-token": boot["csrf"]}).raise_for_status()
    old.wait(timeout=30)
    sentinels.append(home / "secrets.dpapi")
    before = hashes()
    config = {"target": str(target), "staged": str(staged), "home": str(home), "pid": old.pid,
              "version": manifest["version"], "old_files": list(manifest["files"]),
              "new_files": list(manifest["files"]), "desktop_smoke": True}
    def install(fail=False):
        (home / "desktop-smoke.json").unlink(missing_ok=True)
        config_file = qa / "install.json"
        config_file.write_text(json.dumps({**config, "desktop_smoke_fail": fail}), encoding="utf-8")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
            "-File", str(root / "meeting_archive/resources/apply-update.ps1"), "-Config", str(config_file)],
            creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=280)
        receipt = json.loads((qa / "receipt.json").read_text("utf-8-sig"))
        assert receipt["state"] == ("rolled_back" if fail else "installed"), receipt
        assert result.returncode == (1 if fail else 0), receipt
        deadline = time.monotonic() + 80
        report_file = home / "desktop-smoke.json"
        while time.monotonic() < deadline and (not report_file.exists()
                or not json.loads(report_file.read_text("utf-8")).get("ok") or (home / "runtime.json").exists()):
            time.sleep(.2)
        report = json.loads((home / "desktop-smoke.json").read_text("utf-8"))
        assert report["ok"], report
        assert not (home / "runtime.json").exists()
        assert hashes() == before
    install()
    install(fail=True)
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((target / name).read_bytes()).hexdigest() == digest
    print("Real embedded-window installation and rollback after page failure passed; five protected hashes unchanged:", qa)


if __name__ == "__main__":
    main()
