"""Small JSON stores for webhook deliveries and approved edit plans."""

from __future__ import annotations

import json
import threading
from pathlib import Path


class DeliveryStore:
    def __init__(self, path: Path, limit: int = 5000) -> None:
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()
        self._ids: list[str] = []
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, list):
            self._ids = [str(item) for item in data][-self.limit :]
            self._seen = set(self._ids)

    def seen(self, delivery_id: str) -> bool:
        with self._lock:
            return delivery_id in self._seen

    def mark(self, delivery_id: str) -> None:
        with self._lock:
            if delivery_id in self._seen:
                return
            self._ids.append(delivery_id)
            self._seen.add(delivery_id)
            overflow = len(self._ids) - self.limit
            if overflow > 0:
                dropped = self._ids[:overflow]
                self._ids = self._ids[overflow:]
                for item in dropped:
                    self._seen.discard(item)
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._ids), encoding="utf-8")


class PlanStore:
    """Remembers a preview so `/caya fix` can apply that exact edit."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._plans: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            self._plans = {str(key): value for key, value in data.items() if isinstance(value, dict)}

    def get(self, key: str) -> dict[str, str] | None:
        with self._lock:
            value = self._plans.get(key)
            return dict(value) if value else None

    def put(self, key: str, plan: dict[str, str]) -> None:
        with self._lock:
            self._plans[key] = dict(plan)
            self._save()

    def delete(self, key: str) -> None:
        with self._lock:
            if key not in self._plans:
                return
            del self._plans[key]
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._plans, indent=2), encoding="utf-8")
