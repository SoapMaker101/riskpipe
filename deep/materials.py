"""Материалы для глубокого разбора собирает код, а не модель: «поиск даёт указатель, текст акта даёт факт» (mOS).

- Суды: номера ключевых дел, где компания ответчик или должник, уже есть в графе (Checko API). По номеру дела
  sudact.ru (JSON-эндпоинт doc_ajax, тот же, что у формы поиска сайта) даёт ссылку на акт; код читает 1–2 акта —
  единичное чтение страниц, без обхода списков и капчи. Из акта берётся шапка (стороны, предмет) и резолютивная часть.
- Новости: заголовки RSS Google News за год (уже в кэше после шага СМИ).

Модели остаётся только чтение готовых текстов — один ход, без инструментов, предсказуемая стоимость.
"""
from __future__ import annotations

import html as H
import re
import time
from datetime import date, timedelta

from core.http import FileCache, Http, Unavailable

SUDACT = "https://sudact.ru"
RES_MARK = ("Р Е Ш И Л", "РЕШИЛ", "О П Р Е Д Е Л И Л", "ОПРЕДЕЛИЛ", "П О С Т А Н О В И Л", "ПОСТАНОВИЛ")
START_MARK = ("ИМЕНЕМ РОССИЙСКОЙ ФЕДЕРАЦИИ", "Р Е Ш Е Н И Е", "О П Р Е Д Е Л Е Н И Е", "ОПРЕДЕЛЕНИЕ", "РЕШЕНИЕ",
              "ПОСТАНОВЛЕНИЕ")


def key_cases(store, eid: str, today: date, n: int = 2) -> list[dict]:
    """Дела, где компания ответчик/должник: сначала активные банкротные, затем крупнейшие за 12 мес."""
    since = (today - timedelta(days=365)).isoformat()
    rows = store.db.execute("SELECT dst FROM edge WHERE src=? AND rel='ответчик' AND dst LIKE 'дело:%'", (eid,)).fetchall()
    out = []
    for r in rows:
        st = store.latest(r["dst"])
        v = lambda p: (st.get(p) or [{"value": None}])[0]["value"]
        out.append({"number": r["dst"].split(":", 1)[1], "type": v("case_type") or "", "date": v("case_date") or "",
                    "sum": float(v("case_sum") or 0), "active": v("case_active") == "да"})
    out.sort(key=lambda c: (c["active"] and "банкрот" in c["type"], c["date"] >= since, c["sum"]), reverse=True)
    return out[:n]


def _text(page: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S)
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def act_excerpt(text: str, head: int = 1600, tail: int = 1400) -> str:
    """Шапка акта (стороны, предмет, суммы) + резолютивная часть — то, что вынимает методика mOS."""
    i = min([text.find(m) for m in START_MARK if text.find(m) >= 0] or [0])
    body = text[i:]
    j = max([body.rfind(m) for m in RES_MARK if body.rfind(m) >= 0] or [-1])
    if j > head:
        return body[:head] + " […] " + body[j:j + tail]
    return body[:head + tail]


class Sudact:
    def __init__(self, http: Http, cache: FileCache, interval: float = 2.0):
        self.http = http
        self.cache = cache
        self.http.min_interval.setdefault("sudact.ru", interval)

    def act_by_case(self, number: str) -> dict | None:
        hit = self.cache.get("sudact", number, 30)
        if hit is not None:
            return hit or None
        link = None
        for _ in range(5):                                   # сайт отдаёт результат не с первого запроса
            r = self.http.request("GET", f"{SUDACT}/arbitral/doc_ajax/", params={"arbitral-case_doc": number},
                                  headers={"X-Requested-With": "XMLHttpRequest"})
            try:
                d = r.json()
            except ValueError:
                raise Unavailable("sudact.ru: ответ не JSON")
            if d.get("content"):
                m = re.search(r'href="(/arbitral/doc/[^"?]+)', d["content"])
                link = SUDACT + m.group(1) if m else None
                break
            time.sleep(1.5)
        if not link:
            self.cache.put("sudact", number, {})
            return None
        page = self.http.request("GET", link).text
        text = _text(page)
        title = (re.search(r"<title>(.*?)</title>", page, re.S) or [None, ""])[1].split("::")[0].strip()
        hit = {"number": number, "url": link, "title": H.unescape(title), "text": act_excerpt(text)}
        self.cache.put("sudact", number, hit)
        return hit


def collect_materials(store, eid: str, name: str, region: str | None, sudact: Sudact | None, news, today: date,
                      n_acts: int = 2, n_news: int = 8) -> tuple[list[dict], list[str]]:
    """[{kind, title, url, text}], журнал того, что не открылось/не нашлось."""
    mats, log = [], []
    if sudact is not None:
        for c in key_cases(store, eid, today, n_acts):
            try:
                a = sudact.act_by_case(c["number"])
            except Unavailable as e:
                log.append(f"sudact: {e}")
                break
            if a:
                mats.append({"kind": f"судебный акт по делу {c['number']} ({c['type']}, иск {c['sum']:,.0f} ₽)"
                             .replace(",", " "), "title": a["title"], "url": a["url"], "text": a["text"]})
            else:
                log.append(f"акт по делу {c['number']} на sudact.ru не найден")
    if news is not None:
        try:
            for h in news.headlines(name, region)[:n_news]:
                mats.append({"kind": "заголовок новости", "title": h["title"], "url": h["url"],
                             "text": f"{h['title']} ({h.get('date', '')})"})
        except Unavailable as e:
            log.append(f"новости: {e}")
    return mats, log
