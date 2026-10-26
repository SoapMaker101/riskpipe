"""Заголовки СМИ без ключа: RSS Google News за последний год (вместо Яндекс Search API с лимитом 300/сутки).

Сам по себе поиск по имени даёт мусор (тёзки), поэтому заголовки идут в llm/media_classify,
который сверяет компанию по ИНН/городу/отрасли; выход — только гипотезы (media).
"""
from __future__ import annotations

import html as H
import re
import urllib.parse

from core.http import FileCache, Http, Unavailable

OPF = r"^(ООО|АО|ПАО|ЗАО|ОАО|НАО|МУП|ГУП|ФГУП|ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ|АКЦИОНЕРНОЕ ОБЩЕСТВО)\s+"


def core_name(name: str) -> str:
    s = re.sub(r"[«»\"“”]", " ", name or "")
    s = re.sub(OPF, "", s.strip(), flags=re.I)
    s = re.sub(r"\s+г\.\s*\S+$", "", s)          # «МУП "Водоканал" г. Энска»
    s = re.sub(r"\s+", " ", s).strip()
    if s.isupper():
        s = " ".join(w if len(w) <= 4 else w.capitalize() for w in s.split())
    return s


def city_of(region: str | None) -> str:
    r = (region or "").upper()
    return {"СВЕРДЛОВСКАЯ": "Екатеринбург", "МОСКВА": "Москва", "САНКТ-ПЕТЕРБУРГ": "Петербург"}.get(
        next((k for k in ("СВЕРДЛОВСКАЯ", "МОСКВА", "САНКТ-ПЕТЕРБУРГ") if k in r), ""), "")


class GoogleNews:
    def __init__(self, http: Http, cache: FileCache, interval: float = 2.0):
        self.http = http
        self.cache = cache
        self.http.min_interval.setdefault("news.google.com", interval)

    def headlines(self, name: str, region: str | None = None, limit: int = 10) -> list[dict]:
        core = core_name(name)
        q = f'"{core}"' + (f" {city_of(region)}" if len(core) <= 8 and city_of(region) else "") + " when:1y"
        hit = self.cache.get("news", q, 7)
        if hit is None:
            url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=ru&gl=RU&ceid=RU:ru"
            r = self.http.request("GET", url)
            if r.status_code != 200:
                raise Unavailable(f"news.google.com: HTTP {r.status_code}")
            hit = []
            for it in re.findall(r"<item>(.*?)</item>", r.text, re.S)[:limit]:
                g = lambda tag: H.unescape((re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", it, re.S) or [None, ""])[1])
                hit.append({"title": re.sub(r"<[^>]+>", "", g("title")).strip(), "url": g("link").strip(),
                            "date": g("pubDate")[:16], "source": g("source")})
            self.cache.put("news", q, hit)
        return hit
