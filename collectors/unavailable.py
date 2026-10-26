"""Каналы, которые из текущей сети не открываются: ЕФРСБ, КАД, ЕИС, ФССП.

Проба делается один раз за прогон; результат пишется в check_log каждой компании как
«не открылось» с причиной — карточка показывает дыру в покрытии, а не «чисто».
"""
from __future__ import annotations

from core.http import Http, Unavailable

PROBES = {
    "ЕФРСБ (банкротство)": "https://bankrot.fedresurs.ru/backend/cmpbankrupts?searchString=9900010015&limit=1&offset=0",
    "КАД (суды)": "https://kad.arbitr.ru/Kad/SearchInstances",
    "ЕИС (контракты)": "https://zakupki.gov.ru/epz/main/public/home.html",
    "ФССП": None,   # сайт с капчей — не обходим, нужен API-агрегатор
}


def probe(http: Http) -> dict[str, str]:
    """Канал -> причина недоступности (пустая строка — открылся)."""
    out = {}
    for ch, url in PROBES.items():
        if url is None:
            out[ch] = "капча на сайте, канал не подключён"
            continue
        try:
            r = http.request("POST" if "SearchInstances" in url else "GET", url,
                             **({"json": {}} if "SearchInstances" in url else {}))
            out[ch] = "" if r.status_code == 200 and "json" in r.headers.get("content-type", "") else \
                f"HTTP {r.status_code}, антибот/не JSON"
        except Unavailable as e:
            out[ch] = str(e)
    return out
