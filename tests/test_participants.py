import json
from pathlib import Path

import httpx
import pytest

from meeting_archive.app import create_app
from meeting_archive.bitrix import BitrixClient
from meeting_archive.service import Service


@pytest.fixture
async def catalogue_ui(tmp_path, settings, vault):
    home = tmp_path / "home"
    settings.save(home)

    def network_forbidden(request):
        raise AssertionError("Participant filtering must use only local metadata")

    bitrix = BitrixClient(settings, vault, transport=httpx.MockTransport(network_forbidden))
    service = Service(home, vault=vault, client=bitrix)
    for call_id, portal, people in (
        (1, "synthetic.bitrix24.ru", [{"userId": 41, "name": "Иван"}, {"userId": 42, "name": "Анна"}, {"userId": "42", "name": "Анна"}]),
        (2, "synthetic.bitrix24.ru", [{"userId": "41", "name": "Иван"}]),
        (3, "other.bitrix24.ru", [{"userId": 41, "name": "Иван"}]),
        (4, "synthetic.bitrix24.ru", [{"userId": 43}, {"name": "Неизвестный"}, {"userId": True}]),
    ):
        service.db.upsert(portal, {"callId": call_id, "participants": people,
            "startDate": "2026-10-02T09:00:00+03:00", "durationSeconds": 3600,
            "overview": {"topic": "Synthetic"}})
    app = create_app(service, launch_token="synthetic-filter", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://127.0.0.1:8765") as browser:
        await browser.get("/?launch=synthetic-filter")
        yield browser, service
    await bitrix.close()
    service.db.close()


@pytest.mark.asyncio
async def test_local_participants_have_stable_portal_ids_and_unique_meeting_counts(catalogue_ui):
    browser, service = catalogue_ui
    people = (await browser.get("/api/participants")).json()["items"]
    assert len(people) == 4
    by_id = {p["id"]: p for p in people}
    assert by_id["synthetic.bitrix24.ru:user:42"]["count"] == 1
    assert by_id["synthetic.bitrix24.ru:user:41"]["count"] == 2
    assert by_id["other.bitrix24.ru:user:41"]["count"] == 1
    assert by_id["synthetic.bitrix24.ru:user:43"]["label"] == "Участник #43"
    assert not Path(service.settings.archive_root).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("people,count", [
    ("", 4), ("synthetic.bitrix24.ru:user:41", 2),
    ("synthetic.bitrix24.ru:user:41,synthetic.bitrix24.ru:user:42", 1),
    ("synthetic.bitrix24.ru:user:41,other.bitrix24.ru:user:41", 0),
    ("synthetic.bitrix24.ru:user:9999", 0),
])
async def test_filter_matches_all_selected_people_without_conflating_names(catalogue_ui, people, count):
    browser, _ = catalogue_ui
    response = await browser.get("/api/meetings", params={"participants": people, "min_minutes": 30})
    assert response.status_code == 200
    assert response.json()["total"] == count


@pytest.mark.asyncio
@pytest.mark.parametrize("extra,count", [
    ({"q": "sYnThEtIc", "date_from": "2026-10-01", "date_to": "2026-10-02"}, 1),
    ({"q": "Unknown meeting"}, 0),
    ({"date_from": "2026-10-03"}, 0),
])
async def test_participants_title_and_period_are_combined(catalogue_ui, extra, count):
    browser, _ = catalogue_ui
    response = await browser.get("/api/meetings", params={
        "participants": "synthetic.bitrix24.ru:user:41,synthetic.bitrix24.ru:user:42", **extra})
    assert response.json()["total"] == count


@pytest.mark.asyncio
async def test_catalogue_scan_exposes_participants_before_any_material_download(catalogue_ui):
    browser, service = catalogue_ui
    requests = []

    def metadata_only(request):
        requests.append(request)
        if request.url.path.endswith("/im.recent.list"):
            return httpx.Response(200, json={"result": {"items": [], "hasMore": False}})
        assert request.url.path.endswith("/call.followup.list")
        assert "participants" in json.loads(request.content)["select"]
        return httpx.Response(200, json={"result": {"items": [{
            "callId": 5, "startDate": "2026-10-04T09:00:00+03:00",
            "durationSeconds": 1800, "participants": [{"userId": 41}, {"userId": 77, "name": "Ольга"}],
        }], "hasMore": False}})

    await service.client.http.aclose()
    service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(metadata_only))
    await service.scan(full=True)
    people = (await browser.get("/api/participants")).json()["items"]
    participant = next(p for p in people if p["user_id"] == "77")
    assert participant["label"] == "Ольга"
    filtered = (await browser.get("/api/meetings", params={"participants": participant["id"]})).json()
    assert filtered["total"] == 1
    meeting = service.db.meeting(filtered["items"][0]["id"])
    assert meeting["folder"] == ""
    assert not service.db.rows("SELECT * FROM jobs")
    assert not Path(service.settings.archive_root).exists()
    assert len(requests) == 2
    assert all(not request.url.path.endswith("/call.followup.get") for request in requests)
