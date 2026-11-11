import json
import pathlib
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Any


class ObjectCache:

    def __init__(self, path: pathlib.Path | None = None, ttl: float = 86400,
                 capacity: int = 2048) -> None:
        self.ttl = ttl
        self.capacity = capacity
        self._memory: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self.hits = self.misses = 0
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("CREATE TABLE IF NOT EXISTS objects "
                             "(key TEXT PRIMARY KEY, fetched REAL NOT NULL, body TEXT NOT NULL)")

    def get(self, key: str) -> tuple[bool, Any]:
        with self._lock:
            entry = self._memory.get(key)
            if entry is None and self._db is not None:
                row = self._db.execute("SELECT fetched,body FROM objects WHERE key=?", (key,)).fetchone()
                if row is not None:
                    entry = (row[0], json.loads(row[1]))
            if entry is not None and time.time() - entry[0] < self.ttl:
                self._remember(key, entry)
                self.hits += 1
                return True, entry[1]
            self._memory.pop(key, None)
            self.misses += 1
            return False, None

    def _remember(self, key: str, entry: tuple[float, Any]) -> None:
        self._memory[key] = entry
        self._memory.move_to_end(key)
        while len(self._memory) > self.capacity:
            self._memory.popitem(last=False)

    def put_many(self, values: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            if self._db is not None:
                with self._db:
                    self._db.executemany("INSERT OR REPLACE INTO objects VALUES(?,?,?)",
                                         ((k, now, json.dumps(v)) for k, v in values.items()))
            for key, value in values.items():
                self._remember(key, (now, value))

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
