"""Светофор: утверждения -> собственные флаги -> перенос по графу -> цвет и покрытие.

Перенос score.py из прототипа; пороги — в rules.yaml. Новое: падение выручки г/г, недоимка ФНС,
профили заказчиков (учреждение / унитарное / коммерческое), раздельные «процедура идёт» и
«компании нет», веса рёбер в graph/propagate.py.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import yaml

from core.ids import inn_of, is_resolved
from db.store import Store

LEVEL = {"red": 3, "yellow": 2, "info": 1}
RULES_PATH = Path(__file__).with_name("rules.yaml")


def load_rules(path: Path = RULES_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def money(v) -> float | None:
    """'1,5 млрд руб.' -> 1.5e9; '547 млн' -> 5.47e8; '12345678.0' -> 12345678."""
    if v is None:
        return None
    s = str(v).lower().replace("\xa0", " ").replace("₽", "").replace("руб", "")
    neg = s.strip().startswith("-") or "убыт" in s
    m = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)\s*(трлн|млрд|млн|тыс)?", s)
    if not m:
        return None
    num_ = float(m.group(1).replace(" ", "").replace(",", "."))
    mult = {"трлн": 1e12, "млрд": 1e9, "млн": 1e6, "тыс": 1e3}.get(m.group(2), 1)
    return -num_ * mult if neg else num_ * mult


def rub(x: float) -> str:
    if abs(x) >= 1e9:
        return f"{x/1e9:.1f}".replace(".", ",") + " млрд ₽"
    if abs(x) >= 1e6:
        return f"{x/1e6:,.0f}".replace(",", " ") + " млн ₽"
    return f"{x/1e3:,.0f}".replace(",", " ") + " тыс. ₽"


def months_ago(text, today: date) -> float | None:
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", str(text or ""))
    if not m:
        return None
    d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return (today - d).days / 30.4


def num(v) -> float | None:
    m = re.search(r"\d+", str(v or "").replace(" ", ""))
    return float(m.group()) if m else None


def yes(v) -> bool:
    s = str(v or "").strip().lower()
    if not s or s in ("0", "none", "null", "false", "нет", "-", "—"):
        return False
    return not re.match(r"^(нет|не\s|не$|0\b|отсутств|сведений нет|не найден|не обнаруж)", s)


@dataclass
class Flag:
    rule: str
    level: str
    text: str
    kind: str = "fact2"
    url: str | None = None
    observed: str | None = None
    src_class: str | None = None
    via: str | None = None   # через какую связанную сущность пришёл риск
    check: str | None = None  # перекрёстная проверка красного: подтверждено ФНС / расхождение / один источник


@dataclass
class Card:
    eid: str
    name: str
    inn: str | None
    color: str = "grey"
    profile: str = "коммерческое"
    flags: list[Flag] = field(default_factory=list)
    coverage: float = 0.0
    missing: list[str] = field(default_factory=list)
    unavailable: dict = field(default_factory=dict)
    revenue: float | None = None
    action: str = ""
    resolved: bool = True

    def to_dict(self):
        d = asdict(self)
        d["flags"] = [{k: v for k, v in asdict(f).items() if v is not None} for f in self.flags]
        return d


def first(sts, prop):
    rows = sts.get(prop) or []
    return rows[0] if rows else None   # Store.latest уже отсортировал: факт > агрегатор > гипотеза, год, свежесть


def F(rule, level, text, row) -> Flag:
    return Flag(rule, level, text, row["kind"], row["src_url"], row["observed"], row["src_class"])


def profile_of(sts, R) -> str:
    o = first(sts, "okopf")
    code = str(o["value"]) if o else ""
    if any(code.startswith(p) for p in R["profiles"]["budget_okopf_prefix"]):
        return "учреждение"
    if any(code.startswith(p) for p in R["profiles"]["unitary_okopf_prefix"]):
        return "унитарное"
    return "коммерческое"


def own_flags(sts, R: dict, today: date | None = None) -> list[Flag]:
    today = today or date.today()
    f: list[Flag] = []
    g = lambda p: first(sts, p)
    prof = profile_of(sts, R)

    st = g("status")
    if st:
        s = st["value"].lower()
        if any(w in s for w in R["status"]["red_words"]):
            f.append(F("R1_status", "red", f"Статус: {st['value']}", st))
        elif any(w in s for w in R["status"]["terminated_words"]):
            f.append(F("R1_terminated", "red", f"Статус: {st['value']}", st))
        elif any(w in s for w in R["status"]["yellow_words"]):
            f.append(F("Y_status", "yellow", f"Статус: {st['value']}", st))

    rev = money(g("revenue")["value"]) if g("revenue") else None
    B = R["bankruptcy"]
    b = g("bankruptcy")
    if b:
        s = b["value"].lower()
        if any(w in s for w in B["red_words"]) and not ("намерен" in s):
            f.append(F("R2_bankruptcy", "red", f"Банкротство: {b['value']}", b))
        elif any(w in s for w in B["intent_words"]):
            age = months_ago(b["value"], today)
            lvl = "yellow" if age is None or age <= B["intent_months"] else "info"
            txt = f"Федресурс: {b['value'].split(': ', 1)[-1][:110]}"
            if lvl == "yellow" and rev and rev >= B["big_company_revenue"]:
                lvl, txt = "info", txt + f" — при выручке {rub(rev)} это фоновый шум; смотреть сумму требования"
            f.append(F("Y2_bankr_intent", lvl, txt, b))
        elif "прекращ" in s or "заверш" in s:
            f.append(F("I_bankr_closed", "info", f"Было дело о банкротстве: {b['value']}", b))

    for prop, rule, text in (("account_blocks", "R4_blocks", "Решения ФНС о блокировке счетов"),
                             ("disqualified", "R5_disq", "Дисквалифицированное лицо в руководстве"),
                             ("unreliable_info", "R7_unreliable", "Недостоверность сведений в ЕГРЮЛ")):
        r = g(prop)
        if r and yes(r["value"]):
            lvl = "red" if r["kind"] != "hypothesis" else "yellow"
            f.append(F(rule, lvl, f"{text}: {r['value']}", r))

    sc = g("sanctions")
    if sc and yes(sc["value"]) and set(re.split(r",\s*", sc["value"].split(":", 1)[-1].strip())) <= {"Украина"}:
        # список Украины покрывает тысячи российских компаний: для поставщика внутри РФ это фон
        f.append(F("I_sanctions_ua", "info", f"Санкции только Украины — фон: {sc['value'][:60]}", sc))
    elif sc and yes(sc["value"]):
        f.append(F("Y6_sanctions", "yellow", f"Внешние санкции: {sc['value'][:90]}", sc))
    for r in sts.get("sanctions_inferred", []):
        f.append(F("I_sanct_inferred", "info", f"Авто-метка связи с санкционными лицами: {r['value'][:80]}", r))

    A = R["arbitration"]
    arb = g("arb_defendant_sum")
    if arb and rev and rev > 0:
        a = money(arb["value"]) or 0
        share = a / rev
        if share >= A["defendant_red"]:
            f.append(F("R8_arb", "red", f"Иски к компании на рассмотрении {rub(a)} = {share:.0%} выручки"
                       + _arb_note(sts), arb))
        elif share >= A["defendant_yellow"]:
            f.append(F("Y8_arb", "yellow", f"Иски к компании на рассмотрении {rub(a)} = {share:.0%} выручки"
                       + _arb_note(sts), arb))

    E = R["enforcement"]
    enf = g("enforcement_sum")
    if enf:
        e = money(enf["value"]) or 0
        if e >= E["abs_min"] and (e / rev >= E["rel_min"] if rev else e >= E["abs_no_revenue"]):
            f.append(F("Y9_fssp", "yellow", f"Исполнительные производства на {rub(e)}"
                       + (f" = {e/rev:.1%} выручки" if rev else ""), enf))

    td = g("tax_debt")
    if td:
        t = money(td["value"]) or 0
        if t >= R["tax_debt"]["yellow_abs"]:
            f.append(F("Y10_tax", "yellow", f"Налоговая задолженность {td['value']}", td))
        elif t > 0:
            f.append(F("I_tax", "info", f"Небольшая недоимка/штрафы ФНС: {td['value']}", td))
    rn = g("reliability_note_rp")
    if rn and "налоговой задолженност" in rn["value"] and not (td and td["kind"] == "fact"):
        f.append(F("Y10_tax", "yellow", rn["value"], rn))

    S = R["structure"]
    for prop, text in (("mass_address", "Массовый адрес"), ("mass_director", "Массовый руководитель"),
                       ("mass_founder", "Массовый учредитель")):
        r = g(prop)
        if r and yes(r["value"]):
            f.append(F("Y11_mass", "yellow", f"{text}: {r['value']}", r))
    am = g("address_mates")
    if am and (num(am["value"]) or 0) >= S["address_mates_yellow"]:
        emp_ = num(g("employees")["value"]) if g("employees") else None
        big = (emp_ or 0) >= S["address_big_staff"] or (rev or 0) >= S["address_big_revenue"]
        # для крупной компании в бизнес-центре соседи по адресу — фон, а не признак однодневки
        f.append(F("Y11_mass", "info" if big else "yellow",
                   f"По адресу компании зарегистрированы ещё {am['value']} ЮЛ" + (" (крупная компания — фон)" if big else ""), am))

    if prof != "учреждение":
        np_ = g("net_profit")
        if np_ and (money(np_["value"]) or 0) < 0:
            f.append(F("Y12_loss", "yellow", f"Убыток {np_['year'] or ''}: {rub(money(np_['value']))}", np_))
        eq = g("equity")
        if eq and (money(eq["value"]) or 0) < 0:
            f.append(F("Y13_equity", "yellow", f"Отрицательные чистые активы {eq['year'] or ''}: "
                       f"{rub(money(eq['value']))}", eq))
        # падение выручки г/г: два года из одного вида источника
        revs = [r for r in sts.get("revenue", []) if r["year"]]
        if revs:
            best = revs[0]["kind"]
            by_y = {}
            for r in revs:
                if r["kind"] == best:
                    by_y.setdefault(r["year"], money(r["value"]))
            y = max(by_y)
            cur, prev = by_y.get(y), by_y.get(y - 1)
            F_ = R["finance"]
            if cur is not None and prev and prev >= F_["min_revenue_for_drop"] and (prev - cur) / prev > F_["revenue_drop_yellow"]:
                row = next(r for r in revs if r["year"] == y and r["kind"] == best)
                f.append(F("Y19_rev_drop", "yellow", f"Выручка {y} г. {rub(cur)} против {rub(prev)} в {y-1} г. "
                           f"(−{(prev-cur)/prev:.0%})", row))
        emp = g("employees")
        if emp and rev and rev >= S["shell_revenue"] and (num(emp["value"]) or 99) <= S["shell_staff"]:
            f.append(F("Y14_shell", "yellow",
                       f"{num(emp['value']):.0f} сотрудников при выручке {rub(rev)} — признак посредника", emp))
        rv = g("revenue")
        if rv and rv["year"] and rv["year"] < today.year - R["finance"]["stale_years"]:
            f.append(F("I_stale_fin", "info", f"Последняя доступная выручка — за {rv['year']} г. "
                       "(отчётность скрыта или не сдаётся)", rv))

    rd = g("reg_date")
    if rd:
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})|(\d{4})-(\d{2})-(\d{2})", rd["value"])
        if m:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m.group(3) else \
                date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
            if (today - d).days < 365:
                f.append(F("Y15_young", "yellow", f"Компания моложе года (рег. {rd['value']})", rd))

    dc = g("director_changes")
    if dc and (num(dc["value"]) or 0) >= S["director_changes_yellow"]:
        f.append(F("Y16_dirchg", "yellow", f"Смен руководителя за год: {dc['value']}", dc))

    # суды без роли: ВРЕМЕННОЕ правило до коннектора КАД — сигнал «проверить», а не вывод
    ay = g("arb_recent_years")
    if ay and rev and rev > 0 and not arb:          # есть роли (истец/ответчик) — временное правило не нужно
        worst = None
        for y, n_, amt in re.findall(r"(20\d\d): (\d+) на ([\d ]+)", ay["value"]):
            if int(y) >= today.year - 1:
                a = float(amt.replace(" ", ""))
                if not worst or a > worst[2]:
                    worst = (y, n_, a)
        if worst and worst[2] / rev >= A["year_share_yellow"]:
            f.append(F("Y18_arb_year", "yellow",
                       f"Суды {worst[0]} г.: {worst[1]} дел на {rub(worst[2])} = {worst[2]/rev:.0%} выручки; "
                       "роль (истец/ответчик) не разделена — проверить в КАД (временное правило)", ay))

    fm = g("fedresurs_messages")
    if fm and not g("bankruptcy"):
        age = months_ago(fm["value"], today)
        if age is not None and age > 36:
            f.append(F("I_old_fedresurs", "info", f"Старое сообщение Федресурса: {fm['value'][:100]}", fm))
    dh = g("director_hidden")
    if dh:
        f.append(F("I_hidden", "info", dh["value"], dh))
    fs = g("fin_state_rp")
    if fs and fs["value"].upper() in ("НИЗКАЯ",):
        f.append(F("I_agg_score", "info", f"Оценка агрегатора «финансовое состояние»: {fs['value'].lower()} "
                   "(чужой скоринг, не факт)", fs))
    vs = g("vestnik")
    if vs:
        f.append(F("I_vestnik", "info", f"Вестник госрегистрации: {vs['value']} — {vs['quote'] or ''}".strip(" —"), vs))

    cnt = g("arb_defendant_count")
    if cnt and (num(cnt["value"]) or 0) >= A["defendant_cases_yellow"] and cnt["src_url"] and "checko" in cnt["src_url"]:
        dsum = money(g("arb_defendant_sum")["value"]) if g("arb_defendant_sum") else 0
        # счёт без категории спора шумит у сетей и МУП: жёлтый — только если и сумма заметна относительно выручки
        big = (rev or 0) >= B["big_company_revenue"] or bool(rev and (dsum or 0) / rev < A["defendant_cases_min_share"])
        f.append(F("Y20_many_suits", "info" if big else "yellow",
                   f"Исков к компании за 12 мес: {cnt['value']} (порог {A['defendant_cases_yellow']}); категория "
                   "спора не разделена — проверить, не поставщики ли за неоплату"
                   + (f" — сумма исков {rub(dsum or 0)} мала относительно выручки, фон" if big else ""),
                   cnt))
    if g("rnp") and yes(g("rnp")["value"]):
        f.append(F("I_rnp", "info", "В реестре недобросовестных поставщиков (сигнал о поставщике, не о заказчике)", g("rnp")))
    if g("illegal_finance") and yes(g("illegal_finance")["value"]):
        f.append(F("Y21_illegal_fin", "yellow", g("illegal_finance")["value"], g("illegal_finance")))

    for r in sts.get("media_negative", []):
        f.append(Flag("Y17_media", "yellow", f"СМИ: {r['value']}", "hypothesis", r["src_url"], r["observed"],
                      r["src_class"]))
    return f


def _arb_note(sts) -> str:
    n = first(sts, "arb_defendant_count")
    rv = first(sts, "revenue")
    out = (f" (в роли ответчика всего {n['value']} дел; категория спора не разделена — проверить, нет ли исков "
           "о неоплате поставки)") if n else ""
    if rv and rv["year"] and rv["year"] < date.today().year - 2:
        out += f"; выручка взята за {rv['year']} г."
    return out


# какие официальные свойства подтверждают или опровергают красный флаг агрегатора
CROSS = {"R1_status": ("status", "bankruptcy"), "R1_terminated": ("status",), "R2_bankruptcy": ("status", "bankruptcy"),
         "R7_unreliable": ("unreliable_info",), "R5_disq": ("disqualified",), "R4_blocks": ("account_blocks",)}


def crosscheck(flags: list[Flag], sts, R: dict, pb_checked: bool) -> list[Flag]:
    """Правило mOS «одна цифра с одного сайта — гипотеза; факт — два независимых источника или документ»."""
    red_words = tuple(R["status"]["red_words"]) + tuple(R["bankruptcy"]["red_words"]) + tuple(R["status"]["terminated_words"])
    for f in flags:
        if f.level != "red" or f.kind == "fact" or f.rule.startswith("P"):
            continue
        props = CROSS.get(f.rule)
        off = [r for p in props or () for r in sts.get(p, []) if r["kind"] == "fact"]
        if not props or not pb_checked:
            f.check = "единственный источник — агрегатор; подтвердить в первоисточнике"
            continue
        if f.rule in ("R7_unreliable", "R5_disq", "R4_blocks"):
            agree = any(yes(r["value"]) for r in off)
        else:
            agree = any(any(w in str(r["value"]).lower() for w in red_words) for r in off)
        if agree:
            f.check = "подтверждено ФНС"
        else:
            official = next((r["value"] for r in off), "нет записи")
            f.level = "yellow"
            f.check = f"расхождение источников: ФНС — «{official}»; красный снят до сверки"
    return flags


def coverage(sts, store: Store, eid: str, prof: str, today: date):
    nb = store.neighbours(eid)
    ch = {
        "статус": "status" in sts,
        "финансы": prof == "учреждение" or any((r["year"] or 0) >= today.year - 2 for r in sts.get("revenue", [])),
        "суды": any(p.startswith("arb_") for p in sts),
        "собственники": any(n["rel"] == "учредитель" and n["d"] == "in" for n in nb) or "founders_summary" in sts,
        "долги/ФССП": "enforcement_count" in sts or "enforcement_sum" in sts,
    }
    return sum(ch.values()) / len(ch), [k for k, v in ch.items() if not v]


ACTIONS = {
    "red": "не брать в работу без решения риск-менеджера",
    "yellow": "в работу с проверкой отмеченных пунктов",
    "green": "в работу",
    "grey": "данных недостаточно — дособрать до решения",
}


def score_company(store: Store, eid: str, R: dict, own_cache: dict | None = None,
                  today: date | None = None) -> Card:
    from graph.propagate import link_flags   # граф зависит от own_flags этого модуля

    today = today or date.today()
    own_cache = {} if own_cache is None else own_cache
    sts = store.latest(eid)
    card = Card(eid, store.name(eid), inn_of(eid), resolved=is_resolved(eid))
    card.profile = profile_of(sts, R)
    own = crosscheck(own_flags(sts, R, today), sts, R, store.checks(eid).get("pb_nalog") == "нашлось")
    own_cache[eid] = own
    card.flags = own + link_flags(store, eid, R, own_cache, today)
    card.coverage, card.missing = coverage(sts, store, eid, card.profile, today)
    card.unavailable = {ch: res for ch, res in store.checks(eid).items() if res in ("не открылось", "чужая карточка")}
    r = first(sts, "revenue")
    card.revenue = money(r["value"]) if r else None

    top = max((LEVEL[x.level] for x in card.flags), default=0)
    if not card.resolved or "status" not in sts:
        card.color = "grey"
    elif top == 3:
        card.color = "red"
    elif top == 2:
        card.color = "yellow"
    elif card.coverage >= R["coverage"]["green_min"]:
        card.color = "green"
    else:
        card.color = "grey"
    card.action = ACTIONS[card.color]
    if card.color == "red" and all(x.kind != "fact" and x.check != "подтверждено ФНС"
                                   for x in card.flags if x.level == "red"):
        card.action += "; красный флаг по агрегатору — подтвердить в ЕГРЮЛ/ЕФРСБ/КАД"
    return card


def severity(card: dict, R: dict) -> float:
    """Тяжесть жёлтой карточки для очереди глубокого разбора: сумма весов жёлтых флагов (rules.yaml deep.severity),
    факт ФНС весит больше агрегатора, агрегатор — больше гипотезы из СМИ."""
    W = (R.get("deep") or {}).get("severity") or {}
    kind_w = W.get("kind", {"fact": 1.0, "fact2": 0.8, "hypothesis": 0.4})
    rules = W.get("rules", {})
    s = 0.0
    for f in card.get("flags", []):
        if f.get("level") != "yellow":
            continue
        base = next((v for k, v in rules.items() if f["rule"].startswith(k)), W.get("default", 1.0))
        s += base * kind_w.get(f.get("kind"), 0.5)
    return round(s, 2)


def deep_queue(cards: list[dict], R: dict) -> list[tuple[dict, float]]:
    """Очередь глубокого разбора: все красные, затем жёлтые по убыванию тяжести; опознанные по ИНН."""
    reds = [(c, 99.0) for c in cards if c["color"] == "red" and c.get("inn")]
    yel = sorted(((c, severity(c, R)) for c in cards if c["color"] == "yellow" and c.get("inn")),
                 key=lambda x: x[1], reverse=True)
    return reds + [x for x in yel if x[1] > 0]
