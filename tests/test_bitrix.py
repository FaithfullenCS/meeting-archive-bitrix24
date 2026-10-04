from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from meeting_archive.bitrix import BitrixClient, BitrixError, TOKEN_URL


def token_response(**overrides):
    return {"access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_in": 3600,
            "scope": "call,user_basic", "member_id": "synthetic-member",
            "client_endpoint": "https://synthetic.bitrix24.ru/rest/", **overrides}


@pytest.mark.asyncio
async def test_catalogue_only_metadata_and_preserves_opaque_cursor_for_current_user(settings, vault):
    requests = []
    cursor = {"startDate": "2026-10-01T09:00:00Z", "callId": 123}

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, json={"result": {"items": [{"callId": 123}], "hasMore": True,
                                                         "afterCursor": cursor}})
        return httpx.Response(200, json={"result": {"items": [{"callId": 124}], "hasMore": False}})

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        pages = [page async for page in client.catalogue("2020-01-01", "2026-10-02")]
    finally:
        await client.close()
    assert pages == [[{"callId": 123}], [{"callId": 124}]]
    assert requests[1]["pagination"]["afterCursor"] == cursor
    for payload in requests:
        assert payload["filter"]["participantId"] == 41  # Required even for administrators.
        assert "transcription" not in payload["select"]
        assert "tracks" in payload["select"]  # Metadata only, no audio request.
        assert "overview.summary" not in payload["select"]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{"result": {}}, {"result": []}, {"error": {"code": "ACCESS_DENIED"}}, []])
async def test_bad_catalogue_or_access_failure_is_not_empty_history(settings, vault, data):
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)))
    try:
        with pytest.raises(BitrixError):
            _ = [page async for page in client.catalogue("2020-01-01", "2026-10-02")]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_repeated_cursor_stops_instead_of_infinite_history(settings, vault):
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"result": {"items": [], "hasMore": True, "afterCursor": {"id": 123}}})))
    try:
        with pytest.raises(BitrixError, match="курсор"):
            _ = [page async for page in client.catalogue("2020-01-01", "2026-10-02")]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_expired_oauth_rotates_refresh_token_once_for_concurrent_requests(settings, vault):
    settings.member_id = "synthetic-member"
    tokens = []
    old_requests = []
    both_failed = asyncio.Event()

    async def handler(request):
        if str(request.url) == TOKEN_URL:
            tokens.append(request.content.decode())
            return httpx.Response(200, json=token_response())
        if json.loads(request.content)["auth"] == "synthetic-token":
            old_requests.append(request)
            if len(old_requests) == 2:
                both_failed.set()
            await both_failed.wait()
            return httpx.Response(401, json={"error": "EXPIRED_TOKEN"})
        assert json.loads(request.content)["auth"] == "rotated-access"
        return httpx.Response(200, json={"result": {"ID": "41"}})

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        profiles = await asyncio.wait_for(asyncio.gather(client.profile(), client.profile()), timeout=2)
        assert profiles == [{"ID": "41"}, {"ID": "41"}]
    finally:
        await client.close()
    assert len(tokens) == 1
    assert "synthetic-refresh" in tokens[0]
    assert vault.read()["refresh_token"] == "rotated-refresh"


@pytest.mark.asyncio
async def test_local_expiry_renews_before_the_request(settings, vault):
    vault.update(expires_at=time.time() - 1)
    settings.member_id = "synthetic-member"
    calls = []
    def handler(request):
        calls.append(str(request.url))
        assert str(request.url) == TOKEN_URL
        return httpx.Response(200, json=token_response())
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        assert await client.token() == "rotated-access"
    finally:
        await client.close()
    assert calls == [TOKEN_URL]


@pytest.mark.asyncio
async def test_expired_token_api_error_refreshes_and_retries_original_request(settings, vault):
    settings.member_id = "synthetic-member"
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json=token_response())
        if json.loads(request.content)["auth"] == "synthetic-token":
            return httpx.Response(200, json={"error": "EXPIRED_TOKEN"})
        return httpx.Response(200, json={"result": {"ID": "41"}})

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        assert await client.profile() == {"ID": "41"}
    finally:
        await client.close()
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"scope": "user_basic"}, {"member_id": ""}, {"access_token": ""}, {"refresh_token": ""},
    {"client_endpoint": "https://other.bitrix24.ru/rest/"},
    {"client_endpoint": "http://synthetic.bitrix24.ru/rest/"},
    {"client_endpoint": "https://synthetic.bitrix24.ru:8443/rest/"},
    {"client_endpoint": "https://user:password@synthetic.bitrix24.ru/rest/"},
    {"client_endpoint": "https://synthetic.bitrix24.ru/other/"},
    {"client_endpoint": "https://synthetic.bitrix24.ru/rest/?token=synthetic"},
    {"client_endpoint": "https://synthetic.bitrix24.ru/rest/#fragment"},
])
async def test_oauth_exchange_rejects_unsafe_or_incomplete_tokens_without_replacing_vault(settings, vault, overrides):
    original = vault.read()
    data = token_response(**overrides)
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)))
    try:
        with pytest.raises(BitrixError) as error:
            await client.exchange("synthetic-code", "synthetic-id", "synthetic-secret")
        assert error.value.auth
    finally:
        await client.close()
    assert vault.read() == original


@pytest.mark.asyncio
async def test_oauth_exchange_posts_credentials_only_to_fixed_token_endpoint(settings, vault):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=token_response())

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        tokens = await client.exchange("synthetic-code", "synthetic-id", "synthetic-secret")
    finally:
        await client.close()
    assert tokens["member_id"] == "synthetic-member"
    assert len(seen) == 1
    assert str(seen[0].url) == TOKEN_URL
    assert seen[0].method == "POST"
    assert b"client_secret=synthetic-secret" in seen[0].content
    assert b"code=synthetic-code" in seen[0].content
    assert b"redirect_uri" not in seen[0].content
    assert vault.read()["access_token"] == "synthetic-token"  # Committed only after the real access checks.


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"scope": "user_basic"}, {"member_id": "changed-member"},
    {"client_endpoint": "https://other.bitrix24.ru/rest/"},
    {"client_endpoint": "http://synthetic.bitrix24.ru/rest/"},
    {"client_endpoint": "https://synthetic.bitrix24.ru:8443/rest/"},
    {"client_endpoint": "https://synthetic.bitrix24.ru/wrong/"},
    {"client_endpoint": "https://synthetic.bitrix24.ru/rest/?secret=value"},
])
async def test_refresh_rejects_changed_portal_identity_or_rights_before_persisting(settings, vault, overrides):
    settings.member_id = "synthetic-member"
    original = vault.read()
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=token_response(**overrides))))
    try:
        with pytest.raises(BitrixError) as error:
            await client.token(force=True, failed_token="synthetic-token")
        assert error.value.auth
    finally:
        await client.close()
    assert vault.read() == original


@pytest.mark.asyncio
async def test_expired_response_after_retry_does_not_refresh_forever(settings, vault):
    settings.member_id = "synthetic-member"
    requests = []

    def handler(request):
        requests.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json=token_response())
        return httpx.Response(401, json={"error": "EXPIRED_TOKEN"})

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(BitrixError, match="EXPIRED_TOKEN"):
            await client.profile()
    finally:
        await client.close()
    assert len(requests) == 3
    assert sum(str(request.url) == TOKEN_URL for request in requests) == 1


@pytest.mark.asyncio
async def test_webhook_v3_uses_portal_and_participant_filter(settings, vault):
    settings.auth_mode = "webhook"
    vault.update(webhook="https://synthetic.bitrix24.ru/rest/41/synthetic-webhook/")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"result": {"items": [], "hasMore": False}})

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        assert [page async for page in client.catalogue("2020-01-01", "2026-10-02")] == [[]]
    finally:
        await client.close()
    assert str(seen[0].url) == "https://synthetic.bitrix24.ru/rest/api/41/synthetic-webhook/call.followup.list"
    assert json.loads(seen[0].content)["filter"]["participantId"] == 41
    assert "auth" not in json.loads(seen[0].content)


@pytest.mark.parametrize("url", ["http://synthetic.bitrix24.ru/a", "https://127.0.0.1/a",
                                 "https://synthetic.bitrix24.ru.evil.test/a", "https://user:pass@synthetic.bitrix24.ru/a",
                                 "https://synthetic.bitrix24.ru:8443/a", "https://localhost/a"])
def test_recording_url_rejects_unsafe_hosts(settings, vault, url):
    client = BitrixClient(settings, vault)
    try:
        with pytest.raises(BitrixError):
            client.safe_download_url(url)
    finally:
        asyncio.run(client.close())
