"""Разбор ответов pb.nalog.ru и ГИР БО на сохранённых фикстурах (без сети) + ключи."""
import json
from pathlib import Path

from core.ids import inn_valid, norm_case_number, norm_name, ogrn_valid
from core.source_class import classify, kind_of

FIX = Path(__file__).parent / "fixtures"


def test_ids():
    assert inn_valid("9900010015") and not inn_valid("9900010016")
    assert inn_valid("990000200185") and not inn_valid("9900010017")
    assert ogrn_valid("1099900010011")
    assert norm_case_number("а60 12345/25") == "А60-12345/2025"
    assert norm_name("ООО «ТД «Доминант»") == norm_name('ООО "ТД "ДОМИНАНТ"')


def test_source_class():
    assert classify("https://pb.nalog.ru/x") == "official" and kind_of("official") == "fact"
    assert classify("https://www.rusprofile.ru/id/1") == "aggregator" and kind_of("aggregator") == "fact2"
    assert classify("https://t.me/chan") == "media" and kind_of("media") == "hypothesis"


def test_pb_parse_bankrupt():
    """Карточка «Бета-ЛЭП» из pb.nalog.ru: банкротство из ЕГРЮЛ, недостоверность, конкурсный управляющий."""
    from collectors.pb_nalog import PbNalog

    c = json.loads((FIX / "pb_card_9900010022.json").read_text(encoding="utf-8"))
    res = PbNalog.parse(None, "9900010022", {"inn": "9900010022"}, c)
    props = {s.prop: s.value for s in res.statements}
    assert res.page_inn == "9900010022"
    assert "банкрот" in props["status"].lower() and "конкурсн" in props["bankruptcy"].lower()
    assert props["unreliable_info"].startswith("да")
    rels = {l.rel for l in res.links}
    assert "конкурсный_управляющий" in rels and "учредитель" in rels


def test_pb_parse_active_company():
    from collectors.pb_nalog import PbNalog

    c = json.loads((FIX / "pb_card_9900010015.json").read_text(encoding="utf-8"))
    res = PbNalog.parse(None, "9900010015", {"inn": "9900010015"}, c)
    props = {(s.prop, s.year): s.value for s in res.statements}
    assert props[("revenue", 2025)] == "1500000000.0"
    assert res.extra["bo_id"] == "1000001"
    assert {(l.rel, l.other_inn) for l in res.links} >= {("управляющий", "990000200259"),
                                                         ("учредитель", "990000200185")}


def test_checko_parse_roles_and_fssp():
    """Checko: роли в арбитраже, ФССП, заявленное банкротство; ИНН страницы для антиподмены."""
    from collectors.checko import parse

    g = parse("9900010015", "u", (FIX / "checko_9900010015.html").read_text(encoding="utf-8"))
    p = {s.prop: s.value for s in g.statements}
    assert g.page_inn == "9900010015"
    assert p["arb_plaintiff_count"] == "74" and p["arb_defendant_count_total"] == "4"
    assert p["arb_defendant_sum"] == "0" and p["enforcement_sum"] == "0"
    t = parse("9900010030", "u", (FIX / "checko_9900010030.html").read_text(encoding="utf-8"))
    p = {s.prop: s.value for s in t.statements}
    assert p["enforcement_count"] == "2" and float(p["enforcement_sum"]) == 580000
    assert p["bankruptcy"].startswith("заявлено")
    e = parse("9900010022", "u", (FIX / "checko_9900010022.html").read_text(encoding="utf-8"))
    p = {s.prop: s.value for s in e.statements}
    assert p["enforcement_count"].startswith("н/п") and "ЕФРСБ" in p["efrsb_messages"]


def test_media_accept_checks_quote_and_url():
    """Ответ модели принимается, только если цитата дословно из заголовка и url из списка."""
    from llm.media_classify import accept

    heads = [{"title": "Беспилотники атаковали химзавод в Энске", "url": "https://n/1", "date": "x"}]
    ok = accept([{"tag": "production_stop", "quote": "атаковали химзавод", "url": "https://n/1", "harmed": True, "confidence": 0.9}], heads)
    assert len(ok) == 1 and ok[0]["src_class"] == "media"
    assert not accept([{"tag": "production_stop", "quote": "завод закрыт", "url": "https://n/1", "confidence": 0.9}], heads)
    assert not accept([{"tag": "other", "quote": "атаковали", "url": "https://evil/2", "confidence": 0.9}], heads)
    assert not accept([{"tag": "other", "quote": "атаковали", "url": "https://n/1", "confidence": 0.3}], heads)
    assert not accept([{"tag": "nonpayment", "quote": "атаковали химзавод", "url": "https://n/1", "harmed": False,
                        "confidence": 0.9}], heads)


def test_companium_parse_year_window():
    """Companium: иски к компании за последний год (окно 12 мес), ФССП у банкрота, санкции."""
    from collectors.companium import parse

    g = {s.prop: s.value for s in parse("9900010015", "u", (FIX / "companium_9900010015.html").read_text(encoding="utf-8")).statements}
    assert g["arb_plaintiff_count"] == "74" and g["arb_defendant_sum"] == "0" and g["enforcement_sum"] == "0"
    e = parse("9900010022", "u", (FIX / "companium_9900010022.html").read_text(encoding="utf-8"))
    p = {s.prop: s.value for s in e.statements}
    assert e.page_inn == "9900010022" and p["enforcement_count"] == "84" and float(p["enforcement_sum"]) == 96e6
    t = {s.prop: s.value for s in parse("9900010030", "u", (FIX / "companium_9900010030.html").read_text(encoding="utf-8")).statements}
    assert float(t["arb_defendant_sum"]) == 850e6 and t["sanctions"].startswith("да: США")


def test_checko_api_parse_real_fixture():
    """Живой ответ Checko API («Бета-ЛЭП»): окно 12 мес по делам в роли ответчика, остаток долга ФССП."""
    from datetime import date

    from collectors.checko_api import parse_enf, parse_legal

    j = json.loads((FIX / "checko_api_9900010022.json").read_text(encoding="utf-8"))
    lc = j["legal-cases"]["data"]
    p = {s.prop: s.value for s in parse_legal("9900010022", [lc], date(2026, 10, 26))}
    exp = [r for r in lc["Записи"] if r["Дата"] >= "2025-10-26" and any(x["ИНН"] == "9900010022" for x in r["Ответ"])]
    assert p["arb_defendant_count"] == str(len(exp))
    assert abs(float(p["arb_defendant_sum"]) - sum(r.get("СуммИск") or 0 for r in exp)) < 1
    e = {s.prop: s.value for s in parse_enf("9900010022", j["enforcements"]["data"])}
    assert e["enforcement_count"] == "84" and abs(float(e["enforcement_sum"]) - 94800000.0) < 1


def test_checko_api_bankruptcy_case_detected():
    from datetime import date

    from collectors.checko_api import parse_legal

    page = {"ЗапВсего": 1, "Записи": [{"Номер": "А40-1/2026", "Дата": "2026-09-01", "Тип": "Б", "ПрАктив": True,
                                         "Ист": [{"ИНН": "9900010061", "Наим": "Банк"}],
                                         "Ответ": [{"ИНН": "9900010015"}], "СуммИск": 1e6}]}
    p = {s.prop: s.value for s in parse_legal("9900010015", [page], date(2026, 10, 26))}
    assert p["bankruptcy"].startswith("заявлено") and p["arb_defendant_count"] == "1"


def test_checko_api_key_from_env_file(tmp_path, monkeypatch):
    from collectors.checko_api import load_key

    monkeypatch.delenv("CHECKO_API_KEY", raising=False)
    assert load_key(tmp_path) is None
    (tmp_path / ".env").write_text('CHECKO_API_KEY="abc123"\n', encoding="utf-8")
    assert load_key(tmp_path) == "abc123"


def test_api_result_enriched_from_cached_web(tmp_path):
    """API по ключу закрывает суды/ФССП, а санкции/блокировки добираются из кэша веб-карточки без сети."""
    from collectors.base import CollectResult, Statement
    from collectors.companium import FirstAvailable
    from core.http import FileCache

    class Api:
        web, ch = False, "checko_api"
        cache = FileCache(tmp_path)

        def collect(self, inn):
            return CollectResult("checko_api", True, inn, inn, statements=[Statement("arb_defendant_sum", "0", "u", "aggregator")])

    class Web:
        web, ch = True, "companium"
        cache = FileCache(tmp_path)

        def collect(self, inn):
            return CollectResult("companium", True, inn, inn, statements=[
                Statement("arb_defendant_sum", "999", "w", "aggregator"), Statement("sanctions", "да: США", "w", "aggregator")])

    Web.cache.put("companium", "9900010015", {"url": "w", "page": ""})
    r = FirstAvailable(Api(), Web(), log=lambda *_: None).collect("9900010015")
    p = {s.prop: s.value for s in r.statements}
    assert p == {"arb_defendant_sum": "0", "sanctions": "да: США"}
