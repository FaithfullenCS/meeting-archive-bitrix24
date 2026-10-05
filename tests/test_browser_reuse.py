from types import SimpleNamespace

import httpx
import pytest

from meeting_archive import browser
from meeting_archive.app import create_app
from meeting_archive.service import Service


@pytest.fixture
async def service(tmp_path, settings, vault):
    home = tmp_path / "home"
    settings.save(home)
    instance = Service(home, vault=vault)
    yield instance
    await instance.client.close()
    instance.db.close()


def window(found=None):
    calls = []
    native = SimpleNamespace(valid=lambda handle: handle == 42, find=lambda **kw: found,
                             activate=lambda handle: calls.append(("focus", handle)))
    controller = browser.ArchiveWindow(native, "installed-browser.exe", lambda argv, **kw: calls.append(("launch", argv)), clock=lambda: 100)
    return controller, calls


def test_existing_window_directly_focuses_without_launch_or_tab_search():
    controller, calls = window(42)
    controller.open("http://localhost:8765/?launch=synthetic")
    # A cached handle must not enumerate windows again.
    controller.native.find = lambda **kw: pytest.fail("Unexpected window search")
    controller.open("http://localhost:8765/?launch=synthetic")
    assert calls == [("focus", 42), ("focus", 42)]


def test_closed_window_launches_immediately_and_repeated_clicks_do_not_duplicate():
    controller, calls = window()
    url = "http://localhost:8765/?launch=synthetic"
    controller.open(url)
    controller.open(url)
    assert calls == [("launch", ["installed-browser.exe", "--app=" + url])]
    controller.native.find = lambda **kw: 42
    controller.register()
    controller.open(url)
    assert calls[-1] == ("focus", 42)


def test_window_closed_after_registration_is_reopened():
    controller, calls = window(42)
    controller.register()
    controller.native.valid = lambda handle: False
    controller.native.find = lambda **kw: None
    controller.open("http://localhost:8765/?launch=synthetic")
    assert calls[0][0] == "launch"


def test_missing_app_browser_uses_shell_without_search(monkeypatch):
    monkeypatch.setattr(browser, "controller", lambda: None)
    opened = []
    monkeypatch.setattr(browser.webbrowser, "open", lambda url, **_: opened.append(url))
    url = "http://localhost:8765/?launch=synthetic"
    browser.open_browser(url)
    assert opened == [url]


def test_native_registration_does_not_touch_unrelated_browser_windows():
    properties, activations = {}, []
    titles = {1: "Meeting Archive - Yandex Browser", 2: "Meeting Archive", 3: "Other app"}

    def enumerate_windows(callback, _):
        for handle in titles:
            if not callback(handle, 0):
                break

    native = browser.WindowsWindow.__new__(browser.WindowsWindow)
    native.callback_type = lambda callback: callback
    native.api = SimpleNamespace(
        IsWindow=lambda handle: handle in titles,
        IsWindowVisible=lambda handle: True,
        GetPropW=lambda handle, name: properties.get(handle),
        GetWindowTextW=lambda handle, buffer, length: setattr(buffer, "value", titles[handle]),
        GetClassNameW=lambda handle, buffer, length: setattr(buffer, "value", "Chrome_WidgetWin_1"),
        SetPropW=lambda handle, name, value: properties.setdefault(handle, value),
        EnumWindows=enumerate_windows,
        IsIconic=lambda handle: True,
        ShowWindowAsync=lambda handle, mode: activations.append(("restore", handle)),
        SetForegroundWindow=lambda handle: False,
        FlashWindow=lambda handle, flag: activations.append(("indicate", handle)),
    )
    assert native.find(register=True) == 2
    assert properties == {2: 1}
    assert not activations
    native.activate(2)
    assert activations == [("restore", 2), ("indicate", 2)]


@pytest.mark.asyncio
async def test_window_registration_requires_session_and_csrf(service, monkeypatch):
    registered = []
    monkeypatch.setattr(browser, "register_window", lambda: registered.append(True))
    app = create_app(service, "synthetic-window", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765") as client:
        assert (await client.post("/api/browser/ready")).status_code == 401
        await client.get("/?launch=synthetic-window")
        assert (await client.post("/api/browser/ready")).status_code == 403
        csrf = (await client.get("/api/bootstrap")).json()["csrf"]
        assert (await client.post("/api/browser/ready", headers={"X-CSRF-Token": csrf})).status_code == 200
        assert registered == [True]


@pytest.mark.asyncio
async def test_surviving_window_reconnects_after_server_restart_but_profile_reset_revokes_session(service):
    app = create_app(service, "old-launch", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765") as client:
        await client.get("/?launch=old-launch")
        cookies = client.cookies
        old_csrf = (await client.get("/api/bootstrap")).json()["csrf"]
    app = create_app(service, "new-launch", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765", cookies=cookies) as client:
        data = (await client.get("/api/bootstrap")).json()
        assert data["csrf"] != old_csrf
        assert service.vault.read()["ui_session"] not in str(data)
        assert (await client.post("/api/settings", json={"paused": True}, headers={"X-CSRF-Token": old_csrf})).status_code == 403
        assert (await client.get("/?launch=old-launch")).status_code == 200  # Existing valid session only.
    service.vault.write({})  # Simulated uninstall of the synthetic profile.
    app = create_app(service, "reset-launch", manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765", cookies=cookies) as client:
        assert (await client.get("/api/bootstrap")).status_code == 401


def test_isolated_profile_launch_debounces_slow_browser_start(tmp_path):
    calls=[]
    native=SimpleNamespace(valid=lambda _:False,find=lambda **_:None)
    clock=[0]
    window=browser.ArchiveWindow(native,"browser.exe",lambda argv,**_:calls.append(argv),clock=lambda:clock[0],profile=tmp_path/"browser-window")
    window.open("http://localhost:8765/?launch=synthetic")
    clock[0]=2
    window.open("http://localhost:8765/?launch=synthetic")
    assert len(calls)==1
    assert "--user-data-dir="+str(tmp_path/"browser-window") in calls[0]


def test_owned_profile_window_with_browser_suffix_is_reused_and_closed_without_shared_browser(tmp_path):
    properties,closed={},[]
    alive={1,2}
    native=browser.WindowsWindow.__new__(browser.WindowsWindow)
    native.profile=tmp_path/"own-browser"
    native.owned_processes=lambda:{100}
    native.callback_type=lambda fn:fn
    def enumerate_windows(callback,_):
        for hwnd in list(alive):
            if not callback(hwnd,0):
                break
    def close(hwnd,*_):
        closed.append(hwnd)
        alive.remove(hwnd)
        return True
    native.api=SimpleNamespace(IsWindow=lambda hwnd:hwnd in alive,IsWindowVisible=lambda _:True,
        GetPropW=lambda hwnd,_:properties.get(hwnd),SetPropW=lambda hwnd,_,value:properties.setdefault(hwnd,value),
        GetWindowThreadProcessId=lambda hwnd,pid:setattr(pid._obj,"value",100 if hwnd==1 else 200),
        GetWindowTextW=lambda hwnd,buffer,_:setattr(buffer,"value","Meeting Archive - Browser"),
        GetClassNameW=lambda hwnd,buffer,_:setattr(buffer,"value","Chrome_WidgetWin_1"),
        EnumWindows=enumerate_windows,PostMessageW=close)
    assert native.find(register=True)==1
    window=browser.ArchiveWindow(native,"browser.exe",profile=native.profile)
    assert window.close()
    assert closed==[1] and alive=={2}


@pytest.mark.asyncio
async def test_desktop_open_uses_capability_and_running_controller(service,monkeypatch):
    calls=[]
    monkeypatch.setattr(browser,"open_browser",lambda url:calls.append(url))
    app=create_app(service,"desktop-test-token",manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://localhost:8765") as client:
        assert (await client.post("/api/desktop/open")).status_code==403
        headers={"x-desktop-token":"desktop-test-token"}
        assert (await client.post("/api/desktop/open",headers={**headers,"origin":"http://localhost:8765"})).status_code==403
        assert (await client.post("/api/desktop/open",headers=headers,json={"url":"https://untrusted.test"})).status_code==200
    assert calls==["http://localhost:8765/?launch=desktop-test-token"]


def test_profile_process_identity_rejects_other_profile_and_executable(tmp_path, monkeypatch):
    import psutil
    executable = tmp_path / "browser.exe"
    profile = tmp_path / "own-profile"
    def process(pid, exe, folder):
        return SimpleNamespace(info={"pid": pid, "exe": str(exe), "cmdline": [str(exe), "--user-data-dir=" + str(folder)]},
                               children=lambda **_: [SimpleNamespace(pid=pid + 10)])
    monkeypatch.setattr(psutil, "process_iter", lambda _: [
        process(1, executable, profile), process(2, executable, tmp_path / "shared"),
        process(3, tmp_path / "different.exe", profile)])
    native = browser.WindowsWindow.__new__(browser.WindowsWindow)
    native.profile, native.executable = profile, executable
    assert native.owned_processes() == {1, 11}


def test_refused_window_close_does_not_report_success(tmp_path):
    native = SimpleNamespace(find=lambda **_: 1, close=lambda _: False)
    window = browser.ArchiveWindow(native, "browser.exe", profile=tmp_path / "own-profile")
    assert not window.close()
