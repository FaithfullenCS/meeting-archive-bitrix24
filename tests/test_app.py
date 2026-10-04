from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from meeting_archive.app import create_app, relay_base, probe_relay
from meeting_archive.bitrix import BitrixClient, TOKEN_URL
from meeting_archive.service import Service

RELAY = "https://relay.test/integrations/meeting-archive"
PORTAL = "synthetic.bitrix24.ru"


@pytest.fixture
def synthetic_relay(monkeypatch):
    async def probe(base, portal):
        assert base == RELAY
        assert portal == PORTAL
        return {"allowed_portals": [PORTAL]}

    monkeypatch.setattr("meeting_archive.app.probe_relay", probe)


async def start_relay_oauth(browser, csrf, **overrides):
    return await browser.post("/api/auth/oauth", json={"portal": PORTAL, "client_id": "new-id",
        "client_secret": "new-secret", "oauth_relay": RELAY, "flow": "relay", **overrides},
        headers={"X-CSRF-Token": csrf})


async def use_bitrix_transport(service, handler):
    await service.client.http.aclose()
    service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def oauth_tokens(**overrides):
    return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600,
            "member_id": "synthetic-member", "scope": "call,user_basic",
            "client_endpoint": "https://synthetic.bitrix24.ru/rest/", **overrides}


@pytest.fixture
async def ui(tmp_path, settings, vault):
    home = tmp_path / "home"
    settings.save(home)
    bitrix = BitrixClient(settings, vault, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    service = Service(home, vault=vault, client=bitrix)
    bitrix.settings = service.settings
    app = create_app(service, launch_token="synthetic-launch-token", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://127.0.0.1:8765") as browser:
        yield browser, service, vault
    await bitrix.close()
    service.db.close()


async def login(browser):
    assert (await browser.get("/?launch=synthetic-launch-token")).status_code == 303
    response = await browser.get("/api/bootstrap")
    assert response.status_code == 200
    return response.json()["csrf"]


def saved_materials(service, call=700):
    row = service.db.upsert(
        service.settings.portal,
        {
            "callId": call,
            "uuid": f"session-{call}",
            "startDate": "2026-10-04T09:00:00Z",
            "overview": {"topic": "Synthetic cleanup"},
        },
    )
    folder = service.archive.ensure(row)
    for name, content in {
        "audio/first.wav": b"synthetic audio",
        "audio/second.mp4": b"synthetic video",
        "bitrix/transcript.txt": b"synthetic text",
        "notes/memory.md": b"keep notes",
        "local/run-a/transcript.txt": b"local a",
        "local/run-a/run.json": b'{"sample":false}',
        "local/run-b/transcript.txt": b"local b",
        "local/run-b/run.json": b'{"sample":false}',
    }.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    service.db.update_meeting(row["id"], folder=str(folder), audio="saved", bitrix="saved", local="saved", requested=1)
    service.archive.manifest(service.db.meeting(row["id"]))
    return row["id"], folder


@pytest.mark.asyncio
async def test_material_delete_individual_files_runs_then_all_preserves_catalogue(ui):
    browser, service, _ = ui
    csrf = await login(browser)
    headers = {"X-CSRF-Token": csrf}
    id, folder = saved_materials(service)
    service.db.set_state("auto_local_pending:" + str(id), '["first.wav"]')
    plan = (await browser.post("/api/materials/plan", json={"ids": [id]}, headers=headers)).json()
    assert {c["target"] for c in plan["choices"]} == {
        "audio/first.wav",
        "audio/second.mp4",
        "local/run-a",
        "local/run-b",
        "bitrix",
        "notes",
    }
    targets = ["audio/first.wav", "local/run-a"]
    plan = (await browser.post("/api/materials/plan", json={"ids": [id], "targets": targets}, headers=headers)).json()
    args = {"ids": [id], "targets": targets, "token": plan["token"]}
    assert (await browser.post("/api/materials/remove", json=args, headers=headers)).status_code == 400
    result = await browser.post("/api/materials/remove", json={**args, "confirm": True}, headers=headers)
    assert result.status_code == 200, result.text
    assert not (folder / "audio/first.wav").exists() and not (folder / "local/run-a").exists()
    assert (folder / "audio/second.mp4").exists() and (folder / "local/run-b/transcript.txt").exists()
    assert (folder / "notes/memory.md").exists() and (folder / "bitrix/transcript.txt").exists()
    row = service.db.meeting(id)
    assert row["audio"] == row["local"] == row["bitrix"] == "saved"
    assert row["requested"] == 0 and service.db.get_state("auto_local_pending:" + str(id)) == "[]"
    plan = (await browser.post("/api/materials/plan", json={"ids": [id]}, headers=headers)).json()
    result = await browser.post(
        "/api/materials/remove",
        json={"ids": plan["ids"], "targets": plan["targets"], "token": plan["token"], "confirm": True},
        headers=headers,
    )
    assert result.status_code == 200, result.text
    row = service.db.meeting(id)
    assert row["audio"] == row["local"] == row["bitrix"] == "not_saved"
    assert row["metadata"] and (folder / "meeting.json").is_file()
    assert len(service.db.rows("SELECT id FROM meetings")) == 1
    assert (await browser.get(f"/api/meeting/{id}")).status_code == 200


@pytest.mark.asyncio
async def test_material_delete_stale_plan_busy_and_escape_do_not_remove_files(ui):
    browser, service, _ = ui
    csrf = await login(browser)
    headers = {"X-CSRF-Token": csrf}
    id, folder = saved_materials(service)
    plan = service.material_plan([id], ["audio"])
    (folder / "audio/new.wav").write_bytes(b"new")
    response = await browser.post(
        "/api/materials/remove",
        json={"ids": [id], "targets": ["audio"], "token": plan["token"], "confirm": True},
        headers=headers,
    )
    assert response.status_code == 400 and (folder / "audio/first.wav").exists()
    for target in ["meeting.json", "audio/../notes", "audio/first.wav/extra", "audio/..", "audio/C:other"]:
        assert (
            await browser.post("/api/materials/plan", json={"ids": [id], "targets": [target]}, headers=headers)
        ).status_code == 400
    job = service.db.enqueue("transcribe", id)
    assert (await browser.post("/api/materials/plan", json={"ids": [id]}, headers=headers)).status_code == 400
    service.db.job_update(job, state="done")
    service.material_maintenance.add(id)
    assert (await browser.post("/api/jobs/" + str(job) + "/retry", json={}, headers=headers)).status_code == 400
    assert (await browser.post("/api/link", json={"source_id": id, "target_id": id + 1}, headers=headers)).status_code == 400
    assert (folder / "audio/first.wav").exists()


@pytest.mark.asyncio
async def test_archive_cleanup_all_and_http_range(ui):
    browser, service, _ = ui
    csrf = await login(browser)
    headers = {"X-CSRF-Token": csrf}
    id, folder = saved_materials(service, 701)
    second, other = saved_materials(service, 702)
    data = (folder / "audio/first.wav").read_bytes()
    response = await browser.get(f"/api/file/{id}/audio/first.wav", headers={"Range": "bytes=2-6"})
    assert response.status_code == 206 and response.content == data[2:7]
    plan = service.material_plan()
    assert plan["meetings"] == 2
    result = await browser.post(
        "/api/materials/remove",
        json={"ids": plan["ids"], "targets": plan["targets"], "token": plan["token"], "confirm": True},
        headers=headers,
    )
    assert result.status_code == 200, result.text
    assert len(service.db.rows("SELECT id FROM meetings")) == 2
    assert not (other / "notes/memory.md").exists() and (folder / "meeting.json").exists()


@pytest.mark.asyncio
async def test_diarization_support_is_reported_separately_from_hf_access(ui):
    import time
    browser, service, vault = ui
    await login(browser)
    service.settings.diarization = True
    service.db.set_state("hf_verified", "1")
    vault.update(hf_token="synthetic-hf-token")
    service.module.discovery_at = time.monotonic()
    service.module.discovered = {"sources": [{"compatible": True, "models": ["large-v3"],
        "versions": {"torch": "2.7.1", "faster-whisper": "1.2"}}], "cached_models": ["large-v3"]}
    data = (await browser.get("/api/module/engines")).json()
    assert data["features_ready"] is False
    assert data["processing_support"]["requires_install"] is True
    assert "диаризац" in data["processing_support"]["reason"].lower()


@pytest.mark.asyncio
async def test_selected_model_remove_api_rejects_busy_and_requires_confirmation(ui):
    browser, service, _ = ui
    csrf = await login(browser)
    headers = {"X-CSRF-Token": csrf}
    root = service.module.root
    root.mkdir()
    (root / ".meeting-archive-owned").write_text("synthetic")
    packages = [{"engine": "whisper", "model": "tiny"}]
    directory = root / service.module.model_targets("whisper", "tiny")[0]
    directory.mkdir(parents=True)
    (directory / "weight").write_bytes(b"synthetic model")
    plan = (await browser.post("/api/module/models/remove-plan", json={"packages": packages}, headers=headers)).json()
    args = {"packages": packages, "token": plan["token"]}
    assert (await browser.post("/api/module/models/remove", json=args, headers=headers)).status_code == 400
    job = service.db.enqueue("install")
    assert (await browser.post("/api/module/models/remove", json={**args, "confirm": True}, headers=headers)).status_code == 400
    service.db.job_update(job, state="done")
    result = await browser.post("/api/module/models/remove", json={**args, "confirm": True}, headers=headers)
    assert result.status_code == 200, result.text
    assert not directory.exists() and (root / ".meeting-archive-owned").exists()
    assert service.module_maintenance is False


@pytest.mark.asyncio
async def test_api_requires_tray_launch_session_and_bootstrap_hides_credentials(ui):
    browser, service, vault = ui
    assert (await browser.get("/api/bootstrap")).status_code == 401
    assert (await browser.get("/?launch=wrong-token")).status_code == 401
    vault.update(webhook="https://synthetic.bitrix24.ru/rest/41/synthetic-hook/", hf_token="hf_synthetic-secret")
    await login(browser)
    response = await browser.get("/api/bootstrap")
    for value in ("synthetic-token", "synthetic-refresh", "synthetic-secret", "synthetic-hook", "hf_synthetic-secret"):
        assert value not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert browser.cookies.get("meeting_session")


@pytest.mark.asyncio
async def test_host_origin_and_csrf_protect_local_mutations(ui):
    browser, service, _ = ui
    csrf = await login(browser)
    assert (await browser.get("/api/bootstrap", headers={"Host": "evil.test"})).status_code == 403
    assert (await browser.post("/api/settings", json={"paused": True})).status_code == 403
    assert (await browser.post("/api/settings", json={"paused": True}, headers={"X-CSRF-Token": "wrong"})).status_code == 403
    assert (await browser.post("/api/settings", json={"paused": True}, headers={"X-CSRF-Token": csrf, "Origin": "https://evil.test"})).status_code == 403
    assert service.settings.paused is False
    response = await browser.post("/api/settings", json={"paused": True},
                                  headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1:8765"})
    assert response.status_code == 200
    assert service.settings.paused is True


@pytest.mark.asyncio
async def test_hardware_probe_persists_only_for_matching_profile(ui, monkeypatch):
    browser, service, _ = ui
    csrf = await login(browser)
    monkeypatch.setattr(service.module, "python", lambda *_: "synthetic-python")
    monkeypatch.setattr("meeting_archive.app.worker_probe", lambda *_: {"compatible": True, "cuda": True, "torch": "synthetic"})
    response = await browser.post("/api/hardware/probe", json={}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    hardware = (await browser.get("/api/hardware")).json()
    assert hardware["probe"]["compatible"] and "работает" in hardware["cuda"]
    service.settings.model = "tiny"
    hardware = (await browser.get("/api/hardware")).json()
    assert "probe" not in hardware, "A previous model's test must not validate a different profile"


@pytest.mark.asyncio
async def test_meeting_search_accepts_title_catalogue_id_and_call_id(ui):
    browser, service, _ = ui
    await login(browser)
    row = service.db.upsert(service.settings.portal, {"callId": 9876, "overview": {"topic": "Планирование"}})
    for query in ("планир", str(row["id"]), "9876"):
        response = await browser.get("/api/meetings", params={"q": query})
        assert response.status_code == 200 and response.json()["total"] == 1
    assert not service.db.rows("SELECT * FROM jobs"), "Selecting a meeting must not download materials"


@pytest.mark.asyncio
async def test_history_filter_combines_dates_duration_title_and_material_state(ui):
    browser, service, _ = ui
    await login(browser)
    rows = [
        {"callId": 1, "startDate": "2026-09-30T09:00:00+03:00", "durationSeconds": 3600, "overview": {"topic": "Проект"}},
        {"callId": 2, "startDate": "2026-10-01T09:00:00+03:00", "durationSeconds": 1800, "overview": {"topic": "Учебный проект"}},
        {"callId": 3, "startDate": "2026-10-01T11:00:00+03:00", "durationSeconds": 600, "overview": {"topic": "Проект короткий"}},
        {"callId": 4, "startDate": "2026-10-01T13:00:00+03:00", "durationSeconds": 1800, "overview": {"topic": "Другой вопрос"}},
    ]
    for item in rows:
        saved = service.db.upsert(service.settings.portal, item)
        service.db.update_meeting(saved["id"], audio="saved")
    response = await browser.get("/api/meetings", params={"q": "проект", "date_from": "2026-10-01", "date_to": "2026-10-01",
                                                         "min_minutes": 20, "max_minutes": 40, "audio": "saved"})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["call_id"] == "2"
    assert not service.db.rows("SELECT * FROM jobs")


@pytest.mark.asyncio
async def test_file_serving_rejects_path_escape_and_user_notes(ui, tmp_path):
    browser, service, _ = ui
    await login(browser)
    saved = service.db.upsert(service.settings.portal, {"callId": 1, "startDate": "2026-10-01T09:00:00+03:00"})
    folder = service.archive.ensure(saved)
    (folder / "audio/voice.wav").write_bytes(b"synthetic audio")
    (folder / "notes/private.txt").write_text("private synthetic notes", encoding="utf-8")
    service.db.update_meeting(saved["id"], folder=str(folder))
    assert (await browser.get(f"/api/file/{saved['id']}/audio/voice.wav")).content == b"synthetic audio"
    assert (await browser.get(f"/api/file/{saved['id']}/notes/private.txt")).status_code == 400
    escaped = await browser.get(f"/api/file/{saved['id']}/audio/%2e%2e/notes/private.txt")
    assert escaped.status_code == 400
    assert "private synthetic notes" not in escaped.text


@pytest.mark.asyncio
async def test_oauth_state_scope_and_member_binding_then_one_use_callback(ui, synthetic_relay):
    browser, service, vault = ui
    csrf = await login(browser)
    vault.update(hf_token="hf_existing-independent")
    started = await start_relay_oauth(browser, csrf)
    assert started.status_code == 200
    state = parse_qs(urlsplit(started.json()["url"]).query)["state"][0]
    calls = []

    def handler(request):
        calls.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json=oauth_tokens())
        payload = json.loads(request.content)
        assert payload["auth"] == "new-access"
        if request.url.path.endswith("/user.current"):
            return httpx.Response(200, json={"result": {"ID": "42"}})
        assert request.url.path.endswith("/call.followup.list")
        assert payload["filter"]["participantId"] == 42
        return httpx.Response(200, json={"result": {"items": [], "hasMore": False}})

    await use_bitrix_transport(service, handler)
    query = {"state": "wrong-state", "domain": "synthetic.bitrix24.ru", "code": "synthetic-code", "scope": "call,user", "member_id": "synthetic-member"}
    assert (await browser.get("/oauth/callback", params=query)).status_code == 400
    assert calls == []
    query["state"] = state
    valid = await browser.get("/oauth/callback", params=query)
    assert valid.status_code == 303
    await asyncio.sleep(0)
    assert sum(str(request.url) == TOKEN_URL for request in calls) == 1
    assert service.settings.user_id == 42
    assert service.settings.member_id == "synthetic-member"
    assert vault.read()["refresh_token"] == "new-refresh"
    assert vault.read()["hf_token"] == "hf_existing-independent"
    assert service.settings.oauth_relay == RELAY
    assert service.settings.auth_mode == "oauth"
    assert not service.db.rows("SELECT * FROM jobs")
    assert (await browser.get("/oauth/callback", params=query)).status_code == 400
    assert sum(str(request.url) == TOKEN_URL for request in calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [{"domain": "other.bitrix24.ru"}, {"scope": "user"}, {"member_id": ""}])
async def test_oauth_callback_rejects_missing_rights_or_wrong_portal_before_exchange(ui, monkeypatch, synthetic_relay, override):
    browser, service, _ = ui
    csrf = await login(browser)
    started = await start_relay_oauth(browser, csrf)
    assert started.status_code == 200
    state = parse_qs(urlsplit(started.json()["url"]).query)["state"][0]

    async def unexpected_exchange(*_):
        pytest.fail("Unsafe callback reached token exchange")

    monkeypatch.setattr(service.client, "exchange", unexpected_exchange)
    query = {"state": state, "domain": "synthetic.bitrix24.ru", "code": "synthetic-code", "scope": "call,user", "member_id": "synthetic-member", **override}
    assert (await browser.get("/oauth/callback", params=query)).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["profile", "catalogue", "member"])
async def test_failed_oauth_candidate_preserves_working_webhook_and_hf(ui, synthetic_relay, failure):
    browser, service, vault = ui
    csrf = await login(browser)
    service.settings.auth_mode = "webhook"
    service.settings.save(service.home)
    vault.update(webhook="https://synthetic.bitrix24.ru/rest/41/working-hook/", hf_token="hf_existing-independent")
    service.db.set_state("hf_verified", "existing-verification")
    old_settings, old_vault = asdict(service.settings), vault.read()
    old_settings_file = (service.home / "settings.json").read_bytes()
    requests = []

    def handler(request):
        requests.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json=oauth_tokens(member_id="changed-member" if failure == "member" else "synthetic-member"))
        if request.url.path.endswith("/user.current"):
            if failure == "profile":
                return httpx.Response(401, json={"error": "INVALID_TOKEN"})
            return httpx.Response(200, json={"result": {"ID": "42"}})
        assert json.loads(request.content)["filter"]["participantId"] == 42
        return httpx.Response(403, json={"error": {"code": "ACCESS_DENIED"}})

    await use_bitrix_transport(service, handler)
    started = await start_relay_oauth(browser, csrf)
    assert started.status_code == 200
    assert vault.read() == old_vault
    state = parse_qs(urlsplit(started.json()["url"]).query)["state"][0]
    response = await browser.get("/oauth/callback", params={"state": state, "domain": PORTAL,
        "code": "synthetic-code", "member_id": "synthetic-member", "scope": "call,user_basic"})
    assert response.status_code == 400
    assert requests
    assert asdict(service.settings) == old_settings
    assert vault.read() == old_vault
    assert (service.home / "settings.json").read_bytes() == old_settings_file
    assert service.db.get_state("hf_verified") == "existing-verification"
    assert service.connected()
    assert not service.auth_error


@pytest.mark.asyncio
async def test_relay_probe_failure_does_not_store_candidate_credentials(ui, monkeypatch):
    browser, service, vault = ui
    csrf = await login(browser)
    old_settings, old_vault = asdict(service.settings), vault.read()

    async def rejected_probe(*_):
        raise ValueError("Обработчик не разрешает этот портал")

    monkeypatch.setattr("meeting_archive.app.probe_relay", rejected_probe)
    response = await start_relay_oauth(browser, csrf)
    assert response.status_code == 400
    assert asdict(service.settings) == old_settings
    assert vault.read() == old_vault
    assert "new-secret" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [("state", "duplicate"), ("access_token", "unexpected-token"),
                                  ("redirect_uri", "https://evil.test/"), ("server_domain", "evil.test")])
async def test_callback_duplicate_or_unexpected_parameters_do_not_reach_exchange(ui, monkeypatch, synthetic_relay, extra):
    browser, service, _ = ui
    csrf = await login(browser)
    started = await start_relay_oauth(browser, csrf)
    state = parse_qs(urlsplit(started.json()["url"]).query)["state"][0]

    async def unexpected_exchange(*_):
        pytest.fail("Callback parameter injection reached the token exchange")

    monkeypatch.setattr(service.client, "exchange", unexpected_exchange)
    query = [("state", state), ("domain", PORTAL), ("code", "synthetic-code"),
             ("member_id", "synthetic-member"), ("scope", "call,user_basic"), extra]
    assert (await browser.get("/oauth/callback", params=query)).status_code == 400


@pytest.mark.parametrize("value", ["", "http://relay.test/", "https://127.0.0.1/", "https://relay.test:8443/",
                                  "https://user:password@relay.test/", "https://relay.test/?key=value",
                                  "https://relay.test/#fragment", "https://relay.local/", "https://relay.test/../other"])
def test_relay_configuration_rejects_insecure_or_ambiguous_urls(value):
    with pytest.raises(ValueError):
        relay_base(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{"allowed_portals": ["other.bitrix24.ru"]}, {"loopback": "http://evil.test/"},
                                      {"callback_path": "/oauth/callback"}, {"credentials": "server-stored"}, {"version": 2}])
async def test_probe_relay_checks_protocol_and_portal_allowlist(monkeypatch, overrides):
    real_client = httpx.AsyncClient
    data = {"protocol": "meeting-archive-oauth-relay", "version": 1,
            "callback_path": "/integrations/meeting-archive/oauth/callback",
            "install_path": "/integrations/meeting-archive/oauth/install",
            "loopback": "http://127.0.0.1:8765/oauth/callback", "credentials": "desktop-only",
            "allowed_portals": [PORTAL], **overrides}

    def handler(request):
        assert str(request.url) == RELAY + "/oauth/health"
        return httpx.Response(200, json=data)

    monkeypatch.setattr("meeting_archive.app.httpx.AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(ValueError):
        await probe_relay(RELAY, PORTAL)


@pytest.mark.asyncio
async def test_cpu_and_diarization_require_explicit_checks(ui):
    browser, service, _ = ui
    csrf = await login(browser)
    headers = {"X-CSRF-Token": csrf}
    assert (await browser.post("/api/settings", json={"device": "cpu"}, headers=headers)).status_code == 400
    assert service.settings.device == "cuda"
    assert (await browser.post("/api/settings", json={"diarization": True}, headers=headers)).status_code == 400
    assert service.settings.diarization is False
    assert (await browser.post("/api/settings", json={"device": "cpu", "cpu_confirmed": True}, headers=headers)).status_code == 200
    assert service.settings.device == "cpu"


@pytest.mark.asyncio
async def test_archive_root_cannot_be_silently_changed_after_materials_saved(ui, tmp_path):
    browser, service, _ = ui
    csrf = await login(browser)
    saved = service.db.upsert(service.settings.portal, {"callId": 1})
    service.db.update_meeting(saved["id"], folder=str(tmp_path / "archive/existing"))
    old = service.settings.archive_root
    response = await browser.post("/api/settings", json={"archive_root": str(tmp_path / "other")}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 400
    assert service.settings.archive_root == old


@pytest.mark.asyncio
async def test_hidden_partial_worker_outputs_are_not_available(ui):
    browser, service, _ = ui
    await login(browser)
    saved = service.db.upsert(service.settings.portal, {"callId": 1})
    folder = service.archive.ensure(saved)
    pending = folder / "local/.pending-synthetic"
    pending.mkdir()
    (pending / "run.json").write_text(json.dumps({"model": "tiny"}), encoding="utf-8")
    (pending / "transcript.txt").write_text("incomplete", encoding="utf-8")
    service.db.update_meeting(saved["id"], folder=str(folder))
    response = await browser.get(f"/api/meeting/{saved['id']}")
    assert response.status_code == 200
    assert response.json()["runs"] == []
    assert response.json()["local_text"] == ""
    assert not any("pending" in file["name"] for file in response.json()["files"])
    assert (await browser.get(f"/api/file/{saved['id']}/local/.pending-synthetic/transcript.txt")).status_code == 400


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Native Windows junction regression")
async def test_meeting_detail_does_not_read_transcript_outside_meeting(ui, tmp_path):
    import _winapi
    browser, service, _ = ui
    await login(browser)
    saved = service.db.upsert(service.settings.portal, {"callId": 1})
    folder = service.archive.ensure(saved)
    external = tmp_path / "outside"
    external.mkdir()
    (external / "transcript.txt").write_text("outside synthetic private transcript", encoding="utf-8")
    bitrix = folder / "bitrix"
    bitrix.rmdir()
    _winapi.CreateJunction(str(external), str(bitrix))
    service.db.update_meeting(saved["id"], folder=str(folder))
    try:
        response = await browser.get(f"/api/meeting/{saved['id']}")
        assert response.status_code == 400
        assert "outside synthetic private transcript" not in response.text
        assert (await browser.get(f"/api/file/{saved['id']}/bitrix/transcript.txt")).status_code == 400
    finally:
        os.rmdir(bitrix)
