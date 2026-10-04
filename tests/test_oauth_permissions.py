import json

import httpx
import pytest

from meeting_archive.bitrix import BitrixClient, BitrixError, TOKEN_URL
from test_bitrix import token_response
from test_oob import oob_ui as shared_ui

permission_ui = shared_ui


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["app", "", None, ["app"], "call%2Cuser_basic", "user_basic, call"])
async def test_token_service_scope_is_resolved_using_current_application_grants(settings, vault, scope):
    requests = []

    def handler(request):
        requests.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json=token_response(scope=scope))
        assert str(request.url) == "https://synthetic.bitrix24.ru/rest/scope"
        assert json.loads(request.content) == {"auth": "rotated-access"}
        return httpx.Response(200, json={"result": ["user_basic", "call", "disk"]})

    before = vault.read()
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        data = await client.exchange("one-use-code", "owned-id", "owned-secret")
    finally:
        await client.close()
    assert data["access_token"] == "rotated-access"
    assert len(requests) == (1 if isinstance(scope, str) and "call" in scope else 2)
    assert vault.read() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("granted", [["user_basic", "disk"], {"items": []}, ["call", 42], None])
async def test_generic_scope_cannot_bypass_missing_or_unverified_call_permission(settings, vault, granted):
    before = vault.read()

    def handler(request):
        if request.url.host == "oauth.bitrix.info":
            return httpx.Response(200, json=token_response(scope="app"))
        return httpx.Response(200, json={"result": granted})

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(BitrixError) as exc:
            await client.exchange("one-use-code", "owned-id", "owned-secret")
        assert exc.value.auth
    finally:
        await client.close()
    assert vault.read() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_generic_refresh_verifies_grants_before_replacing_saved_pair(settings, vault, allowed):
    settings.member_id = "synthetic-member"
    before = vault.read()

    def handler(request):
        if request.url.host == "oauth.bitrix.info":
            return httpx.Response(200, json=token_response(scope="app"))
        return httpx.Response(200, json={"result": ["call", "user_basic"] if allowed else ["user_basic"]})

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        if allowed:
            assert await client.token(force=True) == "rotated-access"
            assert vault.read()["refresh_token"] == "rotated-refresh"
        else:
            with pytest.raises(BitrixError):
                await client.token(force=True)
            assert vault.read() == before
    finally:
        await client.close()


@pytest.mark.parametrize("code, phrase", [
    ("BITRIX_REST_V3_EXCEPTION_ACCESSDENIEDEXCEPTION", "запретил доступ"),
    ("BITRIX_REST_V3_EXCEPTION_INSUFFICIENTSCOPEEXCEPTION", "право call"),
    ("INVALID_CREDENTIALS", "отозвано"),
])
def test_access_failures_are_actionable_and_require_reauthorization(code, phrase):
    response = httpx.Response(400, json={"error": {"code": code, "message": "private details"}})
    with pytest.raises(BitrixError) as exc:
        BitrixClient._response(response)
    assert exc.value.auth and not exc.value.retryable
    assert phrase in str(exc.value) and code in str(exc.value)
    assert "private details" not in str(exc.value)


@pytest.mark.asyncio
async def test_local_oauth_generic_token_scope_connects_only_after_personal_catalogue_check(permission_ui):
    browser, service, vault, headers, requests = permission_ui

    def handler(request):
        requests.append(request)
        if request.url.host == "oauth.bitrix.info":
            return httpx.Response(200, json=token_response(scope="app"))
        if request.url.path.endswith("/scope"):
            return httpx.Response(200, json={"result": ["user_basic", "call", "disk"]})
        if request.url.path.endswith("/user.current"):
            return httpx.Response(200, json={"result": {"ID": "41"}})
        assert json.loads(request.content)["filter"]["participantId"] == 41
        return httpx.Response(200, json={"result": {"items": [], "hasMore": False}})

    service.client.http._transport = httpx.MockTransport(handler)
    started = await browser.post("/api/auth/oauth", headers=headers,
        json={"portal": "synthetic.bitrix24.ru", "client_id": "owned-id", "client_secret": "owned-secret", "flow": "local"})
    response = await browser.get("/callback", params={"state": started.json()["attempt"],
        "domain": "synthetic.bitrix24.ru", "scope": "user_basic, call, disk", "member_id": "synthetic-member", "code": "one-use-code"})
    assert response.status_code == 303
    assert service.settings.auth_mode == "oauth" and service.settings.user_id == 41
    assert vault.read()["refresh_token"] == "rotated-refresh"
    assert any(request.url.path.endswith("call.followup.list") for request in requests)
