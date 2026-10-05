import httpx
import pytest

from meeting_archive import browser
from meeting_archive.app import create_app
from meeting_archive.service import Service


@pytest.fixture
async def service(tmp_path, settings, vault):
    home = tmp_path / "home"
    settings.save(home)
    instance = Service(home, vault=vault)
    yield instance
    await instance.client.close()
    instance.db.close()


@pytest.mark.asyncio
async def test_desktop_readiness_requires_session_and_csrf(service, monkeypatch):
    registered = []
    monkeypatch.setattr(browser, "mark_ready", lambda: registered.append(True))
    app = create_app(service, "synthetic-window", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765") as client:
        assert (await client.post("/api/desktop/ready")).status_code == 401
        await client.get("/?launch=synthetic-window")
        assert (await client.post("/api/desktop/ready")).status_code == 403
        csrf = (await client.get("/api/bootstrap")).json()["csrf"]
        assert (await client.post("/api/desktop/ready", headers={"X-CSRF-Token": csrf})).status_code == 200
        assert registered == [True]


@pytest.mark.asyncio
async def test_surviving_window_reconnects_after_server_restart_but_profile_reset_revokes_session(service):
    app = create_app(service, "old-launch", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765") as client:
        await client.get("/?launch=old-launch")
        cookies = client.cookies
        old_csrf = (await client.get("/api/bootstrap")).json()["csrf"]
    app = create_app(service, "new-launch", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765", cookies=cookies) as client:
        data = (await client.get("/api/bootstrap")).json()
        assert data["csrf"] != old_csrf
        assert service.vault.read()["ui_session"] not in str(data)
        assert (await client.post("/api/settings", json={"paused": True}, headers={"X-CSRF-Token": old_csrf})).status_code == 403
        assert (await client.get("/?launch=old-launch")).status_code == 200  # Existing valid session only.
    service.vault.write({})  # Simulated uninstall of the synthetic profile.
    app = create_app(service, "reset-launch", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765", cookies=cookies) as client:
        assert (await client.get("/api/bootstrap")).status_code == 401


@pytest.mark.asyncio
async def test_desktop_open_uses_capability_and_running_controller(service,monkeypatch):
    calls=[]
    monkeypatch.setattr(browser,"open_browser",lambda url:calls.append(url))
    app=create_app(service,"desktop-test-token",manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://localhost:8765") as client:
        assert (await client.post("/api/desktop/open")).status_code==403
        headers={"x-desktop-token":"desktop-test-token"}
        assert (await client.post("/api/desktop/open",headers={**headers,"origin":"http://localhost:8765"})).status_code==403
        assert (await client.post("/api/desktop/open",headers=headers,json={"url":"https://untrusted.test"})).status_code==200
    assert calls==["http://localhost:8765/?launch=desktop-test-token"]
