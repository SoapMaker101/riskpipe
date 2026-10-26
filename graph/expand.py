"""Раскрытие связей: шаг 1 приходит из карточки ЕГРЮЛ; шаг 2 — другие компании тех же ФЛ по ИНН.

Раскрытые ФЛ лежат в базе 7 дней (CHANNEL_TTL["pb_person"]): второй заказчик с тем же владельцем
берёт узел из базы без нового запроса. Массовых ФЛ (> N компаний) и арбитражных управляющих не раскрываем.
"""
from __future__ import annotations

from db.store import Store

EXPAND_RELS = ("руководитель", "учредитель", "управляющий", "управляющий_ИП")


def persons_to_expand(store: Store, eid: str, R: dict, limit: int = 6) -> list[str]:
    mass = R["graph"]["mass_person_companies"]
    out = []
    for n in store.neighbours(eid):
        pid = n["other"]
        if n["d"] != "in" or not pid.startswith("fl:") or n["rel"] not in EXPAND_RELS:
            continue
        cnt = max([float(r["value"]) for r in store.latest(pid).get("companies_count", [])
                   if str(r["value"]).replace(".", "").isdigit()] or [0])
        if cnt > mass:
            continue
        if any(x["rel"] in R["graph"]["no_propagation_rels"] for x in store.neighbours(pid)):
            continue                     # арбитражный управляющий: его банкротства — не группа заказчика
        if pid not in out:
            out.append(pid)
    return out[:limit]


def expand(store: Store, eid: str, person_collector, R: dict, run_id: str, today: str, log=print) -> dict:
    """Возвращает статистику: сколько ФЛ раскрыто, сколько взято из базы, сколько не открылось."""
    stat = {"queried": 0, "reused": 0, "failed": 0}
    for pid in persons_to_expand(store, eid, R):
        if store.fresh(pid, "pb_person", today):
            stat["reused"] += 1
            continue
        res = person_collector(pid.split(":", 1)[1])
        if not res.ok:
            stat["failed"] += 1
            store.log_check(pid, "pb_person", "не открылось", run_id, today, res.error)
            store.db.commit()
            continue
        store.ingest(res, run_id, today, requested_inn=pid.split(":", 1)[1])
        stat["queried"] += 1
    return stat


def group_status(store: Store, eid: str, R: dict) -> tuple[bool, list[str]]:
    """Критерий mOS «состав группы закрыт»: по компании пройдены учредители, руководитель, адрес, суды
    (результат записан, включая «пусто»), а каждый ФЛ на одном шаге раскрыт или помечен тупиком с причиной."""
    checks = store.checks(eid)
    passed = {k for k, v in checks.items() if v in ("нашлось", "пусто")}
    missing = []
    if "pb_nalog" not in passed:
        missing += ["учредители", "руководитель"]
    if "адрес" not in passed:
        missing.append("адрес")
    if not passed & {"checko_api", "companium", "checko", "snapshot"}:
        missing.append("суды")
    mass = R["graph"]["mass_person_companies"]
    for n in store.neighbours(eid):
        pid = n["other"]
        if n["d"] != "in" or n["rel"] not in EXPAND_RELS:
            continue
        if not pid.startswith("fl:"):
            if pid.startswith("fl?"):
                missing.append(f"ФЛ без ИНН: {store.name(pid)}")
            continue
        cnt = max([float(r["value"]) for r in store.latest(pid).get("companies_count", [])
                   if str(r["value"]).replace(".", "").isdigit()] or [0])
        dead_end = cnt > mass or any(x["rel"] in R["graph"]["no_propagation_rels"] for x in store.neighbours(pid))
        if not dead_end and store.checks(pid).get("pb_person") != "нашлось":
            missing.append(f"компании ФЛ {store.name(pid)}")
    # mOS: «узлов со статусом „новый“ на расстоянии одного шага не осталось — либо разобраны, либо тупик»
    from graph.frontier import unresolved
    missing += [f"не разобран {name}" for name in unresolved(store, eid, R)]
    return not missing, missing
