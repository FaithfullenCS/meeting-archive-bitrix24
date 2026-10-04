from meeting_archive import browser
from pathlib import Path
import subprocess
import sys


def test_open_never_runs_powershell_or_keyboard_tab_search(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Opening the archive must never run a tab-search helper")

    monkeypatch.setattr(browser.subprocess, "run", forbidden)
    monkeypatch.setattr(browser, "controller", lambda: None)
    monkeypatch.setattr(browser.webbrowser, "open", lambda *args, **kwargs: True)
    browser.open_browser("http://localhost:8765/?launch=synthetic")


def test_repeated_launch_does_not_load_server_stack_before_instance_check():
    subprocess.run([sys.executable, "-c", "import sys; import meeting_archive.launcher; assert not any(name in sys.modules for name in ('uvicorn', 'fastapi', 'httpx'))"],
                   cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True)
