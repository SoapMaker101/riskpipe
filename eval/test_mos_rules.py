"""Правила поиска mOS, встроенные в конвейер: дела в графе, адрес, подсказки, закрытие группы,
перекрёстная проверка красных, импорт графа mOS, методика из файлов mOS."""
from conftest import FL, INN, T, U, W  # noqa: F401

from collectors.base import CollectResult, Statement
from graph.cases import court_picture, ingest_cases


def ul(k):
    return f"ul:{INN[k]}"


CASE = {"number": "А60-1/2026", "date": "2026-06-01", "type": "экономический спор", "court": "АС Свердловской обл.",
        "sum": 4e6, "active": True, "outcome": "Требования удовлетворены", "winner": "Истец",
        "url": "https://kad.arbitr.ru/Card/x", "plaintiffs": [{"inn": INN["Y"], "name": "Поставщик"}],
        "defendants": [{"inn": INN["X"], "name": "Заказчик"}]}


def test_cases_are_graph_nodes_and_counterparties_do_not_carry_risk(w):
    """Суды по составу: дело — узел, истец — контрагент; банкротство истца риск на заказчика НЕ переносит."""
    from datetime import date

    w.company("X"); w.company("Y", status="Процесс банкротства")
    ingest_cases(w.st, ul("X"), [CASE], "t", T)
    rels = {(n["rel"], n["other"]) for n in w.st.neighbours(ul("X"))}
    assert ("ответчик", "дело:А60-1/2026") in rels and ("контрагент", ul("Y")) in rels
    assert w.score(ul("X")).color == "green"
    pic = court_picture(w.st, ul("X"), date(2026, 10, 26))
    assert "1 дел от 1 истцов" in pic[0] and "ТЕСТ-Y" in pic[1]


def test_address_mates_small_is_edge_mass_is_not(w):
    """Адрес по mOS: немного соседей — связь (вес 0.2, только «для сведения»); массовый адрес — не связь."""
    from collectors.pb_nalog import PbNalog

    card = {"vyp": {"ИНН": INN["X"], "sulst_name_ex": "Действующая организация"},
            "masaddress": [{"massinn": INN["Y"], "massnamec": "Сосед"}]}
    res = PbNalog.parse(None, INN["X"], {"inn": INN["X"]}, card)
    assert [l.rel for l in res.links] == ["тот_же_адрес"] and "соседей по адресу: 1" in res.extra["address"]
    card["masaddress"] = [{"massinn": INN[k], "massnamec": k} for k in ("Y", "Z", "P", "Q", "S", "B")]
    res = PbNalog.parse(None, INN["X"], {"inn": INN["X"]}, card)
    assert not res.links and res.extra["address"].startswith("массовый")


def test_address_neighbour_bankrupt_is_info_only(w):
    w.company("X"); w.company("S", status="Процесс банкротства")
    w.link(ul("X"), ul("S"), "тот_же_адрес")
    c = w.score(ul("X"))
    assert c.color == "green" and any(f.rule == "P1_link" and f.level == "info" for f in c.flags)


def test_pick_by_hints():
    """Правило mOS «по подсказкам»: из тёзок выбран тот, чей ОКВЭД в отрасли; равные — не выбираем."""
    from collectors.pb_nalog import pick_by_hints

    cands = [{"inn": "1", "okved": "47.11", "region": "МОСКВА"}, {"inn": "2", "okved": "46.72", "region": "ПЕРМСКИЙ"}]
    assert pick_by_hints(cands, ["46.72"], None)["inn"] == "2"
    assert pick_by_hints([{"inn": "1", "okved": "46.72"}, {"inn": "2", "okved": "46.72"}], ["46.72"], None) is None
    assert pick_by_hints(cands, ["35.1"], None) is None


def test_group_status(w):
    from graph.expand import group_status

    eid = w.company("X")
    closed, gaps = group_status(w.st, eid, w.R)
    assert not closed and {"учредители", "адрес", "суды"} <= set(gaps)
    for ch in ("pb_nalog", "адрес", "checko_api"):
        w.st.log_check(eid, ch, "нашлось", "t", T)
    w.link(f"fl:{FL['a']}", eid, "учредитель")
    closed, gaps = group_status(w.st, eid, w.R)
    assert not closed and any("компании ФЛ" in g for g in gaps)
    w.st.log_check(f"fl:{FL['a']}", "pb_person", "нашлось", "t", T)
    assert group_status(w.st, eid, w.R) == (True, [])


def test_crosscheck_aggregator_red(w):
    """Одна цифра с одного сайта — гипотеза: красный агрегатора против ФНС."""
    eid = w.company("X")                         # статус ФНС «Действующая»
    w.st.add_statement(eid, {"prop": "unreliable_info", "value": "да: адрес", "src_url": "https://www.rusprofile.ru/x",
                             "src_class": "aggregator"}, "t", T)
    w.st.log_check(eid, "pb_nalog", "нашлось", "t", T)
    c = w.score(eid)
    f = [x for x in c.flags if x.rule == "R7_unreliable"][0]
    assert f.level == "yellow" and f.check.startswith("расхождение") and c.color == "yellow"
    w.st.add_statement(eid, {"prop": "unreliable_info", "value": "да: адрес", "src_url": U, "src_class": "official"}, "t", T)
    c = w.score(eid)
    assert c.color == "red"


def test_crosscheck_confirms_bankruptcy(w):
    eid = w.company("X", status="Процесс банкротства; конкурсное производство")
    w.st.add_statement(eid, {"prop": "bankruptcy", "value": "конкурсное производство", "src_url": "https://www.rusprofile.ru/x",
                             "src_class": "aggregator"}, "t", T)
    w.st.log_check(eid, "pb_nalog", "нашлось", "t", T)
    c = w.score(eid)
    assert [x.check for x in c.flags if x.rule == "R2_bankruptcy"] == ["подтверждено ФНС"]
    assert "подтвердить" not in c.action


def test_many_suits_rule(w):
    """≥5 исков за 12 мес: жёлтый, только если сумма ≥ 3 % выручки (без категории спора счёт шумит у сетей и МУП)."""
    def add(eid, n, s):
        for prop, val in (("arb_defendant_count", n), ("arb_defendant_sum", s)):
            w.st.add_statement(eid, {"prop": prop, "value": val, "src_url": "https://checko.ru/search?query=1",
                                     "src_class": "aggregator"}, "t", T)
    eid = w.company("X")
    add(eid, "7", "50000000")                         # 5 % выручки 1 млрд
    assert any(f.rule == "Y20_many_suits" and f.level == "yellow" for f in w.score(eid).flags)
    eid2 = w.company("Y")
    add(eid2, "7", "100000")
    assert [f.level for f in w.score(eid2).flags if f.rule == "Y20_many_suits"] == ["info"]


def test_sanctions_only_ukraine_is_background(w):
    eid = w.company("X", sanctions="да: Украина")
    c = w.score(eid)
    assert c.color == "green" and any(f.rule == "I_sanctions_ua" for f in c.flags)
    assert w.score(w.company("Y", sanctions="да: США, Украина")).color == "yellow"


def test_mos_graph_import_rules(w):
    """Импорт графа mOS: битый ИНН и связь вне словаря отбрасываются, гипотеза риск не переносит."""
    from collectors.mos_graph import ingest_records

    recs = [{"k": "n", "id": ul("X"), "t": "ЮЛ", "name": "X", "src": "[веб, https://site.ru, 2026-10-26]"},
            {"k": "e", "from": "ul:9900010016", "to": ul("X"), "rel": "учредитель", "src": "[https://a.ru]"},
            {"k": "e", "from": ul("S"), "to": ul("X"), "rel": "связан_с", "src": "[https://a.ru]"},
            {"k": "e", "from": ul("S"), "to": ul("X"), "rel": "учредитель", "src": "без ссылки"},
            {"k": "e", "from": ul("S"), "to": ul("X"), "rel": "учредитель", "conf": "гипотеза",
             "src": "[СМИ, https://news.ru/1, 2026-10-01]"},
            {"k": "c", "node": ul("X"), "ch": "суды", "res": "не_открылось"}]
    s = ingest_records(w.st, recs, "mos:t", T)
    assert s["nodes"] == 1 and s["edges"] == 1 and len(s["rejected"]) == 3 and s["checks"] == 1
    w.company("X"); w.company("S", status="Процесс банкротства")
    assert w.score(ul("X")).color == "green"          # ребро-гипотеза не переносит банкротство


def test_method_loaded_from_mos_files():
    """Методика глубокого разбора подтягивается из файлов mOS (сжато для бюджета), а не написана заново."""
    from deep.method import critic_system, research_system_compact, writer_system

    s, used = research_system_compact()
    assert any(k in u for u in used for k in ("граф_связей", "graph.md"))          # mOS (MOS_ROOT) или methodology/
    assert any(k in u for u in used for k in ("судебные_дела", "courts.md"))
    assert len(s) < 6000                                  # бюджет ~30 тыс. токенов на красную
    assert "url" in s and "Нет источника — нет утверждения" in writer_system()
    assert "критик" in critic_system().lower()


def test_cached_only_web_is_not_a_check(tmp_path):
    """Выключенная витрина — «не проверялось», а не «пусто»."""
    from collectors.companium import CachedOnly, Companium
    from core.http import FileCache, Http

    r = CachedOnly(Companium(Http(), FileCache(tmp_path))).collect(INN["X"])
    assert not r.ok and "выключены" in r.error


def test_deep_quotes_must_be_verbatim():
    """Реквизит из акта — с цитатой: находка без дословной цитаты в тексте материала отбрасывается."""
    from deep.analyze import verify_quotes

    mats = [{"url": "https://sudact.ru/arbitral/doc/x/", "text": "Признать ООО «Бета-ЛЭП» несостоятельным (банкротом). "
                                                            "Открыть конкурсное производство сроком на шесть месяцев."}]
    ok, dropped = verify_quotes([
        {"claim": "банкрот", "url": "https://sudact.ru/arbitral/doc/x/", "quote": "Открыть конкурсное производство сроком"},
        {"claim": "пересказ", "url": "https://sudact.ru/arbitral/doc/x/", "quote": "company declared bankrupt by court"},
        {"claim": "чужой url", "url": "https://evil/", "quote": "Открыть конкурсное производство сроком"}], mats)
    assert len(ok) == 1 and dropped == 2


def test_deep_key_cases_and_act_excerpt(w):
    """Ключевые дела: сначала активные банкротные; из акта — шапка и резолютивная часть."""
    from datetime import date

    from deep.materials import act_excerpt, key_cases

    w.company("X")
    ingest_cases(w.st, ul("X"), [CASE, {**CASE, "number": "А41-9/2025", "type": "банкротство", "sum": 0,
                                         "date": "2025-06-01"}], "t", T)
    ks = key_cases(w.st, ul("X"), date(2026, 10, 26))
    assert ks[0]["number"] == "А41-9/2025" and ks[1]["number"] == "А60-1/2026"
    text = "мусор шапки сайта ИМЕНЕМ РОССИЙСКОЙ ФЕДЕРАЦИИ " + "установил " * 400 + "Р Е Ш И Л: взыскать 4 512 000 руб."
    ex = act_excerpt(text)
    assert ex.startswith("ИМЕНЕМ") and ex.rstrip().endswith("руб.") and "[…]" in ex


def test_frontier_order_and_wave(w):
    """Фронтир mOS: неразобранные соседи-ЮЛ по связям риска; больше связей — раньше; контрагенты — не фронтир.
    Волна раскрывает узел карточкой ФНС, и банкротство владельца переносится правилами."""
    from collectors.base import CollectResult, Statement
    from graph.expand import group_status
    from graph.frontier import frontier, wave

    eid = w.company("X")
    w.link(ul("P"), eid, "учредитель", 100)                 # владелец — две связи
    w.link(ul("P"), eid, "тот_же_адрес")
    w.link(eid, ul("Z"), "тот_же_адрес")                    # сосед по адресу — одна связь
    w.link(ul("Y"), eid, "контрагент")                      # контрагент — не фронтир
    assert [u for u, _ in frontier(w.st, eid, w.R)] == [ul("P"), ul("Z")]
    assert any(g.startswith("не разобран") for g in group_status(w.st, eid, w.R)[1])

    class FakePb:
        def collect(self, inn):
            return CollectResult("pb_nalog", True, inn, inn, "Владелец", entity_type="ЮЛ",
                                 statements=[Statement("status", "Процесс банкротства", U, "official")])
    st = wave(w.st, eid, FakePb(), w.R, "t", T, limit=1)
    assert st["opened"] == 1 and frontier(w.st, eid, w.R) == [(ul("Z"), 1)]
    assert w.score(eid).color == "red"                     # материнская ≥ 50 % в банкротстве


def test_web_reader_skips_showcases():
    from deep.web import is_showcase, window

    assert is_showcase("https://www.rusprofile.ru/id/1") and is_showcase("https://checko.ru/company/x")
    assert not is_showcase("https://evro-lep.ru/") and not is_showcase("https://pravda-sotrudnikov.ru/company/x")
    t = "шапка " * 2000 + "ООО «Бета-ЛЭП» задерживает зарплату " + "хвост " * 2000
    assert "задерживает зарплату" in window(t, "ООО «Бета-ЛЭП»", "9900010022", 600)


def test_children_from_checko_and_frontier_priority(w):
    """Дочки из Checko API (СвязУчред) — связь владения со статусом; дочка в банкротстве даёт жёлтый.
    Фронтир: владелец раньше правопредшественника, даже если тот держится на большем числе связей."""
    from collectors.checko_api import parse_company
    from graph.frontier import frontier

    S, L = parse_company(INN["X"], {"СвязУчред": [{"ИНН": INN["S"], "НаимСокр": "Дочка",
                                                   "Статус": "В процессе банкротства"}]})
    assert [(l.rel, l.dir, l.other_status) for l in L] == [("учредитель", "out", "В процессе банкротства")]
    eid = w.company("X")
    w.st.ingest(CollectResult("checko_api", True, INN["X"], INN["X"], links=L), "t", T, requested_inn=INN["X"])
    c = w.score(eid)
    assert c.color == "yellow" and any(f.rule == "P1_link" and "Дочка" in f.text for f in c.flags)

    e2 = w.company("Y")
    w.link(ul("Z"), e2, "правопредшественник"); w.link(e2, ul("Z"), "тот_же_адрес")
    w.link(ul("P"), e2, "учредитель")
    assert [u for u, _ in frontier(w.st, e2, w.R)][0] == ul("P")


def test_merged_predecessor_is_not_risk(w):
    """Правопредшественник, присоединённый к заказчику, «прекратил деятельность» — это норма, не риск.
    А вот правопредшественник в банкротстве — сигнал «переезда от долгов»."""
    eid = w.company("X")
    w.company("Z", status="Деятельность прекращена; Прекратило деятельность при присоединении с 13.12.2012")
    w.link(ul("Z"), eid, "правопредшественник")
    assert w.score(eid).color == "green"
    e2 = w.company("Y")
    w.company("S", status="Процесс банкротства")
    w.link(ul("S"), e2, "правопредшественник")
    assert w.score(e2).color == "yellow"


def card(name, color, flags, inn="9900010015"):
    return {"eid": f"ul:{inn}:{name}", "inn": inn, "name": name, "color": color, "explain": "", "flags": flags,
            "coverage": 1.0, "action": "a"}


def fl(rule, kind="fact"):
    return {"rule": rule, "level": "yellow", "kind": kind, "text": rule}


def test_deep_queue_reds_first_then_heaviest_yellows(w):
    from scoring.score import deep_queue

    cards = [card("СМИ", "yellow", [fl("Y17_media", "hypothesis")]),
             card("Дочка-банкрот", "yellow", [fl("P1_link"), fl("Y10_tax")]),
             card("Красная", "red", []),
             card("Санкции", "yellow", [fl("Y6_sanctions", "fact")]),
             card("Зелёная", "green", []),
             card("Без ИНН", "yellow", [fl("P1_link")], inn=None)]
    q = [c["name"] for c, _ in deep_queue(cards, w.R)]
    assert q == ["Красная", "Дочка-банкрот", "Санкции", "СМИ"]


def test_deep_run_stops_at_run_budget(tmp_path, monkeypatch):
    """Бюджет прогона: разбор идёт, пока в остатке есть оценка на одну компанию; кэш бюджет не тратит."""
    import deep.analyze as da
    from db.store import Store
    from pipeline.run import Pipeline

    monkeypatch.setattr(da, "deep_cfg", lambda: {"run_budget_tokens": 70000, "est_tokens_per_company": 30000,
                                                 "max_companies": 10})
    calls = []

    def fake(store, c, run_id, usage, why, **kw):
        calls.append(c["name"])
        return {"status": "ok", "summary": "s", "tokens": 0 if c["name"] == "Кэш" else 30000, "critic": {}}
    monkeypatch.setattr(da, "analyze", fake)
    monkeypatch.setattr(da, "render", lambda c, d: "")
    p = Pipeline(Store(tmp_path / "d.db"), "r", T, log=lambda *_: None)
    cards = [card("Красная", "red", []), card("Кэш", "yellow", [fl("P1_link")]),
             card("Жёлтая-1", "yellow", [fl("Y9_fssp")]), card("Жёлтая-2", "yellow", [fl("Y12_loss")])]
    p.deep_run(cards, None, tmp_path)
    assert calls == ["Красная", "Кэш", "Жёлтая-1"]            # 30k + 0 + 30k; на четвёртую остатка нет
    assert any("бюджет прогона" in j and "Жёлтая-2" in j for j in p.journal)
