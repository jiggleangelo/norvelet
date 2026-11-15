import secrets
from collections import OrderedDict
from threading import RLock
from typing import Protocol


class Vault(Protocol):
    def store(self, kind: str, value: str) -> str: ...


class FakeVault:
    def __init__(self, max_values=500):
        self._values = OrderedDict()
        self._max_values = max_values
        self._lock = RLock()

    def store(self, kind, value):
        with self._lock:
            token = 'vlt_' + secrets.token_urlsafe(18)
            self._values[token] = (kind, value)
            while len(self._values) > self._max_values:
                self._values.popitem(last=False)
            return token

    def count(self):
        with self._lock:
            return len(self._values)
