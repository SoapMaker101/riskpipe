"""Markdown-отчёт по карточкам прогона и сравнение с прошлым тестом."""
from __future__ import annotations

from scoring.explain import EMOJI

ORDER = {"red": 0, "yellow": 1, "green": 2, "grey": 3}
SECTION = {"green": "В работу", "yellow": "В работу с проверкой", "red": "Не брать без риск-менеджера",
           "grey": "Дособрать"}
# сортировка для менеджера: «в работу → с проверкой → не брать → дособрать»
MANAGER_ORDER = {"green": 0, "yellow": 1, "red": 2, "grey": 3}


def report(cards: list[dict], run: dict, log: list[str]) -> str:
    cnt = {k: sum(1 for c in cards if c["color"] == k) for k in ORDER}
    out = [f"# Прогон {run['run_id']}", "",
           f"Компаний на входе: {len(cards)}. " + " · ".join(f"{EMOJI[k]} {v}" for k, v in cnt.items()), ""]
    if run.get("notes"):
        out += [run["notes"], ""]
    for col in sorted(ORDER, key=MANAGER_ORDER.get):
        cs = [c for c in cards if c["color"] == col]
        if not cs:
            continue
        out += [f"## {EMOJI[col]} {SECTION[col]} ({len(cs)})", ""]
        for c in cs:
            out.append(c["explain"].replace("\n", "  \n") + f"  \nИсточник в списке: {c['origin']}")
            out.append("")
    if log:
        out += ["## Журнал", ""] + [f"- {x}" for x in log] + [""]
    return "\n".join(out)


def compare(cards: list[dict], old: list[dict]) -> str:
    by_name = {}
    for o in old:
        by_name[o["name"]] = o
        if o.get("inn"):
            by_name[o["inn"]] = o
    out = ["# Сравнение с предыдущими карточками (out/cards.json)", "",
           "| Компания | ИНН | Было | Стало | Официальных флагов | Что изменилось |", "|---|---|---|---|---|---|"]
    changed = 0
    for c in sorted(cards, key=lambda c: (ORDER[c["color"]], c["name"])):
        o = by_name.get(c.get("inn")) or by_name.get(c.get("query")) or by_name.get(c["name"])
        was = o["color"] if o else "—"
        n_off = sum(1 for f in c["flags"] if f.get("kind") == "fact" and f["level"] != "info")
        if was != c["color"]:
            changed += 1
        new_rules = sorted({f["rule"] for f in c["flags"] if f["level"] != "info"}
                           - {f["rule"] for f in (o or {}).get("flags", []) if f["level"] != "info"})
        gone = sorted({f["rule"] for f in (o or {}).get("flags", []) if f["level"] != "info"}
                      - {f["rule"] for f in c["flags"] if f["level"] != "info"})
        why = "; ".join(x for x in (("+" + ", +".join(new_rules)) if new_rules else "",
                                    ("−" + ", −".join(gone)) if gone else "") if x)
        out.append(f"| {c['name']} | {c.get('inn') or '—'} | {EMOJI.get(was, was)} | {EMOJI[c['color']]} | "
                   f"{n_off} | {why} |")
    out += ["", f"Цвет изменился у {changed} из {len(cards)}."]
    return "\n".join(out)
