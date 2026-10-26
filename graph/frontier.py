"""Волна фронтира (методика mOS «ищи волнами по фронтиру, а не темами») — кодом, без LLM.

Фронтир заказчика — ЮЛ на одном шаге по связям, через которые идёт риск (владелец, дочка, управляющая
организация, правопредшественник, тот же адрес, связи из глубокого разбора), о которых ещё ничего не известно:
нет статуса и не было карточки ФНС. Порядок — как у graph.py frontier: сначала узлы, которые держатся на
нескольких связях, затем факты раньше гипотез. Каждый узел раскрывается официальной карточкой ФНС
(pb.nalog.ru: статус, процедура банкротства, учредители, руководитель, адрес) — новые узлы и рёбра идут в граф,
а перенос риска пересчитывается правилами.
"""
from __future__ import annotations

from db.store import Store

SKIP_RELS = {"контрагент", "истец", "ответчик", "третье_лицо", "упоминание"}
# что важнее раскрыть: владелец и управляющая компания переносят риск сильнее всего, правопредшественник —
# чаще всего давно присоединённая фирма (в первом прогоне 6 из 21 раскрытых были такими)
REL_PRIORITY = {"учредитель": 3, "материнская": 3, "управляющая_организация": 3, "дочернее": 2, "дочерняя": 2,
                "участник_группы": 2, "поручитель": 2, "тот_же_адрес": 1, "общий_адрес": 1, "кредитор": 1,
                "аффилирован": 1, "правопредшественник": 0.5}


def frontier(store: Store, eid: str, R: dict, limit: int = 3) -> list[tuple[str, int]]:
    carry = set(R["graph"]["carry_rels"]) | {"участник_группы", "дочернее", "поручитель", "кредитор", "аффилирован"}
    score: dict[str, list] = {}
    for n in store.neighbours(eid):
        u = n["other"]
        if not u.startswith("ul:") or n["rel"] in SKIP_RELS or n["rel"] not in carry:
            continue
        score.setdefault(u, []).append((REL_PRIORITY.get(n["rel"], 1), 0 if n["kind"] == "hypothesis" else 1))
    out = []
    for u, links in score.items():
        if store.checks(u).get("pb_nalog") or store.latest(u).get("status"):
            continue                                   # уже разобран — не фронтир
        out.append((u, len(links), max(p for p, _ in links), max(k for _, k in links)))
    # важность связи, затем сколько связей держат узел (как graph.py frontier), затем факт раньше гипотезы
    out.sort(key=lambda x: (x[2], x[1], x[3]), reverse=True)
    return [(u, n) for u, n, _, _ in out[:limit]]


def unresolved(store: Store, eid: str, R: dict) -> list[str]:
    """Для статуса группы: соседи-ЮЛ на одном шаге, которые так и не разобраны."""
    return [store.name(u) for u, _ in frontier(store, eid, R, limit=50)]


def wave(store: Store, eid: str, pb, R: dict, run_id: str, today: str, limit: int = 3) -> dict:
    stat = {"opened": 0, "failed": 0, "nodes": []}
    for u, links in frontier(store, eid, R, limit):
        inn = u.split(":", 1)[1]
        res = pb.collect(inn)
        if not res.ok:
            stat["failed"] += 1
            store.log_check(u, "pb_nalog", "не открылось", run_id, today, res.error)
            store.db.commit()
            continue
        store.ingest(res, run_id, today, requested_inn=inn)
        stat["opened"] += 1
        stat["nodes"].append(store.name(u))
    return stat
