"""Ключи сущностей: ИНН/ОГРН с контрольной суммой, нормализация имён и номеров дел.

Перенесено из прототипа (store.py) и mOS graph.py: inn_valid, entity_id,
формат номера арбитражного дела А00-000/ГГГГ.
"""
from __future__ import annotations

import re


def inn_valid(inn: str | None) -> bool:
    if not inn or not re.fullmatch(r"\d{10}|\d{12}", str(inn)):
        return False
    d = [int(c) for c in str(inn)]
    if len(d) == 10:
        k = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return sum(a * b for a, b in zip(k, d)) % 11 % 10 == d[9]
    k1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    k2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    return (sum(a * b for a, b in zip(k1, d)) % 11 % 10 == d[10]
            and sum(a * b for a, b in zip(k2, d)) % 11 % 10 == d[11])


def ogrn_valid(ogrn: str | None) -> bool:
    if not ogrn or not re.fullmatch(r"\d{13}|\d{15}", str(ogrn)):
        return False
    s = str(ogrn)
    if len(s) == 13:
        return int(s[:12]) % 11 % 10 == int(s[12])
    return int(s[:14]) % 13 % 10 == int(s[14])


LEGAL_FORMS = r"(ооо|ао|пао|зао|оао|нао|муп|гуп|фгуп|мкп|ип|ано|ну|фгбу|гбу|мбу|мку|мау|гку|ук|по|тд|нпо|нпп|пк|ск)"


def norm_name(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'“”„.,()\-]", " ", s)
    s = re.sub(rf"\b{LEGAL_FORMS}\b", " ", s)
    return re.sub(r"\s+", "_", s.strip())


def entity_type(inn: str | None, hint: str | None = None) -> str:
    if hint in ("ФЛ", "ИП", "ЮЛ"):
        return hint
    if inn and len(str(inn)) == 12:
        return "ФЛ"
    return "ЮЛ"


def entity_id(inn: str | None, name: str, typ: str) -> str:
    """ul:<ИНН> | fl:<ИНН> | ul?:<имя> | fl?:<фио>. Узел без ИНН в переносе риска не участвует."""
    pref = "fl" if typ in ("ФЛ", "ИП") else "ul"
    if inn and inn_valid(str(inn)):
        return f"{pref}:{inn}"
    return f"{pref}?:{norm_name(name)}"


def inn_of(eid: str) -> str | None:
    return eid.split(":", 1)[1] if eid.startswith(("ul:", "fl:")) else None


def is_resolved(eid: str) -> bool:
    return eid.startswith(("ul:", "fl:"))


def norm_case_number(s: str) -> str | None:
    """'а60 12345 2025' / 'A60-12345/25' -> 'А60-12345/2025'."""
    m = re.search(r"[АаAa]\s*(\d{2})\s*[-–\s]\s*(\d{1,7})\s*/\s*(\d{2,4})", s or "")
    if not m:
        return None
    y = m.group(3)
    if len(y) == 2:
        y = "20" + y
    return f"А{m.group(1)}-{int(m.group(2))}/{y}"
