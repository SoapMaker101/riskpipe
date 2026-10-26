"""HTTP для коннекторов: лимитер по хосту, ретраи, файловый кэш, журнал url.

Капчу не обходим: при капче коннектор получает Captcha, ждёт окно и пробует снова,
после исчерпания попыток канал помечается «не открылось».
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


class Unavailable(Exception):
    """Источник не ответил (сеть, 4xx/5xx, антибот)."""


class Captcha(Unavailable):
    """Источник требует капчу — не обходим."""


class Http:
    def __init__(self, min_interval: dict[str, float] | None = None, retries: int = 2, timeout: int = 30,
                 log=None):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.min_interval = min_interval or {}
        self.retries = retries
        self.timeout = timeout
        self.last: dict[str, float] = {}
        self.lock = threading.Lock()
        self.journal: list[dict] = []
        self.calls: dict[str, int] = {}
        self.log = log or (lambda *_: None)

    def _wait(self, host: str):
        gap = self.min_interval.get(host, 1.0)
        with self.lock:
            dt = time.monotonic() - self.last.get(host, 0)
            if dt < gap:
                time.sleep(gap - dt)
            self.last[host] = time.monotonic()

    def request(self, method: str, url: str, **kw) -> requests.Response:
        host = url.split("/")[2]
        err = None
        for attempt in range(self.retries + 1):
            self._wait(host)
            self.calls[host] = self.calls.get(host, 0) + 1
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
                self.journal.append({"t": datetime.now().isoformat(timespec="seconds"), "m": method,
                                     "url": url, "status": r.status_code})
                if r.status_code in (403, 451, 401, 429):      # 429: ждёт коннектор, не долбим источник
                    raise Unavailable(f"{host}: HTTP {r.status_code}")
                if r.status_code >= 500:
                    err = Unavailable(f"{host}: HTTP {r.status_code}")
                    time.sleep(2 * (attempt + 1))
                    continue
                return r
            except requests.RequestException as e:
                self.journal.append({"t": datetime.now().isoformat(timespec="seconds"), "m": method,
                                     "url": url, "status": f"ERR {type(e).__name__}"})
                err = Unavailable(f"{host}: {type(e).__name__}")
                time.sleep(2 * (attempt + 1))
        raise err or Unavailable(host)


class FileCache:
    """Кэш логических ответов коннектора: key -> JSON, со сроком годности."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, ns: str, key: str) -> Path:
        h = hashlib.sha1(key.encode()).hexdigest()[:16]
        safe = "".join(c for c in key if c.isalnum())[:40]
        return self.root / ns / f"{safe}_{h}.json"

    def get(self, ns: str, key: str, ttl_days: float):
        p = self._path(ns, key)
        if not p.exists():
            return None
        if datetime.now() - datetime.fromtimestamp(p.stat().st_mtime) > timedelta(days=ttl_days):
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def put(self, ns: str, key: str, value):
        p = self._path(ns, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
