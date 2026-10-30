"""In-memory mapping from adapter response IDs to Codex thread IDs."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable


class ResponseStore:
    """A small TTL + LRU store for previous_response_id continuation.

    This intentionally lives in process memory. Restarting the adapter, using
    multiple workers, or expiring an entry makes the corresponding Codex thread
    no longer reachable through ``previous_response_id``.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._records: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def put(self, response_id: str, thread_id: str) -> None:
        now = self._clock()
        self._purge_expired(now)
        self._records.pop(response_id, None)
        self._records[response_id] = (thread_id, now)
        while len(self._records) > self._max_entries:
            self._records.popitem(last=False)

    def get(self, response_id: str) -> str | None:
        now = self._clock()
        self._purge_expired(now)
        record = self._records.get(response_id)
        if record is None:
            return None
        thread_id, created_at = record
        if now - created_at >= self._ttl_seconds:
            self._records.pop(response_id, None)
            return None
        self._records.move_to_end(response_id)
        return thread_id

    def count(self) -> int:
        self._purge_expired(self._clock())
        return len(self._records)

    def clear(self) -> None:
        self._records.clear()

    def _purge_expired(self, now: float) -> None:
        expired: list[str] = []
        for response_id, (_, created_at) in self._records.items():
            if now - created_at >= self._ttl_seconds:
                expired.append(response_id)
        for response_id in expired:
            self._records.pop(response_id, None)
