import asyncio

import httpx
import pytest

from meeting_archive.app import create_app
from meeting_archive.bitrix import BitrixClient
from meeting_archive.service import Service


@pytest.fixture
async def oob_ui(tmp_path, settings, vault):
    home = tmp_path / "profile"
    settings.save(home)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "oauth.bitrix.info":
            return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh",
                "scope": "call,user_basic", "member_id": "synthetic-member",
                "client_endpoint": "https://synthetic.bitrix24.ru/rest/", "expires_in": 3600})
        if request.url.path.endswith("user.current"):
            return httpx.Response(200, json={"result": {"ID": "41"}})
        return httpx.Response(200, json={"result": {"items": [], "hasMore": False}})

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    service = Service(home, vault=vault, client=client)
    client.settings = service.settings
    app = create_app(service, launch_token="synthetic-launch", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://127.0.0.1:8765") as browser:
        await browser.get("/?launch=synthetic-launch")
        csrf = (await browser.get("/api/bootstrap")).json()["csrf"]
        yield browser, service, vault, {"X-CSRF-Token": csrf}, requests
    await client.close()
    service.db.close()


@pytest.mark.asyncio
async def test_oob_no_relay_one_use_code_verifies_personal_catalogue(oob_ui):
    browser, service, vault, headers, requests = oob_ui
    started = await browser.post("/api/auth/oauth", headers=headers, json={"flow": "oob",
        "portal": "synthetic.bitrix24.ru", "client_id": "owned-id", "client_secret": "owned-secret"})
    assert started.status_code == 200
    assert started.json()["flow"] == "oob"
    assert requests == []  # No platform/server dependency to start the login.
    payload = {"attempt": started.json()["attempt"], "code": "synthetic-one-use-code"}
    assert (await browser.post("/api/auth/oauth/code", headers=headers, json={**payload, "attempt": "wrong"})).status_code == 400
    assert requests == []
    assert (await browser.post("/api/auth/oauth/code", json=payload)).status_code == 403
    response = await browser.post("/api/auth/oauth/code", headers=headers, json=payload)
    assert response.status_code == 200
    await asyncio.sleep(0)
    assert service.settings.user_id == 41
    assert service.settings.oauth_relay == "" and service.settings.oauth_flow == "oob"
    assert vault.read()["refresh_token"] == "new-refresh"
    assert b"redirect_uri" not in requests[0].content
    assert requests[0].url.host == "oauth.bitrix.info"
    catalogue = [r for r in requests if r.url.path.endswith("call.followup.list")]
    assert catalogue and b'"participantId":41' in catalogue[0].content
    assert (await browser.post("/api/auth/oauth/code", headers=headers, json=payload)).status_code == 400
    bootstrap = (await browser.get("/api/bootstrap")).json()
    assert bootstrap["secret_status"]["client_secret_saved"]
    assert "new-access" not in str(bootstrap) and "owned-secret" not in str(bootstrap)


@pytest.mark.asyncio
async def test_oob_wrong_portal_keeps_existing_connection(oob_ui, monkeypatch):
    browser, service, vault, headers, _ = oob_ui
    service.settings.auth_mode = "webhook"
    vault.update(webhook="https://synthetic.bitrix24.ru/rest/41/old-hook/")
    old = vault.read()

    async def wrong_portal(*_):
        raise ValueError("Небезопасный портал")

    monkeypatch.setattr(service.client, "exchange", wrong_portal)
    started = await browser.post("/api/auth/oauth", headers=headers, json={"flow": "oob",
        "portal": "synthetic.bitrix24.ru", "client_id": "owned-id", "client_secret": "owned-secret"})
    response = await browser.post("/api/auth/oauth/code", headers=headers,
        json={"attempt": started.json()["attempt"], "code": "one-use-code"})
    assert response.status_code == 400
    assert vault.read() == old
    assert service.connected() and service.settings.auth_mode == "webhook"
