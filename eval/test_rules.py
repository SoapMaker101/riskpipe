"""Правила и граф на синтетике (кейсы раздела Eval техпроекта, проверяемые без сети)."""
from conftest import FL, INN, T, U, W  # noqa: F401

from cards.validate import validate_card
from collectors.base import CollectResult, Statement


def ul(k):
    return f"ul:{INN[k]}"


def fl(k):
    return f"fl:{FL[k]}"


def test_clean_company_green(w):
    assert w.score(w.company("X")).color == "green"


def test_sister_bankrupt_via_common_founder_gives_yellow(w):
    """Чистая компания, «сестра» через общего учредителя-ФЛ в банкротстве -> жёлтый со ссылкой."""
    w.company("X"); w.company("S", status="Процесс банкротства; конкурсное производство")
    w.link(fl("a"), ul("X"), "учредитель"); w.link(fl("a"), ul("S"), "учредитель")
    c = w.score(ul("X"))
    p = [f for f in c.flags if f.rule == "P2_sister"]
    assert c.color == "yellow" and p and p[0].via == ul("S"), c.flags


def test_child_bankrupt_gives_yellow(w):
    """Перенос вверх: дочка ≥50 % в банкротстве -> жёлтый (вес 0.7)."""
    w.company("X"); w.company("S", status="Процесс банкротства")
    w.link(ul("X"), ul("S"), "учредитель", 100)
    c = w.score(ul("X"))
    assert c.color == "yellow" and any(f.rule == "P1_link" for f in c.flags)


def test_parent_bankrupt_gives_red(w):
    """Перенос вниз: материнская ≥50 % в банкротстве -> красный."""
    w.company("Q"); w.company("P", status="Процесс банкротства")
    w.link(ul("P"), ul("Q"), "учредитель", 100)
    c = w.score(ul("Q"))
    assert c.color == "red" and any(f.rule == "P1_link" and f.level == "red" for f in c.flags)


def test_namesake_without_inn_no_transfer(w):
    """Тёзка владельца без ИНН: цвет не меняется, связь только «для сведения»."""
    w.company("X"); w.company("S", status="Процесс банкротства")
    w.link("fl?:иванов_иван", ul("X"), "учредитель"); w.link("fl?:иванов_иван", ul("S"), "учредитель")
    c = w.score(ul("X"))
    assert c.color == "green", c.flags
    assert any(f.rule == "I_link_hypo" for f in c.flags)


def test_media_link_no_transfer(w):
    """Связь из СМИ (гипотеза) не переносит риск."""
    w.company("Q"); w.company("P", status="Процесс банкротства")
    w.link(ul("P"), ul("Q"), "учредитель", 100, cls="media")
    assert w.score(ul("Q")).color == "green"


def test_mass_person_no_transfer(w):
    """ФЛ с > 20 компаниями даёт вес 0.1: банкротство одной из них не окрашивает заказчика."""
    w.company("X"); w.company("S", status="Процесс банкротства")
    w.link(fl("a"), ul("X"), "руководитель"); w.link(fl("a"), ul("S"), "руководитель")
    w.st.add_statement(fl("a"), {"prop": "companies_count", "value": "45", "src_url": U,
                                 "src_class": "official"}, "t", T)
    c = w.score(ul("X"))
    assert c.color == "green", c.flags


def test_arbitration_manager_no_transfer(w):
    """Конкурсный управляющий ведёт чужие банкротства — через него риск не идёт."""
    w.company("X"); w.company("S", status="Процесс банкротства")
    w.link(fl("c"), ul("X"), "конкурсный_управляющий"); w.link(fl("c"), ul("S"), "конкурсный_управляющий")
    assert w.score(ul("X")).color == "green"


def test_manager_by_any_source_no_transfer(w):
    """Агрегатор пишет «руководитель», ЕГРЮЛ — «конкурсный управляющий»: риск не переносится."""
    w.company("X"); w.company("S", status="Процесс банкротства")
    w.link(fl("c"), ul("X"), "руководитель", cls="aggregator")
    w.link(fl("c"), ul("X"), "конкурсный_управляющий"); w.link(fl("c"), ul("S"), "руководитель")
    assert w.score(ul("X")).color == "green"


def test_many_sisters_one_flag(w):
    """Несколько «сестёр» через одного человека — один флаг с перечнем."""
    w.company("X"); w.company("S", status="Процесс банкротства"); w.company("Y", status="Процесс банкротства")
    w.link(fl("a"), ul("X"), "учредитель"); w.link(fl("a"), ul("S"), "учредитель"); w.link(fl("a"), ul("Y"), "учредитель")
    p = [f for f in w.score(ul("X")).flags if f.rule == "P2_sister"]
    assert len(p) == 1 and "2 компаний" in p[0].text


def test_terminated_sister_not_transferred(w):
    """Давно прекращённая «сестра» — не сигнал (в шаг 2 идут только процедуры, а не прекращение)."""
    w.company("X"); w.company("S", status="Прекращение деятельности юридического лица путем исключения")
    w.link(fl("a"), ul("X"), "учредитель"); w.link(fl("a"), ul("S"), "учредитель")
    assert w.score(ul("X")).color == "green"


def test_media_only_yellow(w):
    """Только жёлтая пресса -> жёлтый, не красный, вид «гипотеза»."""
    c = w.score(w.company("X", media_negative="Telegram: владелец якобы под следствием"))
    assert c.color == "yellow" and all(f.level != "red" for f in c.flags)
    assert [f.kind for f in c.flags if f.rule == "Y17_media"] == ["hypothesis"]


def test_unreliable_from_media_only_yellow(w):
    """Недостоверность по СМИ — только жёлтый (по ФНС — красный)."""
    eid = w.company("X")
    w.st.add_statement(eid, {"prop": "unreliable_info", "value": "да: адрес", "src_url": "https://t.me/y",
                             "src_class": "media"}, "t", T)
    assert w.score(eid).color == "yellow"
    w.st.add_statement(eid, {"prop": "unreliable_info", "value": "да: адрес", "src_url": U,
                             "src_class": "official"}, "t", T)
    assert w.score(eid).color == "red"


def test_substitution_rejected(w):
    """Подменная страница (ИНН не сходится) -> запись отброшена, в журнале «чужая карточка»."""
    res = CollectResult("pb_nalog", True, inn=INN["X"], page_inn="9900010016", name="Больница",
                        statements=[Statement("status", "Действующая организация", U, "official")])
    assert w.st.ingest(res, "t", T, requested_inn=INN["X"]) is None
    assert w.st.latest(ul("X")) == {}
    assert w.st.checks(ul("X"))["pb_nalog"] == "чужая карточка"
    res2 = CollectResult("pb_nalog", True, inn=INN["X"], page_inn=INN["Y"], name="Другая")
    assert w.st.ingest(res2, "t", T, requested_inn=INN["X"]) is None


def test_no_source_no_statement(w):
    assert not w.st.add_statement(ul("X"), {"prop": "status", "value": "x", "src_url": None}, "t", T)


def test_empty_card_grey(w):
    """Компания моложе года / источники закрыты -> серый, не зелёный."""
    c = w.score(w.company("B", full=False))
    assert c.color == "grey" and c.missing


def test_young_company_yellow(w):
    c = w.score(w.company("X", reg_date="2026-03-01"))
    assert c.color == "yellow" and any(f.rule == "Y15_young" for f in c.flags)


def test_revenue_drop(w):
    eid = w.company("X")
    w.st.add_statement(eid, {"prop": "revenue", "value": "2000000000", "year": 2024, "src_url": U,
                             "src_class": "official"}, "t", T)
    c = w.score(eid)
    assert any(f.rule == "Y19_rev_drop" for f in c.flags) and c.color == "yellow"


def test_budget_profile_ignores_finance(w):
    """Учреждение: убыток и отрицательные активы не флагуются (бюджетная бухгалтерия)."""
    eid = w.company("X", okopf="75404", net_profit="-5000000")
    c = w.score(eid)
    assert c.profile == "учреждение" and c.color == "green", c.flags
    eid2 = w.company("Y", okopf="65243", net_profit="-5000000")
    c2 = w.score(eid2)
    assert c2.profile == "унитарное" and any(f.rule == "Y12_loss" for f in c2.flags)


def test_tax_debt_threshold(w):
    c = w.score(w.company("X", tax_debt="38 800 ₽ на 10.2026"))
    assert c.color == "green" and any(f.rule == "I_tax" for f in c.flags)
    c = w.score(w.company("Y", tax_debt="2 500 000 ₽ на 10.2026"))
    assert c.color == "yellow"


def test_two_inns_no_leak(w):
    """Два соседних ИНН: у каждой карточки только свои факты."""
    w.company("X", media_negative="Только про X")
    w.company("Y", reg_date="2026-05-01")
    cx, cy = w.score(ul("X")), w.score(ul("Y"))
    assert all("Только про X" not in f.text for f in cy.flags)
    assert all(f.rule != "Y15_young" for f in cx.flags)


def test_stability(w):
    """Один ИНН три раза подряд: тот же цвет и те же флаги."""
    eid = w.company("X", tax_debt="2 500 000 ₽")
    runs = [w.score(eid).to_dict() for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


def test_card_validator():
    good = {"eid": "ul:9900010015", "inn": "9900010015", "name": "X", "color": "yellow", "action": "a",
            "coverage": 1.0, "missing": [], "run_id": "r", "origin": "o",
            "flags": [{"rule": "Y17_media", "level": "yellow", "kind": "hypothesis", "text": "t",
                       "url": "https://t.me/x", "observed": "2026-10-26", "src_class": "media"}]}
    assert validate_card(good) == []
    bad = {**good, "flags": [{**good["flags"][0], "level": "red"}]}
    assert any("гипотез" in e for e in validate_card(bad))
    nourl = {**good, "flags": [{k: v for k, v in good["flags"][0].items() if k != "url"}]}
    assert validate_card(nourl)
    assert validate_card({**good, "color": "green"})
    assert validate_card({**good, "eid": "ul?:x", "inn": None, "flags": [], "color": "green"})


def test_roles_replace_temporary_rule(w):
    """Есть роли в арбитраже: «суды без роли» не срабатывает; истец по крупным суммам — не риск."""
    eid = w.company("X", arb_recent_years="2026: 6 на 551000000", arb_defendant_sum="0", arb_defendant_count="0")
    c = w.score(eid)
    assert c.color == "green" and not any(f.rule == "Y18_arb_year" for f in c.flags)
    eid2 = w.company("Y", arb_defendant_sum="400000000", arb_defendant_count="5")
    assert any(f.rule == "R8_arb" for f in w.score(eid2).flags)
