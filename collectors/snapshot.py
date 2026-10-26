"""Необязательный снимок: ранее собранные карточки агрегатора (data/raw/rp.json) и LLM-сборщики (A–D.json).

Если файлов нет, снимок пустой и конвейер работает только на коннекторах. Формат rp.json:
[{"q": запрос, "inn", "n": имя, "r": регион, "u": url, "s": [[prop, value, year?]], "l": [[rel, имя, ИНН, тип, роль]]}].
Привязка названий без ИНН — необязательный data/raw/name2inn.json {"название": "ИНН"}.

Класс источника — aggregator и media, поэтому выводы — «факт (агрегатор)» и «гипотеза».
Дата наблюдения — дата изменения файла снимка (а не день прогона): так у фактов честный срок годности.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from collectors.base import CollectResult, Link, Statement
from core.ids import inn_valid
from core.source_class import classify

SKIP_LINKS = ("Среднесписочная численность", "Держатель реестра акционеров")


def load_rp(raw: Path) -> list[CollectResult]:
    out = []
    if not (raw / "rp.json").exists():
        return out
    observed = date.fromtimestamp((raw / "rp.json").stat().st_mtime).isoformat()
    for rec in json.loads((raw / "rp.json").read_text(encoding="utf-8")):
        url = rec["u"]
        sts = [Statement(x[0], x[1], url, "aggregator", year=x[2] if len(x) > 2 else None) for x in rec["s"]]
        links = [Link(rel, "in", nm, inn, typ, url, "aggregator")
                 for rel, nm, inn, typ, role in rec["l"] if inn and nm not in SKIP_LINKS]
        q = rec.get("q") or ""
        req = q if re.fullmatch(r"\d{10}|\d{12}", q) else None
        out.append(CollectResult(channel="snapshot", ok=True, inn=req or rec["inn"], page_inn=rec["inn"],
                                 name=rec["n"], region=rec.get("r"), entity_type="ЮЛ",
                                 statements=sts, links=links, extra={"query": q, "observed": observed}))
    return out


def load_llm(raw: Path) -> list[CollectResult]:
    """Из облачных сборщиков берём только СМИ и санкции и только с валидным ИНН."""
    by_inn: dict[str, CollectResult] = {}
    m = raw / "name2inn.json"
    name2inn = json.loads(m.read_text(encoding="utf-8")) if m.exists() else {}
    for f in "ABCD":
        p = raw / f"{f}.json"
        if not p.exists():
            continue
        for r in json.loads(p.read_text(encoding="utf-8")):
            inn = r.get("inn") or name2inn.get(r["query"].split(" (")[0])
            if not inn_valid(inn):
                continue
            res = by_inn.setdefault(inn, CollectResult(
                channel="snapshot_media", ok=True, inn=inn, page_inn=inn, name=r.get("name"), entity_type="ЮЛ",
                extra={"observed": date.fromtimestamp(p.stat().st_mtime).isoformat()}))
            for s in r.get("statements", []):
                prop = s["prop"]
                if prop == "sanctions" and re.search(r"не найден|нет|inferred|автомат", s["value"], re.I):
                    prop = "sanctions_inferred"
                if prop in ("media_negative", "sanctions", "sanctions_inferred") and s.get("src_url"):
                    cls = s.get("src_class") or classify(s["src_url"])
                    if prop == "media_negative":
                        cls = "media"
                    res.statements.append(Statement(prop, s["value"], s["src_url"], cls, year=s.get("year"),
                                                    quote=s.get("quote"), published=s.get("published")))
    return [r for r in by_inn.values() if r.statements]
