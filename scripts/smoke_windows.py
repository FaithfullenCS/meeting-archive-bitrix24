"""Exercise the packaged GUI application without browser, tray, OAuth or private data."""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def expect_status(request, expected, opener=None):
    try:
        with (opener.open(request, timeout=5) if opener else urllib.request.urlopen(request, timeout=5)) as response:
            actual = response.status
            payload = response.read()
    except urllib.error.HTTPError as exc:
        actual, payload = exc.code, exc.read()
    assert actual == expected, f"Expected HTTP {expected}; received {actual}"
    return payload


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, default=root / "dist/MeetingArchive/MeetingArchive.exe")
    parser.add_argument("--home", type=Path, default=root / ".work/test-built-home")
    parser.add_argument("--tray", action="store_true", help="Also run the native tray message loop")
    parser.add_argument("--bundled-folders", action="store_true", help="Use an isolated default profile to check bundled data folders")
    args = parser.parse_args()
    executable, home = args.exe.resolve(), args.home.resolve()
    if not executable.is_file():
        raise SystemExit("Build the Windows executable first.")
    if home.exists() and any(home.iterdir()):
        raise SystemExit("Choose an empty test home; never smoke-test a personal profile.")
    home.mkdir(parents=True, exist_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run([str(executable), "--self-test"], check=True, timeout=30, creationflags=flags)
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8765)) == 0:
            raise SystemExit("Port 8765 is occupied. This check will not touch another application.")
    launch_arguments = [str(executable), "--no-browser", "--home", str(home)]
    if not args.tray:
        launch_arguments.append("--no-tray")
    environment = os.environ.copy()
    if args.bundled_folders:
        environment["MEETING_ARCHIVE_HOME"] = str(home)
    process = subprocess.Popen(launch_arguments, creationflags=flags, env=environment)
    csrf, opener = None, None
    try:
        deadline = time.monotonic() + 20
        runtime = None
        while time.monotonic() < deadline and process.poll() is None:
            path = home / "runtime.json"
            if path.is_file():
                runtime = json.loads(path.read_text(encoding="utf-8"))
                break
            time.sleep(.1)
        assert runtime and runtime["pid"] == process.pid, "Own packaged process did not start"
        # Keep the smoke client on the same host as the session cookie and launcher URL.
        base = "http://localhost:8765"
        launch_url = urlsplit(runtime["url"])
        launch_tokens = parse_qs(launch_url.query).get("launch", [])
        assert (
            launch_url.scheme == "http"
            and launch_url.netloc == "localhost:8765"
            and launch_url.path == "/"
            and len(launch_tokens) == 1
            and bool(launch_tokens[0])
        ), "Unexpected local launch URL"
        # runtime.json can precede lifespan startup; wait without printing the launch capability.
        while time.monotonic() < deadline:
            try:
                expect_status(base + "/api/bootstrap", 401)
                break
            except urllib.error.URLError:
                time.sleep(.1)
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        page = expect_status(runtime["url"], 200, opener)
        assert b"Meeting Archive" in page
        bootstrap = json.loads(expect_status(base + "/api/bootstrap", 200, opener))
        csrf = bootstrap["csrf"]
        assert bootstrap["settings"]["auto_download"] is False
        assert bootstrap["settings"]["auto_local"] is False
        assert bootstrap["connected"] is False
        assert bootstrap["settings"]["oauth_flow"] == "local"
        if args.bundled_folders:
            assert Path(bootstrap["settings"]["archive_root"]) == executable.parent / "Данные/Совещания"
            assert Path(bootstrap["settings"]["watch_folder"]) == executable.parent / "Данные/Входящие записи"
            assert Path(bootstrap["settings"]["archive_root"]).is_dir()
            assert Path(bootstrap["settings"]["watch_folder"]).is_dir()
        for asset in ("app.js", "styles.css"):
            assert expect_status(base + "/static/" + asset, 200, opener)
        catalogue = json.loads(expect_status(base + "/api/meetings", 200, opener))
        assert catalogue["total"] == 0
        def local_post(path, values):
            return urllib.request.Request(base + path, method="POST", data=json.dumps(values).encode(),
                headers={"X-CSRF-Token": csrf, "Origin": base, "Content-Type": "application/json"})
        cleanup = json.loads(expect_status(local_post("/api/materials/plan", {}), 200, opener))
        assert cleanup["files"] == 0 and cleanup["ids"] == [] and cleanup["token"]
        expect_status(local_post("/api/module/models/remove-plan", {"packages": [{"engine": "whisper", "model": "tiny"}]}), 400, opener)
        # Starting a synthetic attempt must not contact a portal or require a relay.
        login = urllib.request.Request(base + "/api/auth/oauth", method="POST",
            data=json.dumps({"portal": "synthetic.bitrix24.ru", "client_id": "smoke-id",
                             "client_secret": "smoke-secret"}).encode(),
            headers={"X-CSRF-Token": csrf, "Origin": base, "Content-Type": "application/json"})
        started = json.loads(expect_status(login, 200, opener))
        assert started["flow"] == "local"
        assert parse_qs(urlsplit(started["url"]).query)["redirect_uri"] == ["http://localhost:8765/callback"]
        expect_status("http://localhost:8765/callback?state=wrong", 400)
        expect_status(urllib.request.Request(base + "/api/shutdown", data=b"{}", method="POST"), 403, opener)
        request = urllib.request.Request(base + "/api/shutdown", data=b"{}", method="POST",
                                         headers={"X-CSRF-Token": csrf, "Origin": base,
                                                  "Content-Type": "application/json"})
        assert json.loads(expect_status(request, 200, opener))["stopping"]
        assert process.wait(timeout=20) == 0, "Packaged application did not exit cleanly"
        assert not (home / "runtime.json").exists(), "Launch capability was not cleaned up"
        print("Packaged self-test, session, CSRF, static UI, defaults, localhost OAuth start, empty catalogue and graceful shutdown passed"
              + (" with the native tray loop." if args.tray else " without the tray."))
    finally:
        if process.poll() is None:
            if csrf and opener:
                try:
                    expect_status(urllib.request.Request("http://127.0.0.1:8765/api/shutdown", data=b"{}",
                                  method="POST", headers={"X-CSRF-Token": csrf}), 200, opener)
                    process.wait(timeout=5)
                except (OSError, AssertionError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                process.kill()  # Only the exact child created by this smoke check.
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
