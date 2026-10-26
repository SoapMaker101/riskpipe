"""Машинная проверка карточки по cards/schema.json + смысловые правила. Без внешних зависимостей."""
from __future__ import annotations

import json
import re
from pathlib import Path

SCHEMA = json.loads(Path(__file__).with_name("schema.json").read_text(encoding="utf-8"))


def _check(value, sch, path, errs):
    t = sch.get("type")
    if "enum" in sch and value not in sch["enum"]:
        errs.append(f"{path}: {value!r} не из {sch['enum']}")
        return
    types = t if isinstance(t, list) else [t] if t else []
    pymap = {"string": str, "number": (int, float), "array": list, "object": dict, "null": type(None)}
    if types and not any(isinstance(value, pymap[x]) for x in types):
        errs.append(f"{path}: тип {type(value).__name__}, ожидался {types}")
        return
    if isinstance(value, str):
        if "pattern" in sch and not re.search(sch["pattern"], value):
            errs.append(f"{path}: {value!r} не соответствует {sch['pattern']}")
        if len(value) < sch.get("minLength", 0):
            errs.append(f"{path}: пусто")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in sch and value < sch["minimum"] or "maximum" in sch and value > sch["maximum"]:
            errs.append(f"{path}: {value} вне диапазона")
    if isinstance(value, dict):
        for k in sch.get("required", []):
            if k not in value:
                errs.append(f"{path}.{k}: обязательное поле отсутствует")
        for k, sub in sch.get("properties", {}).items():
            if k in value:
                _check(value[k], sub, f"{path}.{k}", errs)
    if isinstance(value, list) and "items" in sch:
        for i, x in enumerate(value):
            _check(x, sch["items"], f"{path}[{i}]", errs)


def validate_card(card: dict) -> list[str]:
    errs: list[str] = []
    _check(card, SCHEMA, card.get("name", "card"), errs)
    flags = card.get("flags") or []
    if any(f.get("level") == "red" and f.get("kind") == "hypothesis" for f in flags):
        errs.append("красный флаг из гипотезы (СМИ)")
    if card.get("color") == "green" and (any(f.get("level") in ("red", "yellow") for f in flags)
                                         or card.get("coverage", 0) < 0.8):
        errs.append("зелёный при флагах или покрытии < 0.8")
    if "?" in card.get("eid", "") and card.get("color") != "grey":
        errs.append("неопознанная компания не серая")
    return errs
