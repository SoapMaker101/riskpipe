import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collectors.base import CollectResult, Statement  # noqa: E402
from db.store import Store  # noqa: E402
from scoring.score import load_rules, score_company  # noqa: E402

T = "2026-10-26"
OFF = "official"
U = "https://pb.nalog.ru/test"

# валидные ИНН (контрольная сумма сходится)
INN = {"X": "9900010015", "Y": "9900010047", "Z": "9900010054", "P": "9900010061", "Q": "9900010079",
       "S": "9900010086", "B": "9900010093", "W": "9900010103"}
FL = {"a": "990000200185", "b": "990000200259", "c": "990000200322"}


class W:
    """Обёртка над чистой базой с короткими помощниками для синтетики."""

    def __init__(self, path):
        self.st = Store(path)
        self.R = load_rules()

    def company(self, key, status="Действующая организация", full=True, **props):
        inn = INN[key]
        sts = [Statement("status", status, U, OFF)]
        if full:
            sts += [Statement("revenue", "1000000000", U, OFF, year=2025),
                    Statement("arb_total_count", "0", "https://www.rusprofile.ru/t", "aggregator"),
                    Statement("enforcement_sum", "0", "https://www.rusprofile.ru/t", "aggregator")]
        for k, v in props.items():
            cls = "media" if k == "media_negative" else OFF
            sts.append(Statement(k, v, "https://t.me/x" if cls == "media" else U, cls))
        res = CollectResult("test", True, inn, inn, f"ТЕСТ-{key}", entity_type="ЮЛ", statements=sts)
        return self.st.ingest(res, "t", T, requested_inn=inn)

    def link(self, src, dst, rel, share=None, cls=OFF):
        self.st.upsert_entity(src, "ФЛ" if src.startswith("fl") else "ЮЛ", src, None, T)
        self.st.upsert_entity(dst, "ЮЛ", dst, None, T)
        self.st.add_edge(src, dst, rel, share, U if cls != "media" else "https://t.me/x", cls, "t", T)
        self.st.db.commit()

    def score(self, eid):
        return score_company(self.st, eid, self.R, {}, date.fromisoformat(T))


@pytest.fixture
def w(tmp_path):
    return W(tmp_path / "t.db")
