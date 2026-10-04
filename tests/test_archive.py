from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from meeting_archive.archive import Archive, clean_metadata, sha256
from meeting_archive.bitrix import BitrixClient, BitrixError


@pytest.mark.asyncio
async def test_download_atomic_manifest_dedup_and_no_secret_url(settings, vault, meeting):
    archive = Archive(Path(settings.archive_root))
    folder = archive.ensure(meeting)
    content = b"synthetic recording bytes"
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=content, headers={"content-type": "audio/webm"})

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    track = {"trackId": "track-1", "fileName": "voice.webm", "fileSize": len(content),
             "url": "https://synthetic.bitrix24.ru/recording?auth=synthetic-download-token"}
    try:
        path, changed = await archive.download(client, folder, track, lambda *_: None)
        assert changed and path.read_bytes() == content
        assert not list((folder / "audio").glob(".download-*"))
        record = json.loads((folder / "meeting.json").read_text("utf-8"))["files"][path.relative_to(folder).as_posix()]
        assert record["sha256"] == sha256(path)
        assert record["size"] == len(content)
        assert await archive.download(client, folder, track, lambda *_: None) == (path, False)
        assert len(calls) == 1
    finally:
        await client.close()
    text = (folder / "meeting.json").read_text("utf-8")
    assert "synthetic-download-token" not in text
    assert "recording?auth" not in text


@pytest.mark.asyncio
async def test_same_size_corruption_is_downloaded_again(settings, vault, meeting):
    archive = Archive(Path(settings.archive_root))
    folder = archive.ensure(meeting)
    content = b"good recording"
    client = BitrixClient(settings, vault, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content)))
    track = {"trackId": 1, "fileName": "voice.webm", "fileSize": len(content), "relUrl": "/recording"}
    try:
        path, _ = await archive.download(client, folder, track, lambda *_: None)
        original_stat = path.stat()
        path.write_bytes(b"x" * len(content))
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        assert path.stat().st_size == original_stat.st_size
        assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
        _, changed = await archive.download(client, folder, track, lambda *_: None)
        assert changed
        assert path.read_bytes() == content
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["mismatch", "timeout", "html", "unsafe_redirect", "length_mismatch"])
async def test_failed_download_preserves_original_and_cleans_temporary_file(settings, vault, meeting, failure):
    archive = Archive(Path(settings.archive_root))
    folder = archive.ensure(meeting)
    target = folder / "audio/track_voice.webm"
    target.write_bytes(b"previous original")

    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if failure == "html":
            return httpx.Response(200, content=b"login", headers={"content-type": "text/html"})
        if failure == "unsafe_redirect":
            return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
        return httpx.Response(200, content=b"short", headers={"content-length": "999"} if failure == "length_mismatch" else {})

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    track = {"trackId": "track", "fileName": "voice.webm", "fileSize": 0 if failure == "length_mismatch" else 20,
             "relUrl": "/recording"}
    try:
        with pytest.raises((BitrixError, httpx.ReadTimeout)):
            await archive.download(client, folder, track, lambda *_: None)
    finally:
        await client.close()
    assert target.read_bytes() == b"previous original"
    assert not list((folder / "audio").glob(".download-*"))


@pytest.mark.asyncio
async def test_relative_redirect_uses_current_recording_directory(settings, vault, meeting):
    archive = Archive(Path(settings.archive_root))
    folder = archive.ensure(meeting)
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if len(urls) == 1:
            return httpx.Response(302, headers={"location": "final.webm"})
        return httpx.Response(200, content=b"recording")

    client = BitrixClient(settings, vault, transport=httpx.MockTransport(handler))
    try:
        await archive.download(client, folder, {"trackId": 1, "relUrl": "/nested/start.webm"}, lambda *_: None)
    finally:
        await client.close()
    assert urls == ["https://synthetic.bitrix24.ru/nested/start.webm", "https://synthetic.bitrix24.ru/nested/final.webm"]


def test_transcript_versions_preserve_notes_and_unchanged_version(settings, meeting):
    archive = Archive(Path(settings.archive_root))
    folder = archive.ensure(meeting)
    note = folder / "notes/private.md"
    note.write_text("Synthetic manual notes", encoding="utf-8")
    first = {"segments": [{"start": 0, "end": 2, "userName": "Test Person", "userId": 41, "text": "Первый текст"}]}
    second = {"segments": [{"start": 0, "end": 2, "userName": "Test Person", "userId": 41, "text": "Обновлённый текст"}]}
    assert archive.transcript(folder, first)
    assert archive.transcript(folder, first)
    assert not list((folder / "bitrix/versions").glob("*/transcript.json"))
    assert archive.transcript(folder, second)
    assert len(list((folder / "bitrix/versions").iterdir())) == 1
    assert "Обновлённый текст" in (folder / "bitrix/transcript.md").read_text("utf-8")
    assert note.read_text("utf-8") == "Synthetic manual notes"


def test_secret_transport_fields_are_not_archived(settings, meeting):
    archive = Archive(Path(settings.archive_root))
    folder = archive.ensure(meeting)
    transcript = {"segments": [{"start": 0, "end": 1, "text": "Synthetic speech", "userId": 41}],
                  "downloadUrl": "https://synthetic.bitrix24.ru/file?auth=synthetic-download-secret", "auth": "synthetic-auth-secret"}
    archive.transcript(folder, transcript)
    text = (folder / "bitrix/transcript.json").read_text("utf-8")
    assert "synthetic-download-secret" not in text
    assert "synthetic-auth-secret" not in text


def test_metadata_excludes_media_and_text():
    cleaned = clean_metadata({"callId": 123, "uuid": "session", "participants": [{"userId": 41, "name": "Test", "url": "secret"}],
                              "tracks": [{"url": "https://synthetic.bitrix24.ru/?auth=secret"}],
                              "transcription": {"segments": [{"text": "speech"}]}, "overview": {"topic": "Topic", "summary": "private"}})
    assert "tracks" not in cleaned and "transcription" not in cleaned
    assert "summary" not in cleaned["overview"]
    assert "url" not in cleaned["participants"][0]


def test_saved_folder_stays_stable_after_uuid_or_title_hydration(settings, meeting):
    archive = Archive(Path(settings.archive_root))
    folder = archive.ensure(meeting)
    meeting.update(folder=str(folder), uuid="changed-session")
    metadata = json.loads(meeting["metadata"])
    metadata["overview"]["topic"] = "New title"
    meeting["metadata"] = json.dumps(metadata)
    assert archive.ensure(meeting) == folder


def test_import_is_copy_deduplicated_and_source_preserved(settings, meeting, tmp_path):
    archive = Archive(Path(settings.archive_root))
    source = tmp_path / "voice.wav"
    source.write_bytes(b"synthetic audio")
    first, changed = archive.import_file(meeting, source)
    assert changed and first.read_bytes() == source.read_bytes()
    assert archive.import_file(meeting, source) == (first, False)
    assert source.read_bytes() == b"synthetic audio"
    assert not list(first.parent.glob(".import-*"))


@pytest.mark.skipif(os.name != "nt", reason="Native Windows junction regression")
@pytest.mark.parametrize("linked_directory", ["audio", "bitrix", "local"])
def test_archive_rejects_child_junctions_outside_root(settings, meeting, tmp_path, linked_directory):
    import _winapi
    archive = Archive(Path(settings.archive_root))
    folder = archive.ensure(meeting)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "preserve.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    linked = folder / linked_directory
    linked.rmdir()
    _winapi.CreateJunction(str(external), str(linked))
    try:
        with pytest.raises(ValueError):
            archive.ensure(meeting)
        assert sentinel.read_text("utf-8") == "preserve"
        assert not (external / "transcript.json").exists()
    finally:
        os.rmdir(linked)
