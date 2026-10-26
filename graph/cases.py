"""Суды по методике mOS «не количество, а состав»: каждое дело — узел графа.

Узел `дело:<номер>` (тип, суд, дата, сумма, исход); рёбра `истец` / `ответчик` от сторон к делу;
ребро `контрагент` от истца к компании — это вертикаль вверх и вниз, которой нет в выписке.
Контрагент риск не переносит (в propagate это не связь владения/управления).
"""
from __future__ import annotations

from datetime import date, timedelta

from core.ids import entity_id, entity_type
from db.store import Store

CLS = "aggregator"


def ingest_cases(store: Store, eid: str, cases: list[dict], run_id: str, today: str) -> int:
    n = 0
    for c in cases:
        cid = f"дело:{c['number']}"
        store.upsert_entity(cid, "дело", f"{c['number']}, {c.get('court') or ''}, {c.get('type') or ''}".strip(", "),
                            None, today)
        for prop, val in (("case_type", c.get("type")), ("case_date", c.get("date")), ("case_sum", c.get("sum")),
                          ("case_outcome", c.get("outcome")), ("case_active", "да" if c.get("active") else "нет"),
                          ("case_court", c.get("court"))):
            if val not in (None, ""):
                store.add_statement(cid, {"prop": prop, "value": val, "src_url": c["url"], "src_class": CLS},
                                    run_id, today)
        for role, side in (("истец", "plaintiffs"), ("ответчик", "defendants")):
            for p in c.get(side) or []:
                pid = eid if p.get("inn") == eid.split(":", 1)[-1] else \
                    entity_id(p.get("inn"), p.get("name") or "?", entity_type(p.get("inn")))
                if pid != eid:
                    store.upsert_entity(pid, entity_type(p.get("inn")), p.get("name"), None, today)
                store.add_edge(pid, cid, role, None, c["url"], CLS, run_id, today)
        ours_def = any(p.get("inn") == eid.split(":", 1)[-1] for p in c.get("defendants") or [])
        if ours_def:
            for p in c.get("plaintiffs") or []:
                if p.get("inn") and p["inn"] != eid.split(":", 1)[-1]:
                    store.add_edge(entity_id(p["inn"], p.get("name") or "?", entity_type(p["inn"])), eid,
                                   "контрагент", None, c["url"], CLS, run_id, today)
        n += 1
    store.db.commit()
    return n


def court_picture(store: Store, eid: str, today: date, top: int = 3) -> list[str]:
    """3–5 строк «Судебной картины»: кто судится с компанией, на что и чем кончается."""
    since = (today - timedelta(days=365)).isoformat()
    rows = store.db.execute(
        "SELECT e2.src plaintiff, e1.dst dno FROM edge e1 JOIN edge e2 ON e2.dst=e1.dst AND e2.rel='истец' "
        "WHERE e1.src=? AND e1.rel='ответчик' AND e1.dst LIKE 'дело:%'", (eid,)).fetchall()
    if not rows:
        return []
    by_pl: dict[str, list] = {}
    types: dict[str, int] = {}
    lost = 0.0
    for r in rows:
        st = store.latest(r["dno"])
        d = (st.get("case_date") or [{}])[0]
        if not d or (d["value"] or "") < since:
            continue
        s = float((st.get("case_sum") or [{"value": 0}])[0]["value"] or 0)
        t = (st.get("case_type") or [{"value": "?"}])[0]["value"]
        types[t] = types.get(t, 0) + 1
        by_pl.setdefault(r["plaintiff"], []).append(s)
        if "удовлетвор" in str((st.get("case_outcome") or [{"value": ""}])[0]["value"]).lower():
            lost += s
    if not by_pl:
        return ["За 12 месяцев исков к компании нет (по списку дел Checko API)"]
    tops = sorted(by_pl.items(), key=lambda kv: -sum(kv[1]))[:top]
    fmt = lambda x: f"{x/1e6:,.1f}".replace(",", " ").replace(".", ",") + " млн ₽"
    out = [f"Иски к компании за 12 мес: {sum(len(v) for v in by_pl.values())} дел от {len(by_pl)} истцов ("
           + ", ".join(f"{k}: {v}" for k, v in types.items()) + f"); удовлетворено на {fmt(lost)}"]
    out.append("Крупнейшие истцы: " + "; ".join(f"{store.name(p)} — {len(v)} дел, {fmt(sum(v))}" for p, v in tops))
    return out
