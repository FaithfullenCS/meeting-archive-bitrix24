from meeting_archive import browser
from pathlib import Path
import subprocess
import sys


def test_open_never_launches_external_browser(monkeypatch, tmp_path):
    import webbrowser
    def forbidden(*args, **kwargs):
        raise AssertionError("The archive must not launch an external browser")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(webbrowser, "open", forbidden)
    window = browser.configure_window(tmp_path)
    browser.open_browser("http://localhost:8765/?launch=synthetic")
    assert window.pending_open


def test_repeated_launch_does_not_load_server_stack_before_instance_check():
    subprocess.run([sys.executable, "-c", "import sys; import meeting_archive.launcher; assert not any(name in sys.modules for name in ('uvicorn', 'fastapi', 'httpx'))"],
                   cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True)
