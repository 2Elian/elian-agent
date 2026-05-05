"""
File history tracking — ported from utils/fileHistory.ts (1115 lines).

Tracks file state (mtime, content hash) at each user message boundary.
Used for:
  - Stale detection: detecting when a file was externally modified
  - Read-before-write enforcement: ensuring files were read before editing
  - Audit trail: knowing what state files were in at each turn
"""
from __future__ import annotations

import hashlib, os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class FileHistoryEntry:
    """Record of a file's state at a point in time."""
    file_path: str
    mtime: float  # Modification timestamp
    content_hash: str  # SHA256 of content
    size_bytes: int
    version: int = 0
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class FileHistorySnapshot:
    """Snapshot of file states at a message boundary."""
    message_id: str
    entries: dict[str, FileHistoryEntry] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class FileHistoryManager:
    """Tracks file state across conversation turns.

    On each user message, a snapshot is created recording the current state
    of all tracked files. Before Write/Edit operations, the current file
    state is compared against the last snapshot to detect external modifications.
    """

    def __init__(self, max_snapshots: int = 50):
        self._snapshots: list[FileHistorySnapshot] = []
        self._tracked_files: set[str] = set()
        self._max_snapshots = max_snapshots
        self._version_counter = 0

    def make_snapshot(self, message_id: str) -> FileHistorySnapshot:
        """Create a snapshot of all tracked files at this message boundary."""
        snapshot = FileHistorySnapshot(message_id=message_id)
        self._version_counter += 1

        for file_path in self._tracked_files:
            entry = self._stat_file(file_path)
            if entry:
                entry.version = self._version_counter
                snapshot.entries[file_path] = entry

        self._snapshots.append(snapshot)
        if len(self._snapshots) > self._max_snapshots:
            self._snapshots.pop(0)  # Evict oldest

        return snapshot

    def _stat_file(self, file_path: str) -> FileHistoryEntry | None:
        """Build a FileHistoryEntry for a file."""
        path = Path(file_path)
        if not path.exists():
            return None
        try:
            stat = path.stat()
            content = path.read_bytes()
            content_hash = hashlib.sha256(content).hexdigest()[:16]
            return FileHistoryEntry(
                file_path=str(path),
                mtime=stat.st_mtime,
                content_hash=content_hash,
                size_bytes=stat.st_size,
            )
        except (OSError, PermissionError):
            return None

    def track_file(self, file_path: str) -> None:
        """Add a file to tracking."""
        resolved = str(Path(file_path).resolve())
        self._tracked_files.add(resolved)

    def is_stale(self, file_path: str) -> bool:
        """Check if file was modified since the last snapshot.

        Returns True if file exists but current mtime differs from last snapshot.
        Returns False if file doesn't exist yet, no snapshots exist, or mtime matches.
        """
        path = Path(file_path)
        if not path.exists():
            return False  # New file, nothing to compare against

        current = self._stat_file(str(path))
        if not current:
            return False

        # Find last snapshot containing this file
        for snap in reversed(self._snapshots):
            entry = snap.entries.get(str(path))
            if entry:
                # Compare mtime and content hash
                return (entry.mtime != current.mtime or
                        entry.content_hash != current.content_hash)

        # No snapshot found — first time tracking this file
        return False

    def file_was_read(self, file_path: str) -> bool:
        """Check if a file was previously read (exists in any snapshot)."""
        resolved = str(Path(file_path).resolve())
        for snap in self._snapshots:
            if resolved in snap.entries:
                return True
        return False

    def get_last_entry(self, file_path: str) -> FileHistoryEntry | None:
        """Get the most recent entry for a file."""
        resolved = str(Path(file_path).resolve())
        for snap in reversed(self._snapshots):
            entry = snap.entries.get(resolved)
            if entry:
                return entry
        return None

    def clear(self) -> None:
        """Reset all history."""
        self._snapshots.clear()
        self._tracked_files.clear()
        self._version_counter = 0

    @property
    def snapshot_count(self) -> int:
        return len(self._snapshots)
