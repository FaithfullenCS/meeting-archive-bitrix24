"""Run real background loops without creating or opening a UI window."""
import asyncio

from meeting_archive.app import create_app
from test_service import record, service as service_fixture

service = service_fixture


async def test_lifespan_discovers_meetings_and_chats_and_runs_jobs_without_ui(service, monkeypatch):
    service.settings.auto_download = True
    service.settings.auto_since = "2026-01-01T00:00:00+00:00"
    seen_job = asyncio.Event()
    network_calls = []

    async def catalogue_pages(*_):
        yield [record()]

    async def profile():
        return {"ID": 41, "NAME": "Synthetic"}

    async def call(method, params, **kwargs):
        network_calls.append(method)
        if method == "im.recent.list":
            return {"items": [{"id": "chat101", "chat_id": 101, "type": "chat", "title": "Synthetic chat"}], "hasMore": False}
        if method == "im.recent.get":
            return []
        raise AssertionError(f"Unexpected synthetic Bitrix call: {method}")

    async def perform(job, progress):
        assert job["kind"] == "fetch"
        seen_job.set()

    async def no_module_discovery():
        return None

    monkeypatch.setattr(service, "catalogue_pages", catalogue_pages)
    monkeypatch.setattr(service.client, "profile", profile)
    monkeypatch.setattr(service.client, "call", call)
    monkeypatch.setattr(service, "perform", perform)
    monkeypatch.setattr(service, "discover_resources", no_module_discovery)
    monkeypatch.setattr(service.notifications, "completed", lambda _: None)
    app = create_app(service, launch_token="synthetic-no-window")

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(seen_job.wait(), timeout=5)
        async def chat_discovered():
            while not service.chat_archive.store().state("last_discovery", 0):
                await asyncio.sleep(.01)
        await asyncio.wait_for(chat_discovered(), timeout=5)
        async def job_finished():
            while service.db.rows("SELECT state FROM jobs WHERE kind='fetch'")[0]["state"] != "done":
                await asyncio.sleep(.01)
        await asyncio.wait_for(job_finished(), timeout=5)
        assert len(service.db.rows("SELECT id FROM meetings")) == 1
        assert service.db.get_state(service.identity_key()) == "Synthetic"
        assert service.chat_archive.store().chat(101)["title"] == "Synthetic chat"
        assert "im.recent.list" in network_calls
        assert service.chat_archive.loop_running and service.chat_archive.file_loop_running
