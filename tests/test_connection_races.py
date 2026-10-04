import asyncio
from dataclasses import replace

import httpx
import pytest

from meeting_archive.bitrix import BitrixClient
from meeting_archive.service import Service
from meeting_archive.app import create_app


@pytest.mark.asyncio
async def test_connection_waits_for_old_refresh_and_preserves_parallel_non_auth_changes(tmp_path, settings, vault):
    home = tmp_path / "home"
    settings.save(home)
    verified = asyncio.Event()

    async def handler(request):
        if request.url.path.endswith("user.current"):
            return httpx.Response(200, json={"result": {"ID": 42}})
        verified.set()
        return httpx.Response(200, json={"result": {"items": [], "hasMore": False}})

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    service = Service(home, client=client, vault=vault)
    client.settings = service.settings
    await client.refresh_lock.acquire()
    task = asyncio.create_task(service.activate_connection(
        replace(service.settings, auth_mode="webhook"), {"webhook": "https://synthetic.bitrix24.ru/rest/41/synthetic-hook/"}))
    try:
        await asyncio.wait_for(verified.wait(), 2)
        await asyncio.sleep(0)
        assert not task.done()
        # These values change while the browser's new login is being verified.
        vault.update(hf_token="hf_new_parallel_synthetic")
        service.settings.language = "en"
        service.settings.local_schedule = {"mode": "window", "days": [0], "start": "22:00", "end": "06:00"}
        client.refresh_lock.release()
        await asyncio.wait_for(task, 2)
        assert service.settings.user_id == 42
        assert service.settings.language == "en"
        assert service.settings.local_schedule["mode"] == "window"
        assert vault.read()["hf_token"] == "hf_new_parallel_synthetic"
    finally:
        if client.refresh_lock.locked():
            client.refresh_lock.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()
        service.db.close()


@pytest.mark.asyncio
async def test_disconnect_waits_for_inflight_refresh_before_clearing_credentials(tmp_path, settings, vault):
    home = tmp_path / "home"
    settings.save(home)
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    service = Service(home, client=client, vault=vault)
    app = create_app(service, launch_token="synthetic-race", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as browser:
        await browser.get("/?launch=synthetic-race")
        csrf = (await browser.get("/api/bootstrap")).json()["csrf"]
        await client.refresh_lock.acquire()
        task = asyncio.create_task(browser.post("/api/auth/disconnect", headers={"X-CSRF-Token": csrf}))
        try:
            await asyncio.sleep(.01)
            assert not task.done()
            vault.update(access_token="synthetic-rotated", hf_token="hf_untouched_synthetic")
            client.refresh_lock.release()
            assert (await asyncio.wait_for(task, 2)).json()["connected"] is False
            assert vault.read()["access_token"] == ""
            assert vault.read()["hf_token"] == "hf_untouched_synthetic"
        finally:
            if client.refresh_lock.locked():
                client.refresh_lock.release()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    await client.close()
    service.db.close()
