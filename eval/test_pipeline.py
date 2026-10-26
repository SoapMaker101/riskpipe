"""Конвейер с фейковыми коннекторами: переиспользование базы, возобновление после падения, протечки."""
import pytest
from conftest import FL, INN, T

from collectors.base import CollectResult, Link, Statement
from db.store import Store
from pipeline.run import Pipeline

U = "https://pb.nalog.ru/fake"


class FakePb:
    """Все компании принадлежат одному ФЛ; можно «уронить» процесс на N-й компании."""

    def __init__(self, crash_on=None):
        self.calls = {"collect": 0, "person": 0}
        self.crash_on = crash_on

    def collect(self, inn):
        self.calls["collect"] += 1
        if self.crash_on and self.calls["collect"] == self.crash_on:
            raise KeyboardInterrupt("процесс убит посреди прогона")
        return CollectResult("pb_nalog", True, inn, inn, f"Компания {inn}", entity_type="ЮЛ",
                             statements=[Statement("status", "Действующая организация", U, "official"),
                                         Statement("revenue", "1000000000", U, "official", year=2025)],
                             links=[Link("учредитель", "in", "Владелец", FL["a"], "ФЛ", U, "official")])

    def person_companies(self, inn):
        self.calls["person"] += 1
        return CollectResult("pb_person", True, inn, inn, "Владелец", entity_type="ФЛ",
                             links=[Link("учредитель", "out", f"К{k}", v, "ЮЛ", U, "official")
                                    for k, v in INN.items()])

    def resolve(self, name):
        return None, []


ROWS = [{"name": f"Компания {k}", "inn": v, "query": v, "origin": "тест"} for k, v in INN.items()]


def make(tmp_path, pb, run="r1"):
    return Pipeline(Store(tmp_path / "p.db"), run, T, pb=pb, log=lambda *_: None)


def test_reuse_person_node(tmp_path):
    """Второй заказчик с тем же владельцем берёт узел ФЛ из базы, без нового запроса связей."""
    pb = FakePb()
    p = make(tmp_path, pb)
    p.load(ROWS)
    p.collect_all()
    assert pb.calls["collect"] == len(ROWS)
    assert pb.calls["person"] == 1


def test_resume_after_kill_no_dups_no_losses(tmp_path):
    """Процесс убит посреди прогона -> перезапуск: ни дублей, ни потерь."""
    p = make(tmp_path, FakePb(crash_on=5))
    p.load(ROWS)
    with pytest.raises(KeyboardInterrupt):
        p.collect_all()
    done = [r for r in p.st.queue("r1") if r["status"] != "new"]
    assert len(done) == 4
    p2 = make(tmp_path, FakePb())
    p2.load(ROWS)                       # повторная загрузка той же очереди не плодит строк
    p2.collect_all()
    q = p2.st.queue("r1")
    assert len(q) == len(ROWS) and all(r["status"] == "collected" for r in q)
    dups = p2.st.db.execute("SELECT entity, prop, value, COUNT(*) n FROM statement GROUP BY entity, prop, value, "
                            "year, src_url, run_id HAVING n > 1").fetchall()
    assert not dups
    cards = p2.score_all()
    assert len(cards) == len(ROWS) and len({c["eid"] for c in cards}) == len(ROWS)


def test_unresolved_row_is_grey(tmp_path):
    p = make(tmp_path, FakePb())
    p.load([{"name": "ООО «Без ИНН»", "inn": None, "query": "Без ИНН", "origin": "тест"}])
    p.collect_all()
    c = p.score_all()[0]
    assert c["color"] == "grey" and c["eid"].startswith("ul?:")
