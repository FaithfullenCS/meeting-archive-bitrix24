import ctypes
import hashlib
import json
import os

import httpx
import pytest

from meeting_archive.bitrix import BitrixClient, BitrixError, TOKEN_URL
from test_bitrix import token_response
from test_reset_profile import profile, reset  # noqa: F401,F811


@pytest.mark.asyncio
async def test_rest3_denial_recovers_once_with_new_pair(settings, vault):
    settings.member_id = "synthetic-member"
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json=token_response())
        if json.loads(request.content)["auth"] == "synthetic-token":
            return httpx.Response(403, json={"error": {"code": "BITRIX_REST_V3_EXCEPTION_ACCESSDENIEDEXCEPTION"}})
        return httpx.Response(200, json={"result": {"items": []}})

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        assert await client.call("call.followup.list", {}) == {"items": []}
        assert requests.count(TOKEN_URL) == 1
    finally:
        await client.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows mutex")
def test_reset_ignores_reused_pid_of_an_unrelated_process(profile):  # noqa: F811
    home, _, meeting, _ = profile
    (home / "runtime.json").write_text(json.dumps({"pid": os.getpid()}))
    result = reset(home, "Profile", "-ConfirmReset")
    assert result.returncode == 0, result.stderr
    assert meeting.exists() and not (home / "settings.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows mutex")
def test_reset_blocks_a_real_profile_mutex_even_without_runtime(profile):  # noqa: F811
    home, _, meeting, _ = profile
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    name = "Local\\MeetingArchive-" + hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:24]
    handle = kernel.CreateMutexW(None, False, name)
    assert handle
    try:
        result = reset(home, "All", "-ConfirmReset")
        assert result.returncode != 0
        assert (home / "settings.json").exists() and meeting.exists()
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.asyncio
async def test_real_denial_is_not_hidden_or_retried_forever(settings, vault):
    settings.member_id = "synthetic-member"
    renewals = []
    def handler(request):
        if str(request.url) == TOKEN_URL:
            renewals.append(request)
            return httpx.Response(200, json=token_response())
        return httpx.Response(403, json={"error": {"code": "BITRIX_REST_V3_EXCEPTION_ACCESSDENIEDEXCEPTION"}})
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(BitrixError):
            await client.call("call.followup.list", {})
        assert len(renewals) == 1
    finally:
        await client.close()
