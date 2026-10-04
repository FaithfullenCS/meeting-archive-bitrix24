from __future__ import annotations

import copy
import json
import shutil
import time
import uuid
from pathlib import Path

import pytest

from meeting_archive.settings import Settings
from meeting_archive.db import Database


@pytest.fixture(autouse=True)
def isolated_discovery(monkeypatch):
    # Tests must not execute Python environments on the developer's computer.
    monkeypatch.setattr("meeting_archive.resource_reuse.environment_candidates", lambda: [])
    monkeypatch.setattr("meeting_archive.resource_reuse._cache_root", lambda: Path(__file__).parent / "absent-model-cache")


@pytest.fixture(autouse=True)
def isolated_hardware(monkeypatch):
    # Installer tests must produce the same result on GPU and CPU CI hosts.
    def fake():
        return {"architecture": "x64", "gpu": {"name": "Synthetic GPU", "compute_capability": "12.0", "driver": "600.00"}}
    monkeypatch.setattr("meeting_archive.hardware.detect", fake)
    monkeypatch.setattr("meeting_archive.app.detect", fake)
    monkeypatch.setattr("meeting_archive.hardware.native_architecture", lambda: "x64")


class MemoryVault:
    def __init__(self, **values):
        self.values = values

    def read(self):
        return copy.deepcopy(self.values)

    def write(self, values):
        self.values = copy.deepcopy(values)

    def update(self, **values):
        self.values.update(values)

    def redact(self, value):
        for key in ("access_token", "refresh_token", "client_secret", "webhook", "hf_token", "ui_session"):
            secret = self.values.get(key)
            if secret:
                value = value.replace(str(secret), "[hidden]")
        return value


@pytest.fixture
def tmp_path():
    # Python 3.12's Windows mkdir(mode=0o700) replaces inherited ACLs, preventing
    # the restricted local test process from reopening pytest's normal temp dirs.
    # Use inherited ACLs and keep every synthetic fixture inside this project.
    root = Path(__file__).resolve().parents[1] / ".work" / "test-runtime"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True)
    yield path
    if path.resolve().is_relative_to(root.resolve()) and path.name:
        shutil.rmtree(path)


@pytest.fixture(autouse=True)
def close_databases(monkeypatch):
    instances = []
    original = Database.__init__

    def create(instance, *args, **kwargs):
        original(instance, *args, **kwargs)
        instances.append(instance)

    monkeypatch.setattr(Database, "__init__", create)
    yield
    for instance in instances:
        instance.close()


@pytest.fixture
def vault():
    return MemoryVault(access_token="synthetic-token", refresh_token="synthetic-refresh",
                       client_id="synthetic-id", client_secret="synthetic-secret",
                       expires_at=time.time() + 3600)


@pytest.fixture
def settings(tmp_path):
    return Settings(archive_root=str(tmp_path / "archive"), portal="synthetic.bitrix24.ru", user_id=41)


@pytest.fixture
def meeting():
    return {"id": 1, "portal": "synthetic.bitrix24.ru", "call_id": "123", "uuid": "session-123",
            "metadata": json.dumps({"callId": 123, "uuid": "session-123", "startDate": "2026-10-01T09:00:00+03:00",
                                    "overview": {"topic": "Synthetic meeting"}, "participants": [{"userId": 41}]}),
            "folder": "", "source": "bitrix", "audio": "not_saved", "bitrix": "not_saved", "local": "not_saved"}
