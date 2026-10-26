"""Companium (companium.ru): публичная карточка без ключа — основной бесплатный источник судов и ФССП.

Лучше Checko для правил техпроекта: суммы исков по ролям есть «за последний год» (окно 12 месяцев),
ФССП показывается и для компаний в банкротстве. Класс — aggregator. Антиподмена: ИНН на странице = запрос.
"""
from __future__ import annotations

import re
import time

from collectors.base import CollectResult, Statement
from collectors.checko import UNITS, section
from core.http import FileCache, Http, Unavailable

BASE = "https://companium.ru"
CH = "companium"
CLS = "aggregator"
NUM = r"([\d ,]+)\s*(трлн|млрд|млн|тыс\.?)?\s*руб"


def _sum(num: str, unit: str | None) -> float:
    return round(float(num.replace(" ", "").replace(",", ".")) * UNITS.get((unit or "").rstrip("."), 1))


def _role(lc: str, role: str):
    """'В роли ответчика 30 около 220 млн руб. за последний год — 0 руб.' -> (30, 2.2e8, 0)."""
    m = re.search(rf"В роли {role}\s+(\d+)\s+(?:около\s+)?{NUM}\.?\s*(?:из них около\s+{NUM}\.?\s*за последний год"
                  rf"|за последний год\s*—\s*{NUM})?", lc)
    if not m:
        return None
    n, a, u, y1, yu1, y2, yu2 = m.groups()
    last = _sum(y1, yu1) if y1 else _sum(y2, yu2) if y2 else None
    return int(n), _sum(a, u), last


def parse(inn: str, url: str, page: str) -> CollectResult:
    m = re.search(r'id="copy-inn"[^>]*>\s*(\d{10,12})', page)
    page_inn = m.group(1) if m else None
    S: list[Statement] = []

    def st(prop, value, quote=None):
        S.append(Statement(prop, value, url, CLS, quote=quote))

    lc = section(page, "legal-cases")
    if lc:
        mm = re.search(r"рассмотрены\s+(\d+)\s+арбитражн", lc)
        if mm:
            st("arb_total_count", mm.group(1))
        pl, df = _role(lc, "истца"), _role(lc, "ответчика")
        if pl:
            st("arb_plaintiff_count", str(pl[0]))
            st("arb_plaintiff_sum_total", str(pl[1]))
            if pl[2] is not None:
                st("arb_plaintiff_sum_year", str(pl[2]))
        if df:
            st("arb_defendant_count_total", str(df[0]))
            st("arb_defendant_sum_total", str(df[1]))
            # правило R8/Y8: иски к компании за последние 12 месяцев
            st("arb_defendant_sum", str(df[2] or 0),
               quote=f"в роли ответчика {df[0]} дел за всё время; за последний год — {df[2] or 0:,.0f} руб.".replace(",", " "))
            st("arb_defendant_count", str(df[0]))
        elif pl:
            st("arb_defendant_sum", "0", quote="дел в роли ответчика нет")
    enf = section(page, "enforcements")
    if enf:
        c = re.search(r"открыто\s+(\d+)\s+исполнительн", enf)
        s = re.search(r"на общую сумму\s+" + NUM, enf)
        st("enforcement_count", c.group(1) if c else "0")
        st("enforcement_sum", str(_sum(s.group(1), s.group(2))) if s else "0", quote=enf[:160])
    elif lc:
        # раздел ФССП выводится только при открытых производствах
        st("enforcement_count", "0", quote="раздел «Исполнительные производства» отсутствует — открытых ИП нет")
        st("enforcement_sum", "0")
    bk = re.search(r"Реестр банкротств\s+(.{0,160})", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)))
    if bk:
        t = bk.group(1)
        mm = re.search(r"найдено\s+(\d+)\s+сообщ\w*\s+о банкротстве.*?последнее сообщение от\s+([\d.]+)", t)
        if mm:
            st("efrsb_messages", f"{mm.group(1)} сообщений ЕФРСБ, последнее {mm.group(2)}")
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))
    if "Нет сведений о приостановке операций по счетам" in txt:
        st("account_blocks", "нет")
    elif re.search(r"Есть сведения о приостановке операций|решени[ея] о приостановлении операций", txt):
        st("account_blocks", "да: есть решения ФНС о приостановлении операций по счетам")
    sc = re.search(r"Входит в санкционные списки следующих стран:\s*(.{0,120}?)(?:\s{2}|Нет связей|Добавить|$)", txt)
    if sc:
        st("sanctions", "да: " + sc.group(1).strip())
    elif "Не входит в санкционные списки" in txt:
        st("sanctions", "нет")
    return CollectResult(channel=CH, ok=True, inn=inn, page_inn=page_inn, statements=S,
                         not_found=[] if lc else ["суды"])


class Companium:
    """Темп как у Checko: пауза между карточками, при 429 — ждём окно, лимит не обходим."""

    ch = CH

    def __init__(self, http: Http, cache: FileCache, interval: float = 15.0, limit_wait: float = 120,
                 limit_tries: int = 2, log=print):
        self.http = http
        self.cache = cache
        self.limit_wait = limit_wait
        self.limit_tries = limit_tries
        self.log = log
        self.http.min_interval.setdefault("companium.ru", interval)

    def collect(self, inn: str) -> CollectResult:
        hit = self.cache.get(CH, inn, 1)
        try:
            if hit is None:
                for attempt in range(self.limit_tries):
                    try:
                        r = self.http.request("GET", f"{BASE}/search?query={inn}")
                        break
                    except Unavailable as e:
                        if "429" not in str(e) or attempt == self.limit_tries - 1:
                            raise
                        self.log(f"  companium.ru: 429, жду {self.limit_wait:.0f} с")
                        time.sleep(self.limit_wait)
                if r.status_code != 200 or "/id/" not in r.url:
                    return CollectResult(channel=CH, ok=True, inn=inn, page_inn=inn, not_found=["карточка Companium"])
                hit = {"url": r.url, "page": r.text}
                self.cache.put(CH, inn, hit)
        except Unavailable as e:
            return CollectResult.fail(CH, inn, str(e))
        return parse(inn, hit["url"], hit["page"])


class CachedOnly:
    """Веб-источник без сети: отдаёт только ранее скачанные карточки (правило mOS — витрины не выкачиваем)."""

    def __init__(self, src):
        self.src = src
        self.ch = src.ch
        self.cache = src.cache
        self.web = True

    def collect(self, inn: str) -> CollectResult:
        if self.cache.get(self.ch, inn, 1) is None:     # не «пусто»: канал не проверялся — так и пишем
            return CollectResult.fail(self.ch, inn, "веб-витрины выключены (правило mOS: не выкачивать)")
        return self.src.collect(inn)


class FirstAvailable:
    """Цепочка бесплатных агрегаторов: первый, кто ответил, закрывает суды/ФССП; упавший источник
    после отказа больше не дёргается в этом прогоне (квота на IP)."""

    def __init__(self, *sources, log=print):
        self.sources = list(sources)
        self.dead: set[int] = set()
        self.log = log

    def collect(self, inn: str) -> CollectResult:
        for i, src in enumerate(self.sources):       # API по ключу — первым: полный список дел, без квоты на IP
            if not getattr(src, "web", True) and i not in self.dead:
                res = src.collect(inn)
                if res.ok and res.statements:
                    return self._with_cached_web(res, inn)
                if not res.ok:
                    self.dead.add(i)
                    self.log(f"  {type(src).__name__} выключен до конца прогона: {res.error}")
        for src in self.sources:                     # затем то, что уже лежит в кэше: квота веба на IP мала
            if getattr(src, "web", True) and src.cache.get(src.ch, inn, 1) is not None:
                return src.collect(inn)
        last = None
        for i, src in enumerate(self.sources):
            if i in self.dead or not getattr(src, "web", True):
                continue
            res = src.collect(inn)
            if res.ok and res.statements:
                return res
            if not res.ok:
                self.dead.add(i)
                self.log(f"  {type(src).__name__} выключен до конца прогона: {res.error}")
            last = res
        return last or CollectResult.fail("aggregators", inn, "все бесплатные агрегаторы исчерпали квоту")

    def _with_cached_web(self, res: CollectResult, inn: str) -> CollectResult:
        """API даёт суды и ФССП; санкции, блокировки и ЕФРСБ добираем из уже скачанной веб-карточки (без сети)."""
        have = {s.prop for s in res.statements}
        for src in self.sources:
            if getattr(src, "web", True) and src.cache.get(src.ch, inn, 1) is not None:
                web = src.collect(inn)
                if web.ok and web.page_inn == inn:
                    res.statements += [s for s in web.statements if s.prop not in have
                                       and not s.prop.startswith(("arb_", "enforcement_"))]
                break
        return res
