"""Durable snapshots of successfully collected notice lists.

Every collected list URL owns two files under the data directory:

* ``<key>.json``        — the last *successful* complete notice list and its
                          collection time.  A failed run never touches it.
* ``<key>.status.json`` — failure metadata.  Its presence marks the list as
                          stale (旧数据), and it survives process restarts.

The notice file is replaced atomically, so a crash can never leave a
half-written list.  Loading a URL yields the last successful list with the
stale marker applied.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

SNAPSHOT_VERSION = 1
_KEY_PATTERN = re.compile(r"[0-9a-f]{32}")


@dataclass
class Snapshot:
    list_url: str
    status: str  # "current" | "stale"
    notices: list[dict]
    collected_at: str
    failed_at: str | None = None
    failure_reason: str | None = None
    failure_addresses: list[str] = field(default_factory=list)


def url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def is_key(value: str) -> bool:
    """True when *value* is a well-formed snapshot key."""
    return bool(_KEY_PATTERN.fullmatch(value))


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write(path: Path, document: dict, directory: Path) -> None:
    fd, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class SnapshotStore:
    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, url: str) -> Path:
        return self.directory / f"{url_key(url)}.json"

    def _status_path(self, url: str) -> Path:
        return self.directory / f"{url_key(url)}.status.json"

    def _read_status(self, url: str) -> dict | None:
        try:
            raw = self._status_path(url).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if data.get("list_url") != url:
            return None
        return data

    def load(self, url: str) -> Snapshot | None:
        """Last successful list for *url*, with the persisted stale marker."""
        path = self._path(url)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if data.get("version") != SNAPSHOT_VERSION or data.get("list_url") != url:
            return None
        status = self._read_status(url) or {}
        stale = bool(status.get("stale"))
        return Snapshot(
            list_url=url,
            status="stale" if stale else "current",
            notices=data.get("notices", []),
            collected_at=data.get("collected_at", ""),
            failed_at=status.get("failed_at") if stale else None,
            failure_reason=status.get("failure_reason") if stale else None,
            failure_addresses=list(status.get("failure_addresses") or []) if stale else [],
        )

    def save(self, url: str, notices: list[dict], collected_at: str | None = None) -> Snapshot:
        """Atomically persist a complete successful collection.

        A successful run clears any previous stale marker: the list is now
        the current truth.
        """
        collected_at = collected_at or _iso_now()
        document = {
            "version": SNAPSHOT_VERSION,
            "list_url": url,
            "collected_at": collected_at,
            "notices": notices,
        }
        path = self._path(url)
        with self._lock:
            _atomic_write(path, document, self.directory)
            try:
                os.unlink(self._status_path(url))
            except FileNotFoundError:
                pass
        return Snapshot(list_url=url, status="current", notices=notices,
                        collected_at=collected_at)

    def mark_failed(self, url: str, reason: str, addresses: list[str]) -> Snapshot | None:
        """Persist the stale marker; the successful list is left untouched."""
        snapshot = self.load(url)
        if snapshot is None:
            return None
        status = {
            "list_url": url,
            "stale": True,
            "failed_at": _iso_now(),
            "failure_reason": reason,
            "failure_addresses": list(addresses),
        }
        with self._lock:
            _atomic_write(self._status_path(url), status, self.directory)
        snapshot.status = "stale"
        snapshot.failed_at = status["failed_at"]
        snapshot.failure_reason = reason
        snapshot.failure_addresses = list(addresses)
        return snapshot

    def exists(self, url: str) -> bool:
        return self._path(url).is_file()

    def list_all(self) -> list[Snapshot]:
        """All saved lists, newest collection first."""
        snapshots: list[Snapshot] = []
        for path in sorted(self.directory.glob("*.json")):
            if path.name.endswith(".status.json"):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            url = data.get("list_url")
            if not url:
                continue
            snapshots.append(self.load(url))
        snapshots = [item for item in snapshots if item is not None]
        snapshots.sort(key=lambda item: item.collected_at, reverse=True)
        return snapshots
