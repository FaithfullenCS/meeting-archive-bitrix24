import hashlib
import json
import shutil
import socket
import subprocess
import sys
import argparse
import uuid
import time
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from meeting_archive.updates import stage_archive  # noqa: E402 (standalone script adds the checkout)
from meeting_archive import __version__  # noqa: E402

parser = argparse.ArgumentParser(description="Program update and rollback on a synthetic profile")
parser.add_argument("--previous", type=Path, default=root / "dist/MeetingArchive")
args = parser.parse_args()
qa = root / ".work" / ("update-smoke-" + uuid.uuid4().hex)
qa.mkdir()
target = qa / "upgrade-target"
home = qa / "upgrade-profile"
source = args.previous.resolve()
previous_version = json.loads((source / "build-info.json").read_text("utf-8"))["version"]
new_program = qa / "staged-program"
assert not target.exists() and not home.exists()
with socket.socket() as probe:
    assert probe.connect_ex(("127.0.0.1", 8765)) != 0, "Another app owns the port"
target.mkdir()
for item in source.iterdir():
    if item.name == "Данные":
        continue
    if item.is_dir():
        shutil.copytree(item, target / item.name)
    else:
        shutil.copy2(item, target / item.name)
files = {p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
         for p in target.rglob("*") if p.is_file() and p.name != "update-manifest.json"}
(target / "update-manifest.json").write_text(json.dumps({"version": previous_version, "files": files}), encoding="utf-8")
home.mkdir()
settings = {"auto_update": False, "archive_root": str(target / "Данные/Совещания"),
            "watch_folder": str(target / "Данные/Входящие записи")}
(home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
sentinels = [target / "Данные/Совещания/synthetic.wav", target / "user-notes.txt",
             home / "module/custom/model.bin", home / "private-note.txt"]
for p in sentinels:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"synthetic preservation sentinel")
manifest = stage_archive(root / "dist/MeetingArchive-Windows-x64.zip", new_program, "v" + __version__)
flags = subprocess.CREATE_NO_WINDOW

def connect():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            runtime = json.loads((home / "runtime.json").read_text("utf-8"))
            client = httpx.Client(timeout=3, follow_redirects=True)
            client.get(runtime["url"]).raise_for_status()
            boot = client.get("http://localhost:8765/api/bootstrap").json()
            if "csrf" in boot:
                return runtime, client, boot
            client.close()
        except (OSError, ValueError, httpx.HTTPError):
            pass
        time.sleep(.2)
    raise AssertionError("Application did not start")

def shutdown(client, boot):
    client.post("http://localhost:8765/api/shutdown", headers={"x-csrf-token": boot["csrf"]}).raise_for_status()
    client.close()
    deadline = time.monotonic() + 25
    while (home / "runtime.json").exists() and time.monotonic() < deadline:
        time.sleep(.2)
    assert not (home / "runtime.json").exists()

old_process = subprocess.Popen([str(target / "MeetingArchive.exe"), "--no-browser", "--no-tray", "--home", str(home)], creationflags=flags)
runtime, client, boot = connect()
assert boot["settings"]["auto_local"] is False
shutdown(client, boot)
old_process.wait(30)
protected = sentinels + [home / "settings.json", home / "secrets.dpapi"]
before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}

def helper(expected, old_files, new_files):
    config = qa / ("install-" + expected + ".json")
    config.write_text(json.dumps({"target": str(target), "staged": str(new_program), "home": str(home),
        "pid": old_process.pid, "version": expected, "headless": True,
        "old_files": list(old_files), "new_files": list(new_files)}), encoding="utf-8")
    environment = {**__import__("os").environ, "_PYI_ARCHIVE_FILE": str(target / "MeetingArchive.exe"),
                   "_PYI_PARENT_PROCESS_LEVEL": "0", "_PYI_APPLICATION_HOME_DIR": str(target / "_internal")}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
        "-File", str(root / "meeting_archive/resources/apply-update.ps1"), "-Config", str(config)],
        creationflags=flags, timeout=100, capture_output=True, text=True, env=environment)
    receipt = json.loads((qa / "receipt.json").read_text("utf-8-sig"))
    return result, receipt

result, receipt = helper(__version__, files, manifest["files"])
assert result.returncode == 0, (receipt, result.stderr)
assert receipt["state"] == "installed", receipt
runtime, client, boot = connect()
assert runtime["version"] == __version__
# A second EXE launch forwards the protocol route to the live server.
subprocess.run([str(target / "MeetingArchive.exe"), "--no-browser", "--no-tray", "--home", str(home),
                "--activate", "meetingarchive:jobs/1,2"], timeout=20, creationflags=flags, check=True)
boot = client.get("http://localhost:8765/api/bootstrap").json()
assert boot["activation"]["ids"] == [1, 2]
assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
shutdown(client, boot)
# Advertise an incompatible version; the helper must restore the previous program.
bad = {**manifest, "version": "999.0.0"}
(new_program / "update-manifest.json").write_text(json.dumps(bad), encoding="utf-8")
result, receipt = helper("999.0.0", manifest["files"], manifest["files"])
assert result.returncode == 1 and receipt["state"] == "rolled_back", (receipt, result.stderr)
runtime, client, boot = connect()
assert runtime["version"] == __version__
assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
shutdown(client, boot)
(qa / "integration-result.json").write_text(json.dumps({"upgrade": previous_version + " -> " + __version__, "rollback": "verified",
    "activation": "forwarded to live instance", "preserved_hashes": before}, indent=2), encoding="utf-8")
print("Upgrade, live activation, rollback and six protected file hashes verified")
