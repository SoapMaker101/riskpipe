"""Перенос риска по графу (раздел «Граф связей» техпроекта).

уровень_связи(X) = max по соседям u: 1[у u красный флаг] × вес(ребро) × step_decay^(шаг − 1)
Шаг 1 — прямые связи X (материнская/владелец-ЮЛ, дочки). Шаг 2 — через ФЛ (руководитель,
учредитель) и владельцев-ЮЛ к их другим компаниям. Только рёбра-факты (официальный источник
или агрегатор), только узлы с ИНН; рёбра-гипотезы и тёзки без ИНН показываются, но не переносят риск.
"""
from __future__ import annotations

from datetime import date

from core.ids import is_resolved
from db.store import Store


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def person_weight(store: Store, pid: str, rel: str, G: dict) -> float:
    """Вес связи через человека; массовый ФЛ (> N компаний) — 0.1."""
    w = G["weights"]
    cnt = 0
    for r in store.latest(pid).get("companies_count", []):
        cnt = max(cnt, _num(r["value"]) or 0)
    cnt = max(cnt, len({n["other"] for n in store.neighbours(pid)}))
    if cnt > G["mass_person_companies"]:
        return w["mass_person"]
    return w["director"] if rel in ("руководитель", "управляющий", "управляющий_ИП", "управляющая_организация") else w["founder_fl"]


def edge_weight(n, G: dict) -> tuple[float, str]:
    """Вес прямой связи ЮЛ–ЮЛ и роль соседа относительно X."""
    w = G["weights"]
    share = n["share_pct"]
    if n["rel"] in ("учредитель", "материнская") and n["d"] == "in":      # сосед владеет X
        if share is None:
            return w["parent_unknown_share"], "владелец"
        return (w["parent_ge50"], "материнская") if share >= 50 else (w["founder_fl"], "совладелец")
    if n["rel"] in ("учредитель", "дочерняя") and n["d"] == "out":          # X владеет соседом
        if share is None:
            return w["child_unknown_share"], "дочка"
        return (w["child_ge50"], "дочка") if share >= 50 else (w["founder_fl"], "зависимая")
    if n["rel"] in ("общий_адрес", "тот_же_адрес"):
        return w["address"], "компания по тому же адресу"
    if n["rel"] == "правопредшественник" and n["d"] == "in":
        return w["successor"], "правопредшественник"
    if n["rel"] in ("руководитель", "управляющая_организация") and n["d"] == "in":
        return w["director"], "управляющая организация"
    return w["founder_fl"], n["rel"]


def link_flags(store: Store, eid: str, R: dict, own_cache: dict, today: date | None = None) -> list:
    from scoring.score import Flag, own_flags

    G = R["graph"]
    no_rel = set(G["no_propagation_rels"])
    carry = set(G["carry_rels"])            # только владение, управление, адрес, правопреемство
    step2_rules = set(G["step2_rules"])

    def reds_of(x):
        if x not in own_cache:
            own_cache[x] = own_flags(store.latest(x), R, today)
        return [f for f in own_cache[x] if f.level == "red"]

    # шаг 1: сосед -> (score, текст, флаг); шаг 2: связующее звено -> (score, префикс, [(сестра, флаг)])
    direct: dict[str, tuple] = {}
    paths: dict[str, list] = {}

    def is_manager(pid):
        """Арбитражный управляющий хотя бы по одному источнику — через него риск не идёт."""
        return any(x["rel"] in no_rel for x in store.neighbours(pid))

    def sisters(u, skip_rel=()):
        out = []
        for n2 in store.neighbours(u):
            v = n2["other"]
            if (v == eid or not v.startswith("ul:") or n2["rel"] in no_rel or n2["rel"] not in carry
                    or n2["kind"] == "hypothesis"):
                continue
            r2 = [f for f in reds_of(v) if f.rule in step2_rules]
            if r2 and v not in [x[0] for x in out]:
                out.append((v, r2[0]))
        return out

    hypo_notes = []
    for n in store.neighbours(eid):
        u = n["other"]
        if n["rel"] in no_rel or n["rel"] not in carry or not u.startswith(("ul", "fl")):
            continue                        # дела, контрагенты, иски: показываются, но риск не переносят
        if n["kind"] == "hypothesis" or not is_resolved(u):
            hypo_notes.append((f"{n['rel']} {store.name(u)} — связь без ИНН/из СМИ, в переносе не участвует",
                               n["src_url"], n["src_class"]))
            continue
        uname = store.name(u)
        if u.startswith("ul:"):
            w, role = edge_weight(n, G)
            reds = reds_of(u)
            if n["rel"] in ("тот_же_адрес", "общий_адрес", "правопредшественник"):
                # не владение: переносится только идущая процедура, а не «прекращена» (присоединение — норма)
                reds = [f for f in reds if f.rule in step2_rules]
            if reds and (u not in direct or w > direct[u][0]):
                direct[u] = (w, f"{role.capitalize()} {uname}: {reds[0].text}", reds[0])
            if n["d"] == "in" and n["rel"] in ("учредитель", "материнская"):      # шаг 2 через владельца-ЮЛ
                sv = sisters(u)
                if sv and (u not in paths or w * G["step_decay"] > paths[u][0]):
                    paths[u] = [w * G["step_decay"], f"Через владельца {uname} связана с", sv]
        else:                                                    # ФЛ: шаг 2 к его другим компаниям
            if is_manager(u):
                continue
            w = person_weight(store, u, n["rel"], G) * G["step_decay"]
            role = {"руководитель": "руководителя", "управляющий": "управляющего",
                    "управляющий_ИП": "управляющего", "учредитель": "учредителя"}.get(n["rel"], n["rel"])
            sv = sisters(u)
            if sv and (u not in paths or w > paths[u][0]):
                paths[u] = [w, f"Через {role} {uname} связана с", sv]

    def level(score):
        for lvl, key in (("red", "red_at"), ("yellow", "yellow_at"), ("info", "info_at")):
            if score >= G[key]:
                return lvl
        return None

    out = []
    for u, (score, text, src) in direct.items():
        if level(score):
            out.append(Flag("P1_link", level(score), f"{text} (вес связи {score:.2f})", src.kind, src.url,
                            src.observed, src.src_class, via=u))
    for u, (score, prefix, sv) in paths.items():
        sv = [x for x in sv if x[0] not in direct]
        if not sv or not level(score):
            continue
        v, src = sv[0]
        names = ", ".join(store.name(x[0]) for x in sv[:3]) + (f" и ещё {len(sv) - 3}" if len(sv) > 3 else "")
        what = src.text if len(sv) == 1 else f"{len(sv)} компаний с красным флагом ({src.text.lower()} и т.п.)"
        out.append(Flag("P2_sister", level(score), f"{prefix} {names}: {what} (вес связи {score:.2f})",
                        src.kind, src.url, src.observed, src.src_class, via=v))
    for note, url, cls in hypo_notes[:3]:
        out.append(Flag("I_link_hypo", "info", note, "hypothesis", url, today.isoformat() if today else None, cls))
    return out
