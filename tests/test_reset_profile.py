import json
import hashlib
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
    chats = tmp_path / "chats"
    chat = chats / "synthetic.bitrix24.ru/user-42/chats/conversations/chat-101"
    (chat / "messages").mkdir(parents=True)
    (chat / "messages/2026-10.md").write_text("Synthetic message", "utf-8")
    (chat / "chat.json").write_text(json.dumps({"schemaVersion": 1, "id": 101, "user_id": 42, "portal": "synthetic.bitrix24.ru"}))
    (chats / "archive.json").write_text(json.dumps({"schemaVersion": 1, "kind": "chat-archive"}))
    (chats / "unrelated.txt").write_text("Synthetic unrelated file")
    (home / "settings.json").write_text(json.dumps({"archive_root": str(archive), "chat_archive_root": str(chats)}))
    (home / ".meeting-archive-profile.json").write_text(json.dumps({"schemaVersion": 1, "product": "MeetingArchive"}))
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
    assert result.returncode == 0, result.stdout + result.stderr
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
    assert result.returncode == 0, result.stdout + result.stderr
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
    assert result.returncode == 0, result.stdout + result.stderr
    assert meeting.exists() is keep
    assert not (home / "settings.json").exists()


def test_all_does_not_delete_an_unmarked_module(profile):
    home, _, _, _ = profile
    (home / "module/.meeting-archive-owned").unlink()
    result = reset(home, "All", "-ConfirmReset")
    assert result.returncode == 0, result.stdout + result.stderr
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


def test_reset_all_includes_chats_preserves_foreign_files(profile):
    home, archive, meeting, _ = profile
    chat_root = Path(json.loads((home / "settings.json").read_text())["chat_archive_root"])
    chat = chat_root / "synthetic.bitrix24.ru/user-42/chats/conversations/chat-101"
    result = reset(home, "All", "-ConfirmReset")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not chat.exists() and not meeting.exists()
    assert (chat_root / "unrelated.txt").exists() and (archive / "unrelated.txt").exists()


def test_custom_components_can_remove_chats_without_profile_or_meetings(profile):
    home, _, meeting, _ = profile
    chat_root = Path(json.loads((home / "settings.json").read_text())["chat_archive_root"])
    result = reset(home, "Custom", "-RemoveChats", "-ConfirmReset")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / "settings.json").exists() and meeting.exists()
    assert not list(chat_root.rglob("chat.json"))


def test_program_removal_uses_manifest_keeps_changed_and_foreign_files(profile, tmp_path):
    home, _, meeting, _ = profile
    app = tmp_path / "application"
    (app / "_internal").mkdir(parents=True)
    files = {"MeetingArchive.exe": b"synthetic executable", "_internal/runtime.dll": b"synthetic dll", "README.md": b"original instructions"}
    for name, data in files.items():
        (app / name).write_bytes(data)
    manifest = {"version": "0.0.0", "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    (app / "update-manifest.json").write_text(json.dumps(manifest))
    (app / "README.md").write_text("Synthetic local changes")
    (app / "unrelated.txt").write_text("Keep this")
    report = tmp_path / "result.json"
    result = reset(home, "Custom", "-RemoveApplication", "-ApplicationRoot", str(app), "-ReportPath", str(report), "-ConfirmReset")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (app / "MeetingArchive.exe").exists() and not (app / "_internal/runtime.dll").exists()
    assert (app / "README.md").read_text() == "Synthetic local changes"
    assert (app / "unrelated.txt").exists() and meeting.exists() and (home / "settings.json").exists()
    summary = json.loads(report.read_text("utf-8-sig"))
    assert summary["completed"] and summary["preserved"]


def test_manifest_traversal_blocks_before_profile_deletion(profile, tmp_path):
    home, _, meeting, _ = profile
    app = tmp_path / "application"
    app.mkdir()
    (app / "update-manifest.json").write_text(json.dumps({"files": {"MeetingArchive.exe": "0"*64, "../foreign.txt": "0"*64}}))
    result = reset(home, "Custom", "-RemoveProfile", "-RemoveApplication", "-ApplicationRoot", str(app), "-ConfirmReset")
    assert result.returncode != 0 and (home / "settings.json").exists() and meeting.exists()


def test_custom_unknown_profile_is_not_implicitly_owned(tmp_path):
    home = tmp_path / "foreign"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"other_product": True}))
    result = reset(home, "Profile", "-ConfirmReset")
    assert result.returncode != 0 and (home / "settings.json").exists()


def test_changed_primary_executable_blocks_program_and_profile_removal(profile, tmp_path):
    home, _, meeting, _ = profile
    app = tmp_path / "application"
    app.mkdir()
    (app / "MeetingArchive.exe").write_bytes(b"modified executable")
    (app / "update-manifest.json").write_text(json.dumps({"files": {"MeetingArchive.exe": hashlib.sha256(b"original executable").hexdigest()}}))
    result = reset(home, "Custom", "-RemoveProfile", "-RemoveApplication", "-ApplicationRoot", str(app), "-ConfirmReset")
    assert result.returncode != 0 and (home / "settings.json").exists() and meeting.exists()


def test_uninstaller_scripts_parse_in_windows_powershell():
    script = r'''$parseErrors=$null; $parseTokens=$null; [System.Management.Automation.Language.Parser]::ParseFile('SOURCE',[ref]$parseTokens,[ref]$parseErrors)|Out-Null; if($parseErrors.Count){$parseErrors|ForEach-Object{$_.Message};exit 1}'''
    for source in (SCRIPT, SCRIPT.with_name("uninstall-ui.ps1")):
        result = subprocess.run([shutil.which("powershell.exe"), "-NoProfile", "-Command", script.replace("SOURCE", str(source).replace("\'", "\'\'"))], capture_output=True)
        assert result.returncode == 0, result.stdout + result.stderr
