"""Checko API (api.checko.ru/v2) по ключу — легальный машинный канал вместо чтения витрин.

Ключ — переменная CHECKO_API_KEY (или riskpipe/.env). Бесплатный тариф — 100 запросов в сутки;
на компанию: legal-cases (обычно 1 страница) + enforcements + company = 3 -> ~33 компании/день.
Класс источника — aggregator.

Формат сверен с живым API 26.10.2026 (фикстуры eval/fixtures/checko_api_*.json):
legal-cases.data = {ЗапВсего, СтрВсего, ОбщСуммИск, Записи[{Номер, Дата, Тип (Г/Б/…), ПрАктив, Суд,
Ист[], Ответ[], СуммИск, Победитель, ИсходТекст, СтрКАД}]}; enforcements.data = {ОбщКолич, ОбщСум,
ОстЗадолж, Записи[]}; company.data = {Статус, Санкции, СанкцииСтраны, НедобПост, ДисквЛица, МассРуковод,
МассУчред, ЮрАдрес{Недост, МассАдрес[]}, Правопредш[], Правопреем[], ЕФРСБ[], …}.

По методике mOS «суды — не количество, а состав»: каждое дело уходит в граф узлом дела с ролями
сторон и контрагентом (extra["cases"] -> graph/cases.py). Правило R8/Y8 техпроекта считается точно:
иски, где компания ответчик, за последние 12 месяцев.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

from collectors.base import CollectResult, Link, Statement
from core.http import FileCache, Http, Unavailable

BASE = "https://api.checko.ru/v2"
CH = "checko_api"
CLS = "aggregator"
CASE_TYPE = {"Б": "банкротство", "Г": "экономический спор", "А": "административный спор"}


def _num(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _parties(r: dict, *keys) -> list[dict]:
    v = _first(r, *keys, default=[]) or []
    return [v] if isinstance(v, dict) else [x for x in v if isinstance(x, dict)]


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def parse_legal(inn: str, pages: list[dict], today: date) -> list[Statement]:
    """pages — ответы legal-cases (role=defendant, sort=-date), свежие дела сверху."""
    url = f"https://checko.ru/search?query={inn}#legal-cases"
    since = (today - timedelta(days=365)).isoformat()
    recs = [r for p in pages for r in (_first(p, "Записи", "Дела", default=[]) or [])]
    total = _first(pages[0], "ЗапВсего", "Всего", default=len(recs)) if pages else 0
    S = [Statement("arb_defendant_count_total", str(int(_num(total))), url, CLS)]
    n = act_n = 0
    s = act_s = lost = 0.0
    bankr = None
    for r in recs:
        if not any(str(_first(x, "ИНН", default="")) == inn for x in _parties(r, "Ответ", "Ответчик", "Ответчики")):
            continue
        amount = _num(_first(r, "СуммИск", "Сумма", "СумИск"))
        if str(_first(r, "Дата", "ДатаРег", default=""))[:10] >= since:
            n += 1
            s += amount
            if _first(r, "Победитель") == "Истец":
                lost += amount
        if _first(r, "ПрАктив"):
            act_n += 1
            act_s += amount
            if str(_first(r, "Тип", default="")).upper() == "Б" and bankr is None:
                bankr = r
    S.append(Statement("arb_defendant_count", str(n), url, CLS))
    S.append(Statement("arb_defendant_sum", str(s), url, CLS,
                       quote=f"в роли ответчика за 12 мес: {n} дел на {_rub(s)} руб.; проиграно на {_rub(lost)} руб.; "
                             f"на рассмотрении {act_n} дел на {_rub(act_s)} руб."))
    S.append(Statement("arb_defendant_active_sum", str(act_s), url, CLS))
    if bankr is not None:
        plaintiffs = ", ".join(str(x.get("Наим")) for x in _parties(bankr, "Ист", "Истец"))
        S.append(Statement("bankruptcy", f"заявлено: в арбитраже рассматривается дело о банкротстве компании "
                                         f"№ {bankr.get('Номер')} от {bankr.get('Дата')}",
                           bankr.get("СтрКАД") or url, CLS, quote=f"{bankr.get('Суд')}; заявитель: {plaintiffs}"[:200]))
    return S


def cases_of(inn: str, pages: list[dict]) -> list[dict]:
    """Дела в форме для графа: номер, дата, тип, суд, сумма, исход, стороны с ИНН."""
    out = []
    for p in pages:
        for r in _first(p, "Записи", default=[]) or []:
            if not r.get("Номер"):
                continue
            side = lambda key: [{"inn": str(x.get("ИНН") or "") or None, "name": x.get("Наим")}
                                for x in _parties(r, *key)]
            out.append({"number": r["Номер"], "date": str(r.get("Дата") or "")[:10],
                        "type": CASE_TYPE.get(str(r.get("Тип") or "").upper(), r.get("Тип")),
                        "court": r.get("Суд"), "sum": _num(r.get("СуммИск")), "active": bool(r.get("ПрАктив")),
                        "outcome": r.get("ИсходТекст"), "winner": r.get("Победитель"),
                        "url": r.get("СтрКАД") or f"https://checko.ru/search?query={inn}#legal-cases",
                        "plaintiffs": side(("Ист", "Истец")), "defendants": side(("Ответ", "Ответчик"))})
    return out


def parse_enf(inn: str, data: dict) -> list[Statement]:
    url = f"https://checko.ru/search?query={inn}#enforcements"
    recs = _first(data, "Записи", default=[]) or []
    cnt = _first(data, "ОбщКолич", "ЗапВсего", "Всего", default=len(recs))
    total = _first(data, "ОстЗадолж", "ОбщСум", "Сумма")       # остаток долга, а не исходная сумма
    if total is None:
        total = sum(_num(_first(r, "ОстЗадолж", "СумДолг", "Сумма")) for r in recs)
    return [Statement("enforcement_count", str(int(_num(cnt))), url, CLS),
            Statement("enforcement_sum", str(_num(total)), url, CLS)]


def parse_company(inn: str, d: dict) -> tuple[list[Statement], list[Link]]:
    """Санкции, РНП, дисквалификация, массовость, недостоверный адрес, ЕФРСБ, правопреемство."""
    url = f"https://checko.ru/search?query={inn}"
    S, L = [], []
    yn = lambda v: "да" if v else "нет"
    if "Санкции" in d:
        S.append(Statement("sanctions", ("да: " + ", ".join(d.get("СанкцииСтраны") or [])) if d["Санкции"] else "нет",
                           url, CLS))
    if d.get("СанкцУчр"):
        S.append(Statement("sanctions_inferred", "учредитель под санкциями (правило 50%)", url, CLS))
    if "НедобПост" in d:
        S.append(Statement("rnp", yn(d["НедобПост"]), url, CLS))
    if d.get("ДисквЛица"):
        S.append(Statement("disqualified", "да: в руководстве дисквалифицированное лицо", url, CLS))
    if d.get("МассРуковод"):
        S.append(Statement("mass_director", "да", url, CLS))
    if d.get("МассУчред"):
        S.append(Statement("mass_founder", "да", url, CLS))
    if d.get("НелегалФин"):
        S.append(Statement("illegal_finance", "да: признаки нелегальной финансовой деятельности (ЦБ)", url, CLS))
    addr = d.get("ЮрАдрес") or {}
    if addr.get("Недост"):
        S.append(Statement("unreliable_info", "да: адрес", url, CLS))
    if addr.get("МассАдрес"):
        S.append(Statement("address_mass_checko", str(len(addr["МассАдрес"])), url, CLS))
    ef = d.get("ЕФРСБ") or []
    if ef:
        last = ef[0] if isinstance(ef, list) else ef
        S.append(Statement("efrsb_messages", f"{len(ef)} сообщений ЕФРСБ; последнее: "
                                             f"{_first(last, 'Тип', 'ТипСообщ', default='')} {_first(last, 'Дата', default='')}",
                           url, CLS))
    # дочки и подконтрольные: ФНС по ИНН юрлица их не отдаёт (только учредителей самой компании)
    for key, rel in (("СвязУчред", "учредитель"), ("СвязУпрОрг", "управляющая_организация")):
        for x in d.get(key) or []:
            if x.get("ИНН"):
                L.append(Link(rel, "out", x.get("НаимСокр") or x.get("НаимПолн"), str(x["ИНН"]), "ЮЛ", url, CLS,
                              other_status=x.get("Статус")))
    for key, rel in (("Правопредш", "правопредшественник"), ("Правопреем", "правопреемник")):
        for x in d.get(key) or []:
            if x.get("ИНН"):
                L.append(Link(rel, "in" if key == "Правопредш" else "out", x.get("НаимПолн") or x.get("НаимСокр"),
                              str(x["ИНН"]), "ЮЛ", url, CLS))
    return S, L


def load_key(root=None) -> str | None:
    """CHECKO_API_KEY из окружения или из <root>/.env (по умолчанию riskpipe/.env)."""
    from pathlib import Path

    from core.env import getenv
    return getenv("CHECKO_API_KEY", Path(root) / ".env" if root else None)


class CheckoApi:
    ch = CH
    web = False

    def __init__(self, http: Http, cache: FileCache, key: str, today: date | None = None, interval: float = 1.0,
                 max_pages: int = 3, with_company: bool = True, log=print):
        self.http = http
        self.cache = cache
        self.key = key
        self.today = today or date.today()
        self.max_pages = max_pages
        self.with_company = with_company
        self.log = log
        self.used = 0
        self.http.min_interval.setdefault("api.checko.ru", interval)

    def _get(self, method: str, inn: str, **params) -> dict:
        ck = inn + "".join(f"_{k}{v}" for k, v in sorted(params.items()))
        hit = self.cache.get(CH + "_" + method, ck, 1)
        if hit is not None:
            return hit
        r = self.http.request("GET", f"{BASE}/{method}", params={"key": self.key, "inn": inn, **params})
        self.used += 1
        j = r.json()
        meta = j.get("meta") or {}
        if meta.get("status") == "error" or r.status_code != 200:
            raise Unavailable(f"Checko API {method}: {meta.get('message') or r.status_code}")  # лимит/ключ
        self.cache.put(CH + "_" + method, ck, j)
        return j

    def collect(self, inn: str) -> CollectResult:
        try:
            pages, since = [], (self.today - timedelta(days=365)).isoformat()
            for page in range(1, self.max_pages + 1):     # свежие сверху: листаем, пока не вышли за 12 мес
                d = self._get("legal-cases", inn, role="defendant", sort="-date", page=page).get("data") or {}
                pages.append(d)
                recs = d.get("Записи") or []
                if not recs or str(recs[-1].get("Дата", ""))[:10] < since or page >= int(d.get("СтрВсего") or 1):
                    break
            en = self._get("enforcements", inn).get("data") or {}
        except Unavailable as e:
            return CollectResult.fail(CH, inn, str(e))
        except ValueError as e:                                   # не JSON
            return CollectResult.fail(CH, inn, f"Checko API: ответ не JSON ({e})")
        S = parse_legal(inn, pages, self.today) + parse_enf(inn, en)
        L: list[Link] = []
        if self.with_company:
            try:
                cs, L = parse_company(inn, self._get("company", inn).get("data") or {})
                S += cs
            except (Unavailable, ValueError) as e:            # без карточки суды и ФССП всё равно засчитаны
                self.with_company = False
                self.log(f"  Checko API company выключен до конца прогона: {e}")
        return CollectResult(channel=CH, ok=True, inn=inn, page_inn=inn, statements=S, links=L,
                             extra={"cases": cases_of(inn, pages)})
