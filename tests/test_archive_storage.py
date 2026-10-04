import hashlib
import json
import shutil
from pathlib import Path

from meeting_archive.archive import Archive


def text(value):
    return {"segments": [{"start": 0, "end": 2, "text": value}]}


def test_current_followup_is_not_copied_into_history(settings, meeting):
    archive = Archive(Path(settings.archive_root))
    folder = archive.ensure(meeting)
    archive.transcript(folder, text("First"))
    current = {p.name: p.read_bytes() for p in (folder / "bitrix").glob("transcript.*")}
    assert not list((folder / "bitrix/versions").glob("*/transcript.*"))
    archive.transcript(folder, text("Second"))
    history = list((folder / "bitrix/versions").iterdir())
    assert len(history) == 1
    assert {p.name:p.read_bytes() for p in history[0].iterdir()} == current
    archive.transcript(folder, text("First"))
    history = list((folder / "bitrix/versions").iterdir())
    assert len(history) == 1 and "Second" in (history[0] / "transcript.md").read_text("utf-8")


def test_old_current_copy_is_removed_but_previous_versions_and_notes_survive(settings, meeting):
    archive = Archive(Path(settings.archive_root))
    folder = archive.ensure(meeting)
    archive.transcript(folder, text("Previous"))
    archive.transcript(folder, text("Current"))
    target = folder / "bitrix"
    duplicate = target / "versions" / hashlib.sha256((target / "transcript.json").read_bytes()).hexdigest()[:16]
    duplicate.mkdir(exist_ok=True)
    for p in target.glob("transcript.*"):
        shutil.copy2(p, duplicate / p.name)
    note = folder / "notes/keep.md"
    note.write_text("Keep this", encoding="utf-8")
    assert len(archive.prune_current_copy(folder, dry_run=True)) == 3
    assert duplicate.exists()
    archive.transcript(folder, text("Current"))
    assert not duplicate.exists()
    assert len(list((target / "versions").iterdir())) == 1
    assert note.read_text("utf-8") == "Keep this"


def test_different_or_extra_history_files_are_never_pruned(settings, meeting):
    archive = Archive(Path(settings.archive_root))
    folder = archive.ensure(meeting)
    archive.transcript(folder, text("Current"))
    target = folder / "bitrix"
    duplicate = target / "versions" / hashlib.sha256((target / "transcript.json").read_bytes()).hexdigest()[:16]
    duplicate.mkdir(parents=True, exist_ok=True)
    for p in target.glob("transcript.*"):
        shutil.copy2(p, duplicate / p.name)
    extra = duplicate / "user-note.md"
    extra.write_text("User note", encoding="utf-8")
    assert archive.prune_current_copy(folder) == []
    extra.unlink()
    (duplicate / "transcript.md").write_text("Different text", encoding="utf-8")
    assert archive.prune_current_copy(folder) == []


def test_legacy_crlf_directory_name_is_recognized_without_rewriting_current(settings, meeting):
    archive = Archive(Path(settings.archive_root))
    folder = archive.ensure(meeting)
    archive.transcript(folder, text("Current"))
    target = folder / "bitrix"
    for path in target.glob("transcript.*"):
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    digest = hashlib.sha256((target / "transcript.json").read_text("utf-8").encode()).hexdigest()
    duplicate = target / "versions" / digest[:16]
    duplicate.mkdir(parents=True)
    for path in target.glob("transcript.*"):
        shutil.copy2(path, duplicate / path.name)
    original = {p.name:p.read_bytes() for p in target.glob("transcript.*")}
    archive.transcript(folder, text("Current"))
    assert not duplicate.exists()
    assert {p.name:p.read_bytes() for p in target.glob("transcript.*")} == original


def test_manifest_routes_markdown_and_notebook_after_archive_move(settings, meeting, tmp_path):
    archive = Archive(Path(settings.archive_root))
    folder = archive.ensure(meeting)
    archive.transcript(folder, text("Current"))
    for name, sample in (("20260101_full",False),("20260102_sample",True)):
        run = folder / "local" / name
        run.mkdir()
        (run / "run.json").write_text(json.dumps({"sample":sample}),encoding="utf-8")
        (run / "transcript.md").write_text("Local",encoding="utf-8")
    archive.manifest(meeting)
    relative = folder.relative_to(archive.root)
    moved = tmp_path / "Moved archive"
    shutil.copytree(archive.root,moved)
    manifest = json.loads((moved / relative / "meeting.json").read_text("utf-8"))
    reading = manifest["reading"]
    assert reading["format"] == "markdown"
    assert reading["local"] == "local/20260101_full/transcript.md"
    assert (moved / relative / reading["followup"]).is_file()
    assert (moved / relative / reading["notebook"]).resolve() == moved / "_notebook" / meeting["portal"]
