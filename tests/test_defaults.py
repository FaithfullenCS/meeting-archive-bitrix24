from pathlib import Path

from meeting_archive.settings import Settings
from meeting_archive import settings as settings_module


def test_new_profile_creates_generic_archive_and_incoming_with_auto_language(tmp_path):
    home = tmp_path / "profile"
    settings = Settings.load(home)
    assert settings.language == "auto"
    assert settings.oauth_relay == ""
    assert settings.oauth_flow == "local"
    assert not settings.auto_download and not settings.auto_local and not settings.watch_enabled
    assert Path(settings.archive_root).is_dir()
    assert Path(settings.watch_folder).is_dir()
    assert Path(settings.archive_root).name == "Совещания"
    assert Path(settings.watch_folder).name == "Входящие записи"
    assert not Path(settings.archive_root).is_relative_to(home)


def test_existing_archive_and_explicit_language_are_preserved(tmp_path):
    home = tmp_path / "profile"
    old = Settings(archive_root=str(tmp_path / "selected"), watch_folder=str(tmp_path / "incoming"), language="ru",
                   oauth_flow="relay")
    old.save(home)
    loaded = Settings.load(home)
    assert loaded.archive_root == old.archive_root
    assert loaded.watch_folder == old.watch_folder
    assert loaded.language == "ru"
    assert loaded.oauth_flow == "relay"
    assert not Path(loaded.archive_root).exists()


def test_frozen_first_run_uses_bundled_directories_and_preserves_explicit_paths(tmp_path, monkeypatch):
    executable = tmp_path / "distribution/MeetingArchive.exe"
    executable.parent.mkdir()
    monkeypatch.setattr(settings_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(settings_module.sys, "executable", str(executable))
    home = tmp_path / "profile"
    monkeypatch.setattr(settings_module, "app_home", lambda: home)
    loaded = Settings.load(home)
    assert Path(loaded.archive_root) == executable.parent / "Данные/Совещания"
    assert Path(loaded.watch_folder) == executable.parent / "Данные/Входящие записи"
    assert Path(loaded.archive_root).is_dir() and Path(loaded.watch_folder).is_dir()
    loaded.archive_root = str(tmp_path / "chosen")
    loaded.save(home)
    assert Settings.load(home).archive_root == loaded.archive_root
