from __future__ import annotations

from codex_responses_adapter.store import ResponseStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_put_and_get() -> None:
    clock = FakeClock()
    store = ResponseStore(ttl_seconds=60, max_entries=10, clock=clock)
    store.put("resp_1", "thread-1")
    assert store.get("resp_1") == "thread-1"
    assert store.get("missing") is None
    assert store.count() == 1


def test_ttl_expiry() -> None:
    clock = FakeClock()
    store = ResponseStore(ttl_seconds=10, max_entries=10, clock=clock)
    store.put("resp_1", "thread-1")
    clock.advance(10)
    assert store.get("resp_1") is None
    assert store.count() == 0


def test_lru_eviction() -> None:
    clock = FakeClock()
    store = ResponseStore(ttl_seconds=60, max_entries=2, clock=clock)
    store.put("a", "thread-a")
    store.put("b", "thread-b")
    assert store.get("a") == "thread-a"
    store.put("c", "thread-c")
    assert store.get("b") is None
    assert store.get("a") == "thread-a"
    assert store.get("c") == "thread-c"
    assert store.count() == 2


def test_clear() -> None:
    store = ResponseStore(ttl_seconds=60, max_entries=2, clock=FakeClock())
    store.put("a", "thread-a")
    store.clear()
    assert store.count() == 0
