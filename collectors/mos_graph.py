"""Записи графа в формате mOS (graph.jsonl) -> база riskpipe.

Один формат на два случая: импорт накопленных графов кейсов mOS и вливание результата глубокого
разбора (deep/analyze.py), который возвращает записи графа, как помощники mOS.

Правила mOS переносятся кодом: у записи источник со ссылкой (иначе отбрасывается), ИНН проходит
контрольную сумму, связь — из закрытого словаря mOS, `conf: гипотеза` -> вид «гипотеза»
(такие рёбра показываются, но риск не переносят). `ip:<ИНН>` -> `fl:<ИНН>`.

    python -m collectors.mos_graph "<путь к graph.jsonl или папке с кейсами>" [--db data/riskpipe.db]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.ids import inn_valid  # noqa: E402
from core.source_class import classify  # noqa: E402

URL = re.compile(r"https?://[^\s,\]\)]+")
FALLBACK_RELS = {"учредитель", "руководитель", "дочернее", "участник_группы", "правопреемник", "тот_же_адрес",
                 "тот_же_телефон", "тот_же_домен", "истец", "ответчик", "третье_лицо", "заявитель_банкротства",
                 "контрагент", "сторона_договора", "предмет_спора", "залогодержатель", "лизингодатель",
                 "поручитель", "кредитор", "владеет", "арендует", "аффилирован", "упоминание"}
# связи mOS -> связи riskpipe, по которым считается перенос риска (остальные — как есть, без переноса)
REL_MAP = {"дочернее": "учредитель", "правопреемник": "правопредшественник"}


def mos_root() -> Path | None:
    """Папка с исходниками mOS (mOS_vault/, mos/) — необязательно: MOS_ROOT в окружении или в .env."""
    from core.env import getenv
    v = getenv("MOS_ROOT")
    return Path(v) if v and Path(v).exists() else None


def mos_rels() -> set[str]:
    """Закрытый словарь связей — прямо из graph.py mOS (без импорта модуля), иначе копия."""
    root = mos_root()
    if root is None:
        return FALLBACK_RELS
    p = root / "mos" / "vault" / ".claude" / "skills" / "graph.py"
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "RELS":
                return set(ast.literal_eval(node.value).keys())
    except (OSError, SyntaxError, ValueError):
        pass
    return FALLBACK_RELS


def node_id(raw: str) -> str | None:
    """id mOS -> id riskpipe. None — запись отбрасывается (битый ИНН)."""
    raw = (raw or "").strip()
    if raw.startswith("ip:"):
        raw = "fl:" + raw[3:]
    m = re.match(r"^(ul|fl):(\d+)$", raw)
    if m:
        return raw if inn_valid(m.group(2)) else None
    if raw.startswith(("ul:?", "fl:?")):
        return raw[:2] + "?:" + raw[4:]
    return raw if raw else None


def ingest_records(store, records: list[dict], run_id: str, today: str | None = None) -> dict:
    """Возвращает статистику: принято / отброшено с причинами."""
    today = today or date.today().isoformat()
    rels = mos_rels()
    stat = {"nodes": 0, "edges": 0, "checks": 0, "rejected": []}
    for rec in records:
        url = (URL.search(str(rec.get("src") or rec.get("url") or "")) or [None])[0]
        k = rec.get("k")
        if k == "n":
            nid = node_id(rec.get("id"))
            if not nid:
                stat["rejected"].append(f"узел {rec.get('id')}: ИНН не проходит контрольную сумму")
                continue
            typ = rec.get("t") or ("ФЛ" if nid.startswith("fl") else "ЮЛ")
            store.upsert_entity(nid, typ, rec.get("name"), None, today)
            if url:
                for prop, val in (rec.get("attrs") or {}).items():
                    if isinstance(val, (str, int, float)) and str(val).strip():
                        store.add_statement(nid, {"prop": f"mos_{prop}", "value": val, "src_url": url,
                                                  "src_class": classify(url, "media")}, run_id, today)
            stat["nodes"] += 1
        elif k == "e":
            a, b, rel = node_id(rec.get("from")), node_id(rec.get("to")), rec.get("rel")
            if not a or not b:
                stat["rejected"].append(f"ребро {rec.get('from')}→{rec.get('to')}: битый ИНН")
                continue
            if rel not in rels:
                stat["rejected"].append(f"ребро {rel}: нет в словаре mOS")
                continue
            if not url:
                stat["rejected"].append(f"ребро {a}→{b} {rel}: нет ссылки на источник")
                continue
            for x in (a, b):
                store.upsert_entity(x, "ФЛ" if x.startswith("fl") else ("ЮЛ" if x.startswith("ul") else x.split(":")[0]),
                                    None, None, today)
            cls = classify(url, "media")
            hypo = rec.get("conf") == "гипотеза" or rel in ("упоминание", "аффилирован")
            store.add_edge(a, b, REL_MAP.get(rel, rel), (rec.get("attrs") or {}).get("доля"), url, cls, run_id, today,
                           kind="hypothesis" if hypo else None)
            stat["edges"] += 1
        elif k == "c":
            nid = node_id(rec.get("node"))
            if nid:
                res = {"нашлось": "нашлось", "пусто": "пусто", "не_открылось": "не открылось"}.get(rec.get("res"), "пусто")
                store.log_check(nid, f"mos_{rec.get('ch')}", res, run_id, today, rec.get("note"))
                stat["checks"] += 1
    store.db.commit()
    return stat


def read_jsonl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def main(argv=None):
    from db.store import Store

    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=str(mos_root() / "mOS_vault" / "10_Cases") if mos_root() else None,
                    help="graph.jsonl или папка с кейсами (по умолчанию MOS_ROOT/mOS_vault/10_Cases)")
    ap.add_argument("--db", default=str(ROOT / "data" / "riskpipe.db"))
    a = ap.parse_args(argv)
    if not a.path:
        sys.exit("Укажите путь к graph.jsonl или к папке с кейсами (или задайте MOS_ROOT)")
    p = Path(a.path)
    files = [p] if p.is_file() else [f for f in p.rglob("graph.jsonl") if "_template" not in f.parts]
    st = Store(a.db)
    for f in files:
        s = ingest_records(st, read_jsonl(f), f"mos:{f.parent.name}")
        print(f"{f.parent.name}: узлов {s['nodes']}, рёбер {s['edges']}, каналов {s['checks']}, "
              f"отброшено {len(s['rejected'])}" + "".join(f"\n  - {r}" for r in s["rejected"][:5]))


if __name__ == "__main__":
    main()
