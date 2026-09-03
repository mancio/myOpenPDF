from contextlib import contextmanager
from threading import Lock
from typing import Iterator

_registry_lock = Lock()
_keyed_locks: dict[str, Lock] = {}


def _get_lock(key: str) -> Lock:
    with _registry_lock:
        lock = _keyed_locks.get(key)
        if lock is None:
            lock = Lock()
            _keyed_locks[key] = lock
    return lock


@contextmanager
def keyed_lock(key: str) -> Iterator[None]:
    lock = _get_lock(key)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
