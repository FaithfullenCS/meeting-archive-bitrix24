"""A slow model directory must not block the initial interface."""
import asyncio
import threading

from test_app import ui as ui_fixture

ui = ui_fixture


async def test_bootstrap_returns_while_model_scan_is_running_and_reuses_one_scan(ui, monkeypatch):
    browser, service, _ = ui
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_status():
        calls.append(1)
        entered.set()
        if not release.wait(15):
            raise RuntimeError("synthetic model scan timed out")
        return {"installed": True, "size_gb": 2, "installing": False}

    monkeypatch.setattr(service.module, "status", slow_status)
    monkeypatch.setattr(service, "automation_status", lambda readiness=None: {"pending": 0, "ready": True, "reason": "Готова"})
    monkeypatch.setattr(service, "transcription_status", lambda: {"ready": True, "reason": ""})
    await browser.get("/?launch=synthetic-launch-token")
    try:
        first = await asyncio.wait_for(browser.get("/api/bootstrap"), timeout=3)
        assert first.status_code == 200
        assert first.json()["module"]["checking"] is True
        assert first.json()["transcription"]["ready"] is False
        for _ in range(5):
            assert (await asyncio.wait_for(browser.get("/api/bootstrap"), timeout=3)).status_code == 200
        assert await asyncio.to_thread(entered.wait, 1)
        assert len(calls) == 1
    finally:
        release.set()
    async def ready():
        while True:
            data = (await browser.get("/api/bootstrap")).json()
            if data["module"].get("installed") and data["transcription"]["ready"]:
                return data
            await asyncio.sleep(.02)
    result = await asyncio.wait_for(ready(), timeout=3)
    assert result["module"]["size_gb"] == 2
    assert len(calls) == 1


async def test_bootstrap_reports_model_scan_failure_without_leaking_secrets(ui, monkeypatch):
    browser, service, _ = ui
    monkeypatch.setattr(service.module, "status", lambda: (_ for _ in ()).throw(ValueError("synthetic-token")))
    monkeypatch.setattr(service, "automation_status", lambda readiness=None: {"pending": 0, "ready": True, "reason": "Готова"})
    monkeypatch.setattr(service, "transcription_status", lambda: {"ready": True, "reason": ""})
    await browser.get("/?launch=synthetic-launch-token")
    async def failed():
        while True:
            data = (await browser.get("/api/bootstrap")).json()
            if data["module"].get("error"):
                return data
            await asyncio.sleep(.02)
    result = await asyncio.wait_for(failed(), timeout=3)
    assert "synthetic-token" not in str(result)
    assert "[hidden]" in result["module"]["error"]
    assert result["transcription"]["ready"] is False


async def test_settings_change_discards_inflight_model_result(ui, monkeypatch):
    browser, service, _ = ui
    entered, release = threading.Event(), threading.Event()
    calls = []

    def status():
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return {"installed": True, "size_gb": len(calls), "installing": False}

    monkeypatch.setattr(service.module, "status", status)
    monkeypatch.setattr(service, "transcription_status", lambda: {"ready": True, "reason": ""})
    monkeypatch.setattr(service, "automation_status", lambda readiness=None: {"pending": 0, "ready": True, "reason": "Готова"})
    await browser.get("/?launch=synthetic-launch-token")
    try:
        await browser.get("/api/bootstrap")
        assert await asyncio.to_thread(entered.wait, 1)
        service.settings.model = "small"
        response = await browser.get("/api/bootstrap")
        assert response.json()["module"]["checking"] is True
        assert response.json()["transcription"]["ready"] is False
    finally:
        release.set()
    async def refreshed():
        while True:
            result = (await browser.get("/api/bootstrap")).json()
            if result["module"].get("size_gb") == 2:
                return result
            await asyncio.sleep(.02)
    await asyncio.wait_for(refreshed(), timeout=3)
    assert len(calls) == 2
