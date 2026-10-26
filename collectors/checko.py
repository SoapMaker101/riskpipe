"""Checko (checko.ru): публичная карточка компании без ключа — замена закрытых из этой сети КАД, ФССП, ЕФРСБ.

Даёт: арбитраж с разделением ролей (всего и «рассматриваются сейчас»: истец/ответчик, число и сумма),
исполнительные производства ФССП, сообщения ЕФРСБ и заявленное дело о банкротстве, блокировки счетов,
санкции. Класс источника — aggregator («факт (агрегатор)»), красное подтверждать в первоисточнике.
Антиподмена: ИНН на странице = ИНН запроса (Rusprofile в тесте подменял карточки при потоковом чтении).
"""
from __future__ import annotations

import html as H
import re
import time

from collectors.base import CollectResult, Statement
from core.http import FileCache, Http, Unavailable

BASE = "https://checko.ru"
CH = "checko"
CLS = "aggregator"
UNITS = {"трлн": 1e12, "млрд": 1e9, "млн": 1e6, "тыс": 1e3}


def _text(fragment: str) -> str:
    t = H.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return re.sub(r"\s+", " ", t).strip()


def section(page: str, sid: str) -> str | None:
    i = page.find(f'id="{sid}"')
    if i < 0:
        return None
    j = page.find("<section", i + 10)
    return _text(page[i:j if j > 0 else i + 6000])


def _sum(num: str, unit: str | None) -> float:
    return round(float(num.replace(" ", "").replace(",", ".")) * UNITS.get((unit or "").rstrip("."), 1))


ROLE = re.compile(r"(Истец|Ответчик)\s+([\d ,]+)\s*(трлн|млрд|млн|тыс\.?)?\s*руб\.\s*(\d+)\s*дел")


def parse(inn: str, url: str, page: str) -> CollectResult:
    m = re.search(r'id="copy-inn"[^>]*>\s*(\d{10,12})', page) or re.search(r"ИНН\s*</?[^>]*>\s*(\d{10,12})", page)
    page_inn = m.group(1) if m else None
    if not page_inn:
        m2 = re.search(r"\b(\d{10})\b", _text(section(page, "details") or ""))
        page_inn = m2.group(1) if m2 else None
    S: list[Statement] = []

    def st(prop, value, quote=None):
        S.append(Statement(prop, value, url, CLS, quote=quote))

    lc = section(page, "legal-cases")
    if lc:
        roles = ROLE.findall(lc)        # 1-2: всего (истец, ответчик), 3-4: рассматриваются сейчас
        if len(roles) >= 2:
            (r1, a1, u1, n1), (r2, a2, u2, n2) = roles[:2]
            st("arb_plaintiff_count", n1)
            st("arb_plaintiff_sum_total", str(_sum(a1, u1)))
            st("arb_defendant_count_total", n2)
            st("arb_defendant_sum_total", str(_sum(a2, u2)),
               quote=f"ответчик: {n2} дел на {a2.strip()} {u2 or ''} руб. за всё время")
        if len(roles) >= 4:
            (r3, a3, u3, n3), (r4, a4, u4, n4) = roles[2:4]
            st("arb_active_plaintiff_sum", str(_sum(a3, u3)))
            st("arb_defendant_count", n4)
            st("arb_defendant_sum", str(_sum(a4, u4)),
               quote=f"рассматриваются сейчас, ответчик: {n4} дел на {a4.strip()} {u4 or ''} руб.")
        elif len(roles) >= 2:
            st("arb_defendant_count", "0")
            st("arb_defendant_sum", "0", quote="рассматриваемых дел, где компания ответчик, нет")
        mm = re.search(r"рассмотрены (\d+) судебн", lc)
        if mm:
            st("arb_total_count", mm.group(1))

    enf = section(page, "enforcements")
    bk = section(page, "bankruptcy")
    if enf:
        if "Нет сведений" in enf:
            st("enforcement_count", "0"); st("enforcement_sum", "0")
        else:
            c = re.search(r"открыто\s+(\d+)\s+исполнительн", enf)
            s = re.search(r"на общую сумму\s+([\d ,]+)\s*(трлн|млрд|млн|тыс\.?)?\s*руб", enf)
            if c:
                st("enforcement_count", c.group(1))
            if s:
                st("enforcement_sum", str(_sum(s.group(1), s.group(2))), quote=enf[:200])
    elif bk:
        st("enforcement_count", "н/п: компания в процедуре банкротства, ИП приостановлены")

    facts = " ".join(x for x in (section(page, "red-facts"), section(page, "yellow-facts")) if x)
    if "рассматривается дело о банкротстве" in facts:
        st("bankruptcy", "заявлено: в арбитраже рассматривается дело о банкротстве компании (ответчик)",
           quote="В арбитражном суде рассматривается дело о банкротстве, в котором компания является ответчиком")
    if bk:
        mm = re.search(r"содержит\s+(\d+)\s+сообщ", bk)
        last = re.search(r"Дата последнего сообщения\s+([\d]+ \w+ \d{4})", bk)
        case = re.search(r"№\s*(А\d{2}-\d+/\d{4})", bk)
        st("efrsb_messages", f"{mm.group(1) if mm else '?'} сообщений ЕФРСБ"
           + (f", последнее {last.group(1)}" if last else "") + (f", дело {case.group(1)}" if case else ""))

    blk = section(page, "bank-account-blocking-section")
    if blk:
        if "Нет сведений о приостановлении" in blk:
            st("account_blocks", "нет")
        elif re.search(r"приостановлен", blk) and "Проверить" not in blk[:60]:
            st("account_blocks", "да: " + blk[:150])
        sc = re.search(r"Санкции\s+(.{0,120}?)(Это ваша|Мониторинг|Нет связей|$)", blk)
        if sc and "PRO" not in sc.group(1):
            v = sc.group(1).strip()
            st("sanctions", "нет" if v.startswith("Не входит") else f"да: {v[:100]}")
    return CollectResult(channel=CH, ok=True, inn=inn, page_inn=page_inn, statements=S,
                         not_found=[] if lc else ["суды"])


class Checko:
    """Темп: Checko отвечает 429 после ~10 быстрых карточек и держит блок > 15 мин (квота на IP);
    ждём окно и повторяем, лимит не обходим."""

    ch = CH

    def __init__(self, http: Http, cache: FileCache, interval: float = 15.0, limit_wait: float = 120,
                 limit_tries: int = 4, log=print):
        self.http = http
        self.cache = cache
        self.limit_wait = limit_wait
        self.limit_tries = limit_tries
        self.log = log
        self.n_limited = 0
        self.http.min_interval.setdefault("checko.ru", interval)

    def _get(self, url):
        for attempt in range(self.limit_tries):
            try:
                return self.http.request("GET", url)
            except Unavailable as e:
                if "429" not in str(e) or attempt == self.limit_tries - 1:
                    raise
                self.n_limited += 1
                self.log(f"  checko.ru: 429, жду {self.limit_wait:.0f} с (попытка {attempt + 1})")
                time.sleep(self.limit_wait)

    def collect(self, inn: str) -> CollectResult:
        hit = self.cache.get("checko", inn, 1)
        try:
            if hit is None:
                r = self._get(f"{BASE}/search?query={inn}")
                if r.status_code != 200 or "/company/" not in r.url:
                    return CollectResult(channel=CH, ok=True, inn=inn, page_inn=inn, not_found=["карточка Checko"])
                if "captcha" in r.text.lower()[:5000]:
                    raise Unavailable("checko.ru: капча")
                hit = {"url": r.url, "page": r.text}
                self.cache.put("checko", inn, hit)
        except Unavailable as e:
            return CollectResult.fail(CH, inn, str(e))
        return parse(inn, hit["url"], hit["page"])
