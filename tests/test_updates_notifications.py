from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile

import httpx
import pytest

from meeting_archive import __version__
from meeting_archive.notifications import Notifications, parse_activation
from meeting_archive.settings import Settings
from meeting_archive.updates import ASSET, REPOSITORY, stage_archive, safe_relative, version


@pytest.fixture
def service(tmp_path, vault):
    from meeting_archive.service import Service
    result = Service(tmp_path / "home", vault=vault)
    yield result
    result.db.close()


def archive_fixture(path, *, bad_hash=False, extra=None):
    content = b"synthetic executable"
    manifest = {"version": "9.0.0", "files": {"MeetingArchive.exe": hashlib.sha256(content).hexdigest()}}
    if bad_hash:
        manifest["files"]["MeetingArchive.exe"] = "0" * 64
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("MeetingArchive/update-manifest.json", json.dumps(manifest))
        archive.writestr("MeetingArchive/MeetingArchive.exe", content)
        archive.writestr("MeetingArchive/Данные/Совещания/", b"")
        if extra:
            info = zipfile.ZipInfo("placeholder")
            info.filename = extra
            archive.writestr(info, b"bad")
    return path.read_bytes()


@pytest.mark.parametrize("value", ["../a", "/a", "a/../b", "C:/a", "a\\b", "Данные/a", "a//b", "a./b"])
def test_unsafe_manifest_paths(value):
    with pytest.raises(ValueError):
        safe_relative(value)


def test_stage_only_program_files(tmp_path):
    archive_fixture(tmp_path / "build.zip")
    result = stage_archive(tmp_path / "build.zip", tmp_path / "staged", "v9.0.0")
    assert result["version"] == "9.0.0"
    assert (tmp_path / "staged/MeetingArchive.exe").read_bytes() == b"synthetic executable"
    assert not (tmp_path / "staged/Данные").exists()


@pytest.mark.parametrize("extra", ["../escaped", "MeetingArchive/../escaped", "MeetingArchive/a\\b"])
def test_zip_escape_rejected_before_payload_write(tmp_path, extra):
    archive_fixture(tmp_path / "build.zip", extra=extra)
    with pytest.raises(ValueError):
        stage_archive(tmp_path / "build.zip", tmp_path / "staged", "v9.0.0")
    assert not (tmp_path / "staged/MeetingArchive.exe").exists()


def test_invalid_hash_and_version(tmp_path):
    archive_fixture(tmp_path / "build.zip", bad_hash=True)
    with pytest.raises(ValueError, match="сумма"):
        stage_archive(tmp_path / "build.zip", tmp_path / "staged", "9.0.0")
    with pytest.raises(ValueError, match="Версия"):
        stage_archive(tmp_path / "build.zip", tmp_path / "staged", "9.0.1")


def test_version_comparison():
    assert version("v0.2.10") > version("0.2.9")
    with pytest.raises(ValueError):
        version("v1.0.0-beta")


@pytest.mark.parametrize("uri", ["https://bad", "meetingarchive:meeting/0", "meetingarchive:meeting/1,2",
                                "meetingarchive:settings/1", "meetingarchive:jobs/1;calc", "meetingarchive:meeting/../1"])
def test_activation_rejects_arbitrary_input(uri):
    with pytest.raises(ValueError):
        parse_activation(uri)


def test_activation_routes():
    assert parse_activation("meetingarchive:meeting/15") == {"route": "meeting", "ids": [15]}
    assert parse_activation("meetingarchive:jobs/2,3")["ids"] == [2, 3]


async def test_notification_delivery_failure_isolated(service):
    def failure(*_):
        raise OSError("synthetic unavailable Windows")
    notifier = Notifications(service, failure)
    assert not await notifier.send("test", "test", "meetingarchive:settings")
    assert "synthetic" in notifier.error


async def test_error_switch_and_auth_dedup(service):
    calls = []
    notifier = Notifications(service, lambda *args: calls.append(args) or True)
    await notifier.failed({"id": 1}, "auth", True)
    await notifier.failed({"id": 2}, "auth", True)
    assert len(calls) == 1
    assert calls[0][2] == "meetingarchive:connection"
    service.settings.notify_errors = False
    await notifier.failed({"id": 3}, "error")
    assert len(calls) == 1


async def test_completed_groups_and_switches(service, monkeypatch):
    calls = []
    notifier = Notifications(service, lambda *args: calls.append(args) or True)
    original_sleep = asyncio.sleep
    async def immediate(_):
        await original_sleep(0)
    monkeypatch.setattr("meeting_archive.notifications.asyncio.sleep", immediate)
    notifier.completed({"id": 1, "meeting_id": 11, "kind": "fetch"})
    notifier.completed({"id": 2, "meeting_id": 22, "kind": "fetch"})
    await asyncio.gather(*list(notifier.timers.values()))
    assert len(calls) == 1 and calls[0][2] == "meetingarchive:jobs/1,2"
    service.settings.notifications_enabled = False
    notifier.completed({"id": 3, "meeting_id": 33, "kind": "transcribe"})
    assert not notifier.timers


async def test_github_download_verified(service, tmp_path, monkeypatch):
    content = archive_fixture(tmp_path / "build.zip")
    digest = hashlib.sha256(content).hexdigest()
    base = f"https://github.com/{REPOSITORY}/releases/download/v9.0.0/"
    assets = [{"name": name, "state": "uploaded", "size": len(content), "browser_download_url": base + name}
              for name in (ASSET, ASSET + ".sha256")]
    def handler(request):
        if request.url.host == "api.github.com":
            assert REPOSITORY in str(request.url)
            return httpx.Response(200, json={"tag_name": "v9.0.0", "draft": False, "prerelease": False, "assets": assets})
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text=f"{digest}  {ASSET}\n")
        return httpx.Response(200, content=content)
    client_class = httpx.AsyncClient
    monkeypatch.setattr("meeting_archive.updates.httpx.AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
    await service.updates.check(True)
    assert service.updates.state["state"] == "ready", service.updates.state
    assert (service.updates.root / "v9.0.0/program/MeetingArchive.exe").exists()


@pytest.mark.parametrize("tag,draft,prerelease", [("v" + __version__, False, False), ("v0.1.0", False, False),
                                              ("v9.0.0", True, False), ("v9.0.0", False, True)])
async def test_ignore_non_upgrade(service, monkeypatch, tag, draft, prerelease):
    client_class = httpx.AsyncClient
    def handler(_):
        return httpx.Response(200, json={"tag_name": tag, "draft": draft, "prerelease": prerelease})
    monkeypatch.setattr("meeting_archive.updates.httpx.AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw))
    await service.updates.check(True)
    assert service.updates.state["state"] == "current"


async def test_rate_limit_backoff(service, monkeypatch):
    client_class = httpx.AsyncClient
    def handler(_):
        return httpx.Response(429, headers={"Retry-After": "7200"})
    monkeypatch.setattr("meeting_archive.updates.httpx.AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw))
    await service.updates.check()
    import time
    assert service.updates.state["retry_at"] > time.time() + 7100
    assert service.updates.state["state"] == "error"


def test_old_settings_receive_defaults(tmp_path):
    tmp_path.joinpath("settings.json").write_text('{"paused": true}', encoding="utf-8")
    settings = Settings.load(tmp_path)
    assert settings.paused and settings.notifications_enabled and settings.auto_update


async def test_desktop_activation_capability_and_session_security(service):
    from meeting_archive.app import create_app
    app = create_app(service, launch_token="synthetic-desktop-token", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765") as client:
        assert (await client.post("/api/desktop/activate", json={"uri": "meetingarchive:settings"})).status_code == 403
        headers = {"x-desktop-token": "synthetic-desktop-token"}
        assert (await client.post("/api/desktop/activate", headers={**headers, "Origin": "https://bad.invalid"},
                                  json={"uri": "meetingarchive:settings"})).status_code == 403
        assert (await client.post("/api/desktop/activate", headers=headers, json={"uri": "meetingarchive:meeting/5"})).status_code == 200
        assert (await client.get("/api/updates")).status_code == 401
        await client.get("/?launch=synthetic-desktop-token")
        boot = (await client.get("/api/bootstrap")).json()
        assert boot["activation"]["ids"] == [5]
        assert (await client.post("/api/notifications/ack", json={"sequence": boot["activation"]["sequence"]})).status_code == 403
        csrf = {"x-csrf-token": boot["csrf"]}
        await client.post("/api/notifications/ack", json={"sequence": "stale"}, headers=csrf)
        assert service.notifications.activation
        await client.post("/api/notifications/ack", json={"sequence": boot["activation"]["sequence"]}, headers=csrf)
        assert service.notifications.activation is None
        assert (await client.post("/api/updates/install", headers=csrf)).status_code == 400


async def test_wait_for_jobs_and_cancel_install(service, monkeypatch):
    import sys
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    service.updates.shutdown = lambda: pytest.fail("Application must not shut down while busy")
    service.updates.state["state"] = "ready"
    service.active_tasks[1] = object()
    service.updates.request_install()
    await asyncio.sleep(0)
    assert service.updates.waiting
    service.updates.cancel_install()
    await asyncio.gather(service.updates.install_task, return_exceptions=True)
    assert not service.updates.waiting


async def test_waiting_update_never_claims_next_job(service, monkeypatch):
    service.alive = True
    service.updates.waiting = True
    monkeypatch.setattr(service.db, "claim", lambda *_args, **_kw: pytest.fail("Claimed a job during update"))
    task = asyncio.create_task(service.job_loop(("fetch",)))
    await asyncio.sleep(.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_native_shortcut_identity_in_isolated_folder(tmp_path):
    import os
    import sys
    if os.name != "nt":
        pytest.skip("Windows COM shortcut")
    import ctypes
    from meeting_archive.desktop_shortcut import notification_shortcut, shell_link, query, call, checked, GUID, PropertyKey, PropVariant
    from meeting_archive.notifications import APP_ID, ACTIVATOR
    path = tmp_path / "test.lnk"
    notification_shortcut(path, sys.executable, "--self-test", APP_ID, ACTIVATOR)
    assert path.is_file()
    with shell_link() as link:
        persist = query(link, "0000010b-0000-0000-c000-000000000046")
        try:
            checked(call(persist, 5, [ctypes.c_wchar_p, ctypes.c_uint32], str(path), 0))
        finally:
            call(persist, 2, [])
        store = query(link, "886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
        try:
            for pid, expected in ((5, APP_ID), (26, ACTIVATOR)):
                key = PropertyKey(GUID.from_string("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"), pid)
                value = PropVariant()
                checked(call(store, 5, [ctypes.POINTER(PropertyKey), ctypes.POINTER(PropVariant)], ctypes.byref(key), ctypes.byref(value)))
                try:
                    if pid == 5:
                        assert ctypes.wstring_at(value.value.pointer) == expected
                    else:
                        assert ctypes.string_at(value.value.pointer, 16) == bytes(GUID.from_string(expected))
                finally:
                    ctypes.WinDLL("ole32").PropVariantClear(ctypes.byref(value))
        finally:
            call(store, 2, [])
