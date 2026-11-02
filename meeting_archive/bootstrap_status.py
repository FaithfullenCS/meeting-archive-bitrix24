"""Non-blocking, single-flight model status for the initial interface."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict


class BootstrapModelStatus:
    def __init__(self, service):
        self.service = service
        self.lock = threading.Lock()
        self.key = None
        self.running = False
        self.finished_at = 0.0
        self.data = self.pending()

    @staticmethod
    def pending():
        return {
            "module": {"checking": True, "installed": False, "size_gb": 0, "installing": False},
            "transcription": {"checking": True, "ready": False, "reason": "Проверяем выбранную модель…"},
            "local_automation": {"checking": True, "ready": False, "pending": 0, "reason": "Проверяем состояние авторасшифровки…"},
        }

    def snapshot(self, jobs):
        # Changing settings or an installation job invalidates UI readiness.
        key = json.dumps([asdict(self.service.settings), self.service.module.discovery_at,
                          [(j["id"], j["state"]) for j in jobs if j["kind"] == "install"]], sort_keys=True)
        with self.lock:
            if key != self.key:
                self.key, self.data, self.finished_at = key, self.pending(), 0.0
            if not self.running and (not self.finished_at or time.monotonic() - self.finished_at >= 300):
                self.running = True
                threading.Thread(target=self._refresh, args=(key,), name="BootstrapModelStatus", daemon=True).start()
            return {name: dict(value) for name, value in self.data.items()}

    def _refresh(self, key):
        try:
            transcription = self.service.transcription_status()
            automatic = self.service.automation_status(readiness=transcription)
            with self.lock:
                if self.key == key:
                    self.data.update(transcription=transcription, local_automation=automatic)
            module = self.service.module.status()
            with self.lock:
                if self.key == key:
                    self.data["module"] = module
        except Exception as exc:
            error = self.service.vault.redact(str(exc))
            with self.lock:
                if self.key == key:
                    self.data = {
                        "module": {"checking": False, "installed": False, "size_gb": 0, "installing": False, "error": error},
                        "transcription": {"checking": False, "ready": False, "reason": "Не удалось проверить модель: " + error},
                        "local_automation": {"checking": False, "ready": False, "pending": 0, "reason": "Не удалось проверить авторасшифровку: " + error},
                    }
        finally:
            with self.lock:
                if self.key == key:
                    self.finished_at = time.monotonic()
                self.running = False
