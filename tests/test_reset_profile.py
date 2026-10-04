import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows profile reset script")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/reset-profile.ps1"


def reset(home, mode="Profile", *extra, stdin=None):
    return subprocess.run(
        [shutil.which("powershell.exe"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
         "-ProfileHome", str(home), "-Mode", mode, *extra],
        input=stdin, capture_output=True, timeout=20,
    )


@pytest.fixture
def profile(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    archive = tmp_path / "archive"
    meeting = archive / "portal/2026/10/2026-10-04_120000_42"
    (meeting / "audio").mkdir(parents=True)
    (meeting / "audio/recording.wav").write_bytes(b"synthetic recording")
    (meeting / "meeting.json").write_text(json.dumps({"schemaVersion": 1, "source": "bitrix", "callId": 42}))
    (home / "settings.json").write_text(json.dumps({"archive_root": str(archive)}))
    for name in ("secrets.dpapi", "archive.sqlite", "archive.sqlite-wal", ".write-abcd"):
        (home / name).write_bytes(b"synthetic profile")
    (home / "uploads").mkdir()
    (home / "uploads/temp.wav").write_bytes(b"temporary import")
    (home / "module").mkdir()
    (home / "module/.meeting-archive-owned").write_text("Meeting Archive optional module")
    (home / "module/model.bin").write_bytes(b"synthetic model")
    (archive / "unrelated.txt").write_bytes(b"keep foreign files")
    original = tmp_path / "original.wav"
    original.write_bytes(b"original import source")
    return home, archive, meeting, original


def test_reset_profile_keeps_archive_models_and_originals(profile):
    home, archive, meeting, original = profile
    result = reset(home, "Profile", "-ConfirmReset")
    assert result.returncode == 0, result.stderr
    assert not (home / "settings.json").exists()
    assert not (home / "secrets.dpapi").exists()
    assert not (home / "archive.sqlite").exists()
    assert not (home / "uploads").exists()
    assert not (home / ".write-abcd").exists()
    assert (meeting / "audio/recording.wav").read_bytes() == b"synthetic recording"
    assert (home / "module/model.bin").exists()
    assert original.exists() and (archive / "unrelated.txt").exists()


def test_reset_all_removes_meetings_owned_module_but_keeps_foreign_files(profile):
    home, archive, meeting, original = profile
    result = reset(home, "All", "-ConfirmReset")
    assert result.returncode == 0, result.stderr
    assert not meeting.exists() and not (home / "module").exists()
    assert not (home / "secrets.dpapi").exists()
    assert (archive / "unrelated.txt").exists() and original.exists()


def test_preview_and_declined_confirmation_do_not_delete(profile):
    home, _, meeting, _ = profile
    assert reset(home, "All", "-Preview").returncode == 0
    assert reset(home, "Profile", stdin=b"no\n").returncode == 0
    assert (home / "secrets.dpapi").exists() and meeting.exists()


@pytest.mark.parametrize("choice,keep", [("1", True), ("2", False)])
def test_interactive_choice_controls_meeting_removal(profile, choice, keep):
    home, _, meeting, _ = profile
    result = reset(home, "Ask", stdin=(choice + "\nDELETE\n").encode())
    assert result.returncode == 0, result.stderr
    assert meeting.exists() is keep
    assert not (home / "settings.json").exists()


def test_all_does_not_delete_an_unmarked_module(profile):
    home, _, _, _ = profile
    (home / "module/.meeting-archive-owned").unlink()
    result = reset(home, "All", "-ConfirmReset")
    assert result.returncode == 0, result.stderr
    assert (home / "module/model.bin").exists()




def test_archive_junction_blocks_entire_plan(profile, tmp_path):
    home, archive, meeting, original = profile
    link = archive / "external"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(original.parent)], check=True, capture_output=True)
    try:
        result = reset(home, "All", "-ConfirmReset")
        assert result.returncode != 0
        assert (home / "secrets.dpapi").exists() and meeting.exists() and original.exists()
    finally:
        link.rmdir()  # Remove only the junction itself, never the referenced directory.
