"""Веб-указатели для глубокого разбора: поиск ищет модель, страницы читает код.

Правила mOS: «поиск даёт указатель — сниппет не источник»; «читаем страницы, не выкачиваем базы»;
«витрина не открывается — нормальный исход, а не повод обходить». Поэтому:
1. Haiku с одним инструментом WebSearch (без WebFetch) за 1–2 хода возвращает только список ссылок с причиной —
   сайт компании, отзывы сотрудников, новости, документы, связанные ЮЛ вне выписки.
2. Код открывает до N страниц (единичное чтение), отбрасывая витрины-агрегаторы, и вырезает из текста окно
   вокруг упоминаний компании. Дальше эти тексты читаются вместе с судебными актами (deep/analyze.py).
"""
from __future__ import annotations

import html as H
import re

from core.http import FileCache, Http, Unavailable
from core.source_class import host

# витрины: по правилу mOS не читаются (часть закрыта robots, часть подменяет данные при потоковом чтении)
SHOWCASES = ("rusprofile.ru", "checko.ru", "companium.ru", "list-org.com", "zachestnyibiznes.ru", "sbis.ru",
             "saby.ru", "audit-it.ru", "spark-interfax.ru", "kontur.ru", "vbankcenter.ru", "rbc.ru/companies",
             "synapsenet.ru", "e-ecolog.ru", "ortexgroup.ru", "focus.kontur.ru", "companies.rbc.ru")

POINTER_TASK = """\
КОМПАНИЯ: {name}, ИНН {inn}{region}
ИЗВЕСТНО: {brief}

Найди ссылки, которые стоит прочитать для оценки риска заказчика: официальный сайт компании, отзывы сотрудников
(задержки зарплаты), новости о компании и её владельцах, документы о связанных компаниях и смене собственников.
Сделай не больше {searches} поисков — все ОДНИМ ходом параллельно; сниппеты не пересказывай.
НЕ включай витрины-агрегаторы (rusprofile, checko, companium, list-org, zachestnyibiznes, sbis, audit-it, spark).
ОТВЕТ — только JSON: {{"urls":[{{"url":"...","why":"<что ожидаешь там найти>"}}]}} — не больше {pages} ссылок.
"""


def is_showcase(url: str) -> bool:
    h = host(url)
    return any(h == d or h.endswith("." + d) or d in url for d in SHOWCASES)


def window(text: str, name: str, inn: str | None, size: int = 2500) -> str:
    """Окно текста вокруг упоминаний компании (ИНН или ядро названия); иначе начало страницы."""
    core = re.sub(r"[«»\"]", "", re.sub(r"^\s*(ООО|АО|ПАО|ЗАО|ОАО|МУП|ГУП)\s+", "", name or "", flags=re.I)).strip()
    pos = [m.start() for key in (inn, core) if key for m in re.finditer(re.escape(key), text, re.I)]
    if not pos:
        return text[:size]
    start = max(0, pos[0] - size // 4)
    return text[start:start + size]


class PageReader:
    def __init__(self, http: Http, cache: FileCache):
        self.http = http
        self.cache = cache

    def read(self, url: str, name: str, inn: str | None) -> dict | None:
        if is_showcase(url):
            return None
        hit = self.cache.get("pages", url, 7)
        if hit is None:
            try:
                r = self.http.request("GET", url)
            except Unavailable:
                self.cache.put("pages", url, {})
                return None
            if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
                self.cache.put("pages", url, {})
                return None
            t = re.sub(r"<script.*?</script>|<style.*?</style>|<nav.*?</nav>|<footer.*?</footer>", " ", r.text, flags=re.S)
            t = re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", t))).strip()
            title = H.unescape((re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S) or [None, url])[1]).strip()
            hit = {"url": r.url or url, "title": re.sub(r"\s+", " ", title)[:150], "text": t[:60000]}
            self.cache.put("pages", url, hit)
        if not hit:
            return None
        return {**hit, "text": window(hit["text"], name, inn)}
