import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest

from meeting_archive.app import LOCAL_CALLBACK
from test_oob import oob_ui as shared_oob_ui

local_ui = shared_oob_ui


async def start(browser, headers, **overrides):
    return await browser.post("/api/auth/oauth", headers=headers, json={
        "portal": "synthetic.bitrix24.ru", "client_id": "owned-id",
        "client_secret": "owned-secret", **overrides})


def query(started, **overrides):
    return {"state": started.json()["attempt"], "domain": "synthetic.bitrix24.ru",
            "code": "synthetic-one-use-code", "member_id": "synthetic-member",
            "scope": "call,user_basic,disk", "server_domain": "oauth.bitrix.info", **overrides}


@pytest.mark.asyncio
async def test_local_default_redirect_returns_to_same_computer_and_verifies_catalogue(local_ui):
    browser, service, vault, headers, requests = local_ui
    started = await start(browser, headers)
    assert started.status_code == 200 and started.json()["flow"] == "local"
    assert requests == []
    link = urlsplit(started.json()["url"])
    assert link.scheme == "https" and link.hostname == "synthetic.bitrix24.ru"
    assert parse_qs(link.query) == {"client_id": ["owned-id"],
        "state": [started.json()["attempt"]], "redirect_uri": [LOCAL_CALLBACK]}
    assert "owned-secret" not in started.text
    # The callback is public: Bitrix24 opens localhost, while the initiating UI was 127.0.0.1.
    completed = await browser.get(LOCAL_CALLBACK, params=query(started))
    assert completed.status_code == 303 and completed.headers["location"] == "http://localhost:8765/?launch=synthetic-launch#connection"
    assert "httponly" in completed.headers["set-cookie"].lower()
    await asyncio.sleep(0)
    assert service.settings.oauth_flow == "local" and service.settings.oauth_relay == ""
    assert service.settings.user_id == 41 and service.settings.member_id == "synthetic-member"
    assert vault.read()["refresh_token"] == "new-refresh"
    token_request = requests[0]
    assert token_request.method == "POST" and token_request.url.host == "oauth.bitrix.info"
    body = parse_qs(token_request.content.decode())
    assert body["redirect_uri"] == [LOCAL_CALLBACK] and body["client_secret"] == ["owned-secret"]
    assert any(r.url.path.endswith("user.current") for r in requests)
    assert any(r.url.path.endswith("call.followup.list") and b'"participantId":41' in r.content for r in requests)
    bootstrap = await browser.get("http://localhost:8765/api/bootstrap")
    assert bootstrap.status_code == 200
    assert "owned-secret" not in bootstrap.text and "new-access" not in bootstrap.text
    assert (await browser.get(LOCAL_CALLBACK, params=query(started))).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"domain": "other.bitrix24.ru"}, {"scope": "user_basic"}, {"member_id": ""},
    {"code": ""}, {"code": "x" * 257}, {"code": "code\n"},
    {"server_domain": "untrusted.invalid"},
])
async def test_local_rejects_invalid_callback_before_exchange(local_ui, override):
    browser, _, vault, headers, requests = local_ui
    old = vault.read()
    started = await start(browser, headers)
    assert (await browser.get("/callback", params=query(started, **override))).status_code == 400
    assert requests == [] and vault.read() == old
    assert (await browser.get("/callback", params=query(started))).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["state", "duplicates", "unknown", "wrong_route"])
async def test_local_route_and_state_are_bound_to_attempt(local_ui, invalid):
    browser, _, _, headers, requests = local_ui
    started = await start(browser, headers)
    parameters = query(started)
    path = "/callback"
    if invalid == "state":
        parameters["state"] = "wrong-state"
    elif invalid == "duplicates":
        parameters = list(parameters.items()) + [("code", "another-code")]
    elif invalid == "unknown":
        parameters["access_token"] = "unsolicited-token"
    else:
        path = "/oauth/callback"
    assert (await browser.get(path, params=parameters)).status_code == 400
    assert requests == []
    # A stray browser request cannot consume a legitimate state.
    assert (await browser.get("/callback", params=query(started))).status_code == 303


@pytest.mark.asyncio
async def test_local_expired_attempt_does_not_exchange(local_ui, monkeypatch):
    browser, _, _, headers, requests = local_ui
    started = await start(browser, headers)
    import meeting_archive.app as app_module
    current = app_module.time.time()
    monkeypatch.setattr(app_module.time, "time", lambda: current + 601)
    assert (await browser.get("/callback", params=query(started))).status_code == 400
    assert requests == []


@pytest.mark.asyncio
async def test_local_failed_exchange_preserves_connection_and_hides_code(local_ui, monkeypatch):
    browser, service, vault, headers, _ = local_ui
    service.settings.auth_mode = "webhook"
    vault.update(webhook="https://synthetic.bitrix24.ru/rest/41/old-hook/")
    old = vault.read()

    async def failed(*_):
        raise ValueError("synthetic-one-use-code owned-secret <script>broken</script>")

    monkeypatch.setattr(service.client, "exchange", failed)
    started = await start(browser, headers)
    response = await browser.get("/callback", params=query(started))
    assert response.status_code == 400
    assert "synthetic-one-use-code" not in response.text and "owned-secret" not in response.text
    assert "<script>" not in response.text
    assert vault.read() == old and service.settings.auth_mode == "webhook"
    assert service.connected()
    assert (await browser.get("/callback", params=query(started))).status_code == 400


@pytest.mark.asyncio
async def test_local_reuses_saved_secret_only_for_same_application(local_ui):
    browser, service, _, headers, requests = local_ui
    started = await start(browser, headers, client_id="synthetic-id", client_secret="")
    assert started.status_code == 200 and requests == []
    assert (await browser.get("/callback", params=query(started, server_domain="oauth.bitrix24.tech"))).status_code == 303
    await asyncio.sleep(0)
    assert service.settings.oauth_flow == "local"
    assert (await start(browser, headers, client_id="another-id", client_secret="")).status_code == 400
