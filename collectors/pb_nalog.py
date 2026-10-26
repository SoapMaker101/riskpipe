"""ФНС «Прозрачный бизнес» (pb.nalog.ru): официальный канал ЕГРЮЛ + открытые данные ФНС.

Что даёт по ИНН ЮЛ: статус (в т.ч. процедура банкротства из ЕГРЮЛ), дата регистрации, ОКВЭД, ОКОПФ,
численность, доходы по налоговой отчётности, недоимка, недостоверность, массовый адрес,
руководитель и учредители с ИНН и числом их компаний, публикации в Вестнике госрегистрации.
По ИНН ФЛ: компании, где он руководитель / учредитель (второй шаг графа).

Протокол: POST *-proc.json -> {id, token} -> POST get-response. Капча не обходится:
при капче ждём окно (по замеру — около минуты) и повторяем; после N попыток канал «не открылось».
"""
from __future__ import annotations

import re
import time

from collectors.base import CollectResult, Link, Statement
from core.http import Captcha, FileCache, Http, Unavailable
from core.ids import entity_type, inn_valid, norm_name

BASE = "https://pb.nalog.ru/"
CH = "pb_nalog"
CLS = "official"


def self_mass_address() -> int:
    """Порог массового адреса — из rules.yaml (structure.address_mates_yellow)."""
    from scoring.score import load_rules
    return int(load_rules()["structure"]["address_mates_yellow"])


def pick_by_hints(cands: list[dict], okved_hints: list[str], region_hint: str | None) -> dict | None:
    """+2 за ОКВЭД из отраслевого списка, +1 за регион. Победитель должен опережать второго."""
    def score(c):
        s = 2 if any(str(c.get("okved") or "").startswith(p) for p in okved_hints) else 0
        if region_hint and region_hint.upper()[:6] in str(c.get("region") or "").upper():
            s += 1
        return s
    ranked = sorted(cands, key=score, reverse=True)
    if ranked and score(ranked[0]) > 0 and (len(ranked) == 1 or score(ranked[0]) > score(ranked[1])):
        ranked[0]["hint"] = (f"ОКВЭД {ranked[0].get('okved') or '?'}"
                             + (f", регион {ranked[0].get('region')}" if region_hint else "")
                             + f"; отвергнуто тёзок: {len(cands) - 1}")
        return ranked[0]
    return None


def card_url(inn: str) -> str:
    return f"{BASE}search.html#mode=search-all&queryAll={inn}"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


OPF = {"ооо": "ООО", "ао": "АО", "пао": "ПАО", "зао": "ЗАО", "оао": "ОАО", "муп": "МУП", "гуп": "ГУП",
       "фгуп": "ФГУП", "нао": "НАО"}


def _opf(name: str) -> str | None:
    m = re.match(r"\s*([А-Яа-яЁё]{2,5})\b", name or "")
    return OPF.get(m.group(1).lower()) if m else None


class PbNalog:
    def __init__(self, http: Http, cache: FileCache, captcha_wait: float = 70, captcha_tries: int = 4,
                 interval: float = 4.0, log=print):
        self.http = http
        self.cache = cache
        self.captcha_wait = captcha_wait
        self.captcha_tries = captcha_tries
        self.http.min_interval.setdefault("pb.nalog.ru", interval)
        self.log = log
        self.n_captcha = 0

    # ---------- транспорт ----------
    def _once(self, path: str, data: dict) -> dict:
        r = self.http.request("POST", BASE + path, data=data).json()
        if "pbSearchCaptcha" in str(r.get("ERRORS", "")) or r.get("captchaRequired"):
            raise Captcha("pb.nalog.ru: капча")
        if "id" not in r:
            raise Unavailable(f"pb.nalog.ru: неожиданный ответ {str(r)[:120]}")
        d = {"id": r["id"], "method": "get-response"}
        if "token" in r:
            d["token"] = r["token"]
        for _ in range(6):
            x = self.http.request("POST", BASE + path, data=d)
            if x.status_code == 200 and x.text.strip():
                j = x.json()
                if "ERRORS" in j:
                    if "aptcha" in str(j["ERRORS"]):
                        raise Captcha("pb.nalog.ru: капча")
                    raise Unavailable(f"pb.nalog.ru: {str(j['ERRORS'])[:120]}")
                return j
            time.sleep(1.0)
        raise Unavailable("pb.nalog.ru: нет ответа на get-response")

    def call(self, path: str, data: dict) -> dict:
        for attempt in range(self.captcha_tries):
            try:
                return self._once(path, data)
            except Captcha:
                self.n_captcha += 1
                if attempt == self.captcha_tries - 1:
                    raise
                self.log(f"  pb.nalog.ru: капча, жду {self.captcha_wait:.0f} с (попытка {attempt + 1})")
                time.sleep(self.captcha_wait)
        raise Unavailable("pb.nalog.ru")

    def search(self, query: str, ttl: float = 1) -> dict:
        hit = self.cache.get("pb_search", query, ttl)
        if hit is not None:
            return hit
        r = self.call("search-proc.json", {"mode": "search-all", "queryAll": query, "page": 1, "pageSize": 50})
        self.cache.put("pb_search", query, r)
        return r

    def card(self, inn: str, token: str, ttl: float = 1) -> dict:
        hit = self.cache.get("pb_card", inn, ttl)
        if hit is not None:
            return hit
        r = self.call("company-proc.json", {"token": token, "method": "get-request"})
        self.cache.put("pb_card", inn, r)
        return r

    # ---------- резолв по названию ----------
    def resolve(self, name: str, okved_hints: list[str] | None = None,
                region_hint: str | None = None) -> tuple[str | None, list[dict]]:
        """Название -> ИНН. Одно действующее совпадение — берём. Несколько — правило mOS: выбрать наиболее
        вероятного по подсказкам (отрасль, регион), если он явно лучше остальных; иначе кандидаты на выбор."""
        q = re.sub(r"[«»\"]", " ", name)
        q = re.sub(r"^\s*(ООО|АО|ПАО|ЗАО|ОАО|МУП|ГУП|ФГУП|НАО)\s+", "", q).strip()
        rows = (self.search(q, ttl=7).get("ul") or {}).get("data", [])
        want, opf = norm_name(name), _opf(name)
        cands = [r for r in rows if norm_name(r.get("namec", "")) == want and str(r.get("pr_liq")) == "0"]
        if opf:
            same = [r for r in cands if _opf(r.get("namec", "")) == opf]
            cands = same or cands
        brief = [{"inn": r["inn"], "name": r.get("namec"), "region": r.get("regionname"),
                  "status": r.get("sulst_name_ex"), "okved": r.get("okved2") or r.get("okved2main")} for r in cands[:8]]
        if len(cands) == 1 and inn_valid(cands[0]["inn"]):
            return cands[0]["inn"], brief
        if len(cands) > 1:
            pick = pick_by_hints(brief, okved_hints or [], region_hint)
            if pick:
                return pick["inn"], [pick] + [b for b in brief if b is not pick]
        if not cands:
            brief = [{"inn": r["inn"], "name": r.get("namec"), "region": r.get("regionname"),
                      "status": r.get("sulst_name_ex")} for r in rows[:5]]
        return None, brief

    # ---------- компания ----------
    def collect(self, inn: str) -> CollectResult:
        try:
            s = self.search(inn)
            rows = [r for r in (s.get("ul") or {}).get("data", []) if r.get("inn") == inn]
            if not rows:
                got = [r.get("inn") for r in (s.get("ul") or {}).get("data", [])]
                return CollectResult(channel=CH, ok=True, inn=inn, page_inn=got[0] if got else inn,
                                     not_found=["ЕГРЮЛ"] if not got else [])
            row = rows[0]
            c = self.card(inn, row["token"])
        except Unavailable as e:
            return CollectResult.fail(CH, inn, str(e))
        return self.parse(inn, row, c, s)

    def parse(self, inn: str, row: dict, c: dict, search: dict | None = None) -> CollectResult:
        v = c.get("vyp") or {}
        url = card_url(inn)
        page_inn = str(v.get("ИНН") or row.get("inn") or "")
        S = []

        def st(prop, value, year=None, quote=None):
            S.append(Statement(prop, value, url, CLS, year=year, quote=quote))

        status = v.get("sulst_name_ex") or row.get("sulst_name_ex")
        detail = v.get("НаимСтатусЮЛ")
        since = (v.get("ДатаСтатусЮЛ") or "")[:10]
        if status:
            st("status", status + (f"; {detail}" + (f" с {since}" if since else "") if detail else ""))
        if detail and re.search(r"наблюден|конкурсн|внешн|санаци|реализац", detail.lower()):
            st("bankruptcy", f"{detail} с {since} (ЕГРЮЛ)")
        if c.get("liquidated") and not status:            # статус ЕГРЮЛ точнее: «прекратило при присоединении»
            st("status", "Прекращена (ликвидирована)")
        if v.get("ДатаОГРН"):
            st("reg_date", v["ДатаОГРН"])
        if v.get("КодОКВЭД"):
            st("okved_main", f"{v.get('НаимОКВЭД', '')} ({v['КодОКВЭД']})")
        if v.get("okopf12"):
            st("okopf", str(int(_f(v["okopf12"]))))
        for x in c.get("sschr") or []:
            if x.get("sschr") is not None:
                st("employees", str(int(x["sschr"])), int(x["yearcode"]))
        for x in c.get("form1") or []:
            if not x.get("empty") and x.get("revenue") is not None:
                st("revenue", str(x["revenue"]), int(x["yearcode"]))
        # недоимка: последняя отчётная точка, где сведения есть
        arr = [a for a in c.get("arrear") or [] if not a.get("empty")]
        if arr:
            y, p = max((a["yearcode"], a["periodcode"]) for a in arr)
            last = [a for a in arr if (a["yearcode"], a["periodcode"]) == (y, p)]
            tot = sum(_f(a.get("totalsum")) or 0 for a in last)
            if tot > 0:
                st("tax_debt", f"{_rub(tot)} на {int(p):02d}.{int(y)}", int(y),
                   quote="; ".join(f"{a.get('kbkname')}: {_rub(_f(a.get('totalsum')) or 0)}" for a in last)[:200])
        # недостоверность
        inval = []
        if str(v.get("invalid", "0")) not in ("0", "0.0"):
            inval.append("есть")
        if v.get("СвНедАдресЮЛ"):
            inval.append("адрес")
        if _f(v.get("masrukinvalid")):
            inval.append("руководитель")
        if _f(v.get("masuchrinvalid")):
            inval.append("учредитель")
        if inval:
            st("unreliable_info", "да: " + ", ".join(dict.fromkeys(inval)))
        ma = c.get("masaddress") or []
        if ma:
            st("address_mates", str(len(ma)), quote=", ".join(m.get("massnamec", "") for m in ma[:5]))
        for x in c.get("vestnik") or []:
            st("vestnik", f"публикаций: {int(_f(x.get('count')) or 0)} (код {int(_f(x.get('code')) or 0)})",
               quote=x.get("url"))
        if search:
            rdl = (search.get("rdl") or {}).get("data") or []
            if rdl:
                st("disqualified", "да: " + "; ".join(str(r.get("name") or r.get("fio") or "") for r in rdl[:3]))

        # связи
        L = []
        for p in v.get("masruk") or []:
            pos = (p.get("position") or "").upper()
            pinn = p.get("inn")
            if "КОНКУРСН" in pos or "ВРЕМЕНН" in pos or "АРБИТРАЖН" in pos:
                rel = "конкурсный_управляющий"
            elif "УПРАВЛЯЮЩ" in pos and pinn and len(pinn) == 12:
                rel = "управляющий"
            elif pinn and len(pinn) == 10:
                rel = "управляющая_организация"
            else:
                rel = "руководитель"
            L.append(Link(rel, "in", p.get("name"), pinn, entity_type(pinn), url, CLS,
                          other_props={"companies_count": p.get("cnt")} if p.get("cnt") else None))
        for p in v.get("masuchr") or []:
            pinn = p.get("inn")
            L.append(Link("учредитель", "in", p.get("name"), pinn, entity_type(pinn), url, CLS,
                          other_props={"companies_count": p.get("cnt")} if p.get("cnt") else None))
        # канал «адрес» (методика mOS): немного соседей — связи «тот же адрес»; массовый адрес / БЦ — не связь
        for m in ma if 0 < len(ma) < self_mass_address() else []:
            if inn_valid(m.get("massinn")):
                L.append(Link("тот_же_адрес", "out", m.get("massnamec"), m["massinn"], "ЮЛ", url, CLS))
        not_found = []
        if not v.get("masuchr") and str(v.get("okopf12", "")).startswith("12300"):
            not_found.append("учредители")
        res = CollectResult(channel=CH, ok=True, inn=inn, page_inn=page_inn,
                            name=v.get("НаимЮЛСокр") or row.get("namec"), region=v.get("НаимРегион") or row.get("regionname"),
                            entity_type="ЮЛ", statements=S, links=L, not_found=not_found)
        res.extra["address"] = "массовый адрес — связи не строим" if len(ma) >= self_mass_address() else             (f"соседей по адресу: {len(ma)}" if ma else "соседей по адресу нет")
        m = re.search(r"organizations-card/(\d+)", (v.get("bourl") or row.get("bourl") or ""))
        if m:
            res.extra["bo_id"] = m.group(1)
        return res

    # ---------- человек -> его компании (шаг 2 графа) ----------
    def person_companies(self, inn: str, ttl: float = 7) -> CollectResult:
        """ИНН ФЛ -> компании, где он руководитель или учредитель. Возвращает связи от лица ФЛ."""
        hit = self.cache.get("pb_person", inn, ttl)
        try:
            if hit is None:
                s = self.search(inn, ttl=ttl)
                hit = {"name": None, "roles": {}}
                for kind in ("upr", "uchr"):
                    for row in (s.get(kind) or {}).get("data", []):
                        if row.get("inn") != inn:
                            continue
                        hit["name"] = row.get("name")
                        x = self.call("search-proc.json", {"mode": "search-ul", "token": row["token"],
                                                           "queryUl": row.get("name") or "", "page": 1,
                                                           "pageSize": 50})
                        hit["roles"][kind] = [{"inn": u.get("inn"), "name": u.get("namec"),
                                               "status": u.get("sulst_name_ex"), "pr_liq": u.get("pr_liq")}
                                              for u in (x.get("ul") or {}).get("data", [])]
                self.cache.put("pb_person", inn, hit)
        except Unavailable as e:
            return CollectResult.fail("pb_person", inn, str(e))
        url = card_url(inn)
        L = []
        for kind, rel in (("upr", "руководитель"), ("uchr", "учредитель")):
            for u in hit["roles"].get(kind, []):
                if inn_valid(u.get("inn")):
                    L.append(Link(rel, "out", u.get("name"), u["inn"], "ЮЛ", url, CLS,
                                  other_status=u.get("status")))
        n = len({u["inn"] for r in hit["roles"].values() for u in r if u.get("inn")})
        return CollectResult(channel="pb_person", ok=True, inn=inn, page_inn=inn, name=hit.get("name"),
                             entity_type="ФЛ", links=L,
                             statements=[Statement("companies_count", str(n), url, CLS)] if n else [])
