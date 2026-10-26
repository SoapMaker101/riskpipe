"""Глубокий анализ фирмы и поиск материалов по ней — методикой mOS, в бюджете техпроекта (~30 тыс. токенов
на красную карточку).

Схема mOS «поиск даёт указатель, текст акта даёт факт». Ступени, каждая в чистом контексте через установленный
Claude Code (`claude -p`, подписка), без инструментов и без «размышления» (MAX_THINKING_TOKENS=0):
0. Материалы собирает КОД (deep/materials.py): акты sudact.ru по номерам ключевых дел из графа + заголовки новостей.
1. Чтение — Haiku, 1 ход: находки с дословными цитатами и записи графа mOS. Код принимает находку, только если
   цитата стоит в тексте материала по этому url (сжатая методика mOS: правила исследователя, «что вынимается
   из акта», «чего не делать», словарь связей).
2. Сводка — Sonnet, без инструментов, 1 ход: что случилось, что из этого следует, что проверить.
   Новых фактов не добавляет — только из находок и карточки.
3. Критик mOS (critic.py) — Sonnet, без инструментов, 1 ход; его сбой — «error», а не «clean».

Код проверяет ответ: находки без url отбрасываются, записи графа идут через collectors.mos_graph
(контрольная сумма ИНН, закрытый словарь mOS, conf «гипотеза» не переносит риск).
Расход считается по ступеням и сверяется с бюджетом rules.yaml (deep.budget_tokens).
Цвет карточки разбор НЕ меняет: это слой аналитика поверх правил.

    python -m deep.analyze --inn 9900010022            # по последней карточке из базы
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collectors.mos_graph import ingest_records  # noqa: E402
from deep.method import critic_system, research_system_compact, writer_system  # noqa: E402
from llm.media_classify import LimitReached, claude_exe  # noqa: E402

CACHE = ROOT / "data" / "cache" / "deep"

EXTRACT_TASK = """КОМПАНИЯ: {name}, ИНН {inn}{region}
УЖЕ ИЗВЕСТНО (не повторяй):
{facts}

МАТЕРИАЛЫ (собраны кодом; читай только их, других источников нет):
{materials}

ЗАДАЧА: вынь из материалов то, что важно для риска поставщика и чего нет в «уже известно»: стороны и роль компании,
предмет спора, суммы, исход и стадию, заявителя банкротства, связанные лица (бенефициары, управляющие, поручители),
события из новостей о самой компании. Тёзок и чужие компании не бери.

ОТВЕТ — только JSON:
{{"findings":[{{"claim":"<одно утверждение по-русски>","url":"<ровно url материала>",
"quote":"<ДОСЛОВНЫЙ фрагмент этого материала, 5–25 слов — без перевода и пересказа>",
"confidence":"первоисточник|СМИ|косвенно"}}],
"graph":[<записи graph.jsonl mOS, только по ИНН из текста: {{"k":"e","from":"ul:ИНН","to":"ul:ИНН","rel":"<словарь>","conf":"факт|гипотеза","src":"[sudact.ru, url, дата]"}}>],
"not_found":["<чего в материалах нет, а для решения нужно>"]}}
Не больше 8 находок. Судебный акт — первоисточник, заголовок новости — СМИ.
"""

WRITE_TASK = """\
КАРТОЧКА RISKPIPE (факты с источниками):
{facts}

НАХОДКИ ПОИСКА (со ссылками):
{findings}

НЕ НАЙДЕНО: {not_found}

ОТВЕТ — только JSON:
{{"summary":"<3–5 строк: что случилось и что из этого следует для поставщика; у фактов [url]>",
"profile_md":"<до 1200 знаков: Что за бизнес · Собственники и связи · Судебная картина · Чего не нашли; со сносками>",
"what_to_check":["<что проверить перед решением, 2–5 пунктов>"]}}
"""


def deep_cfg() -> dict:
    from scoring.score import load_rules
    d = {"budget_tokens": 30000, "run_budget_tokens": 300000, "est_tokens_per_company": 30000, "max_companies": 12,
         "acts": 2, "news": 8,
         "extract_model": "haiku", "write_model": "sonnet", "critic_model": "sonnet"}
    d.update(load_rules().get("deep") or {})
    return d


def _run_claude(system: str, user: str, model: str, tools: str, max_turns: int, usage: dict,
                timeout: int = 600) -> str:
    exe = claude_exe()
    if not exe:
        raise RuntimeError("Claude Code не найден: глубокий разбор недоступен")
    cmd = [exe, "-p", "--model", model, "--output-format", "json", "--max-turns", str(max_turns),
           "--system-prompt", system, "--tools", tools, "--setting-sources", "", "--strict-mcp-config"]
    if tools:
        cmd += ["--allowedTools", tools]
    env = {**os.environ, "MAX_THINKING_TOKENS": "0"}
    r = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, cwd=os.environ.get("TEMP") or None, env=env)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude -p: код {r.returncode}: {(r.stderr or r.stdout or '')[:200]}")
    u = d.get("usage") or {}
    tin = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    usage["tokens_in"] = usage.get("tokens_in", 0) + tin
    usage["tokens_out"] = usage.get("tokens_out", 0) + u.get("output_tokens", 0)
    usage["cost_usd"] = usage.get("cost_usd", 0) + (d.get("total_cost_usd") or 0)
    usage["last"] = {"in": tin, "out": u.get("output_tokens", 0), "turns": d.get("num_turns"),
                     "cost": d.get("total_cost_usd") or 0}
    if d.get("is_error"):
        msg = str(d.get("result") or d.get("terminal_reason"))
        if d.get("api_error_status") == 429 or "limit" in msg.lower():
            raise LimitReached(f"claude -p: {msg}")
        if d.get("subtype") == "error_max_turns":
            return d.get("result") or ""
        raise RuntimeError(f"claude -p: {msg[:200]}")
    return d.get("result") or ""


def _json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


def _stage(kind: str, system: str, user: str, model: str, tools: str, turns: int, usage: dict, stages: dict):
    """Вызов ступени с кэшем на 7 дней (повторный прогон — те же выводы, ноль токенов) и учётом расхода."""
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"{kind}_{hashlib.sha1((model + system + user).encode('utf-8')).hexdigest()[:20]}.json"
    if p.exists() and time.time() - p.stat().st_mtime < 7 * 86400:
        cached = json.loads(p.read_text(encoding="utf-8"))
        stages[kind] = {**cached.get("usage", {}), "cached": True}
        return cached.get("data")
    local: dict = {}
    data = _json(_run_claude(system, user, model, tools, turns, local))
    for k in ("tokens_in", "tokens_out", "cost_usd"):
        usage[k] = usage.get(k, 0) + local.get(k, 0)
    stages[kind] = {**local.get("last", {}), "model": model}
    if data is not None:
        p.write_text(json.dumps({"data": data, "usage": stages[kind]}, ensure_ascii=False), encoding="utf-8")
    return data


def _norm(t: str) -> str:
    t = (t or "").replace("«", '"').replace("»", '"').replace("“", '"').replace("”", '"').replace("ё", "е")
    return re.sub(r"\s+", " ", t).strip().lower()


def verify_quotes(findings: list, mats: list[dict]) -> tuple[list[dict], int]:
    """Правило mOS «реквизит из акта — с цитатой»: цитата должна дословно стоять в материале по этому url."""
    by_url = {m["url"]: _norm(m["text"]) for m in mats}
    ok = [f for f in findings if isinstance(f, dict) and f.get("url") in by_url
          and len(_norm(f.get("quote"))) >= 10 and _norm(f.get("quote")) in by_url[f["url"]]]
    return ok, len(findings) - len(ok)


def facts_of(store, card: dict, limit: int = 2000) -> str:
    """Сжатые факты: красные и жёлтые флаги, суды, группа, выручка — без «для сведения» и служебных строк."""
    from scoring.score import money

    eid = card["eid"]
    lines = [f"{card['name']}, ИНН {card.get('inn')}: {card['color']}"]
    for f in card.get("flags", []):
        if f.get("level") in ("red", "yellow"):
            lines.append(f"- {f['text'][:160]} [{f.get('url', '')}]")
    lines += [f"- суды: {x}" for x in card.get("courts", [])]
    if card.get("group"):
        lines.append("- группа: " + ", ".join(card["group"][:6]))
    by_year = {}
    for r in store.latest(eid).get("revenue", []):
        if r["year"] and money(r["value"]) is not None:
            by_year.setdefault(r["year"], money(r["value"]))
    if by_year:
        lines.append("- выручка: " + "; ".join(f"{y}: {v/1e6:,.0f} млн ₽".replace(",", " ")
                                              for y, v in sorted(by_year.items(), reverse=True)[:3]))
    people = [f"{n['rel']} {store.name(n['other'])}" for n in store.neighbours(eid)
              if n["other"].startswith("fl") and n["d"] == "in"]
    if people:
        lines.append("- люди: " + "; ".join(people[:5]))
    return "\n".join(lines)[:limit]


def analyze(store, card: dict, run_id: str, usage: dict, why: str = "красная карточка",
            sudact=None, news=None) -> dict:
    cfg = deep_cfg()
    if sudact is None or news is None:
        from collectors.news import GoogleNews
        from core.http import FileCache, Http
        from deep.materials import Sudact
        http, cache = Http(), FileCache(ROOT / "data" / "cache")
        sudact = sudact or Sudact(http, cache)
        news = news or GoogleNews(http, cache)
    stages: dict = {}
    before = usage.get("tokens_in", 0) + usage.get("tokens_out", 0)
    facts = facts_of(store, card)
    system, used = research_system_compact()
    reg = store.db.execute("SELECT region FROM entity WHERE id=?", (card["eid"],)).fetchone()
    out = {"mos_parts": used, "date": date.today().isoformat(), "stages": stages}

    # 1. материалы собирает код (акты sudact по номерам дел из графа, заголовки новостей), Haiku только читает
    from deep.materials import collect_materials

    mats, mlog = collect_materials(store, card["eid"], card["name"], reg["region"] if reg else None,
                                   sudact, news, date.today(), cfg["acts"], cfg["news"])
    # 0б. веб-указатели: Haiku только ищет (WebSearch, без чтения), страницы открывает код — витрины исключены
    if cfg.get("web_pages", 0) > 0:
        from deep.web import POINTER_TASK, PageReader, is_showcase

        try:
            ptr = _stage("pointers", "Ты ищешь ссылки-указатели для исследователя mOS. Сниппет — не источник: "
                         "только ссылки и причина, без пересказа. Ответ — только JSON.",
                         POINTER_TASK.format(name=card["name"], inn=card.get("inn"),
                                             region=f", {reg['region']}" if reg and reg["region"] else "",
                                             brief=facts[:600], searches=cfg.get("web_searches", 4),
                                             pages=cfg["web_pages"] * 2),
                         cfg.get("pointer_model", "haiku"), "WebSearch", 3, usage, stages) or {}
        except LimitReached:
            raise
        except Exception as e:                                   # noqa: BLE001 — указатели не обязательны
            ptr = {}
            mlog.append(f"веб-указатели: {type(e).__name__}: {str(e)[:120]}")
        reader, taken = PageReader(sudact.http, sudact.cache), 0
        for p in ptr.get("urls") or []:
            u = str(p.get("url") or "")
            if taken >= cfg["web_pages"] or not u.startswith("http"):
                continue
            if is_showcase(u):
                mlog.append(f"витрина пропущена по правилу mOS: {u}")
                continue
            page = reader.read(u, card["name"], card.get("inn"))
            if page:
                mats.append({"kind": f"страница ({p.get('why') or 'указатель поиска'})", "title": page["title"],
                             "url": page["url"], "text": page["text"]})
                taken += 1
            else:
                mlog.append(f"не открылась: {u}")
    out["materials"] = [{"kind": m["kind"], "title": m["title"], "url": m["url"]} for m in mats]
    out["materials_log"] = mlog
    mtext = "\n\n".join(f"[{i}] {m['kind']}: {m['title']}\nurl: {m['url']}\n{m['text']}"
                         for i, m in enumerate(mats, 1)) or "(материалов не нашлось)"
    raw = (_stage("extract", system, EXTRACT_TASK.format(
        name=card["name"], inn=card.get("inn"), region=f", {reg['region']}" if reg and reg["region"] else "",
        facts=facts, materials=mtext), cfg["extract_model"], "", 1, usage, stages) or {}) if mats else {}
    findings, dropped = verify_quotes(raw.get("findings") or [], mats)
    gstat = ingest_records(store, [g for g in raw.get("graph") or [] if isinstance(g, dict)], f"deep:{run_id}")

    # 2. сводка — Sonnet, без инструментов
    flist = "\n".join(f"- {f['claim']} [{f.get('confidence')}, {f['url']}"
                      + (f", опубл. {f['published']}" if f.get("published") else "") + "]"
                      + (f" «{f['quote']}»" if f.get("quote") else "") for f in findings) or "(ничего не найдено)"
    wr = _stage("write", writer_system(), WRITE_TASK.format(
        facts=facts, findings=flist, not_found="; ".join(raw.get("not_found") or []) or "—"),
        cfg["write_model"], "", 1, usage, stages)
    if not wr:
        out.update(status="error", reason="сводка не вернула JSON — разбора нет", findings=findings, graph=gstat)
        return out
    out.update(status="ok", summary=wr.get("summary", ""), profile_md=wr.get("profile_md", ""),
               findings=findings, findings_dropped=dropped, what_to_check=wr.get("what_to_check") or [],
               not_found=raw.get("not_found") or [], graph=gstat)

    # 3. критик mOS — Sonnet, без инструментов
    out["critic"] = critic(facts, out, cfg, usage, stages)
    spent = usage.get("tokens_in", 0) + usage.get("tokens_out", 0) - before
    out["tokens"] = spent
    out["budget"] = cfg["budget_tokens"]
    out["over_budget"] = spent > cfg["budget_tokens"]
    return out


def critic(facts: str, deep: dict, cfg: dict, usage: dict, stages: dict) -> dict:
    """Критик mOS в чистом контексте. Не блокирует; «не отработал» — это error, а не clean."""
    draft = f"{deep.get('summary', '')}\n\n{deep.get('profile_md', '')}\n\nЧто проверить:\n" + \
            "\n".join(f"- {x}" for x in deep.get("what_to_check", []))
    user = (f"КАРТОЧКА RISKPIPE (факты с источниками, для сверки):\n{facts}\n\n"
            f"ЧЕРНОВИК РАЗБОРА, который уйдёт менеджеру:\n{draft[:4000]}\n\n"
            f"Сегодня {date.today().isoformat()}. Разбери черновик по своим правилам.")
    try:
        data = _stage("critic", critic_system(), user, cfg["critic_model"], "", 1, usage, stages)
    except LimitReached:
        raise
    except Exception as e:                                       # noqa: BLE001
        return {"verdict": "error", "reason": f"{type(e).__name__}: {str(e)[:150]}"}
    if not data or data.get("verdict") not in ("pushback", "clean"):
        return {"verdict": "error", "reason": "критик не вернул вердикт — контр-взгляда нет"}
    return {k: data.get(k, [] if k in ("weak_claims", "missing_source") else "")
            for k in ("verdict", "counterview", "weak_claims", "missing_source")}


def render(card: dict, d: dict) -> str:
    out = [f"# Глубокий разбор: {card['name']}, ИНН {card.get('inn')}", "",
           f"Методика mOS: {', '.join(d.get('mos_parts', []))}. Дата: {d.get('date')}."]
    st = d.get("stages") or {}
    if st:
        out.append("Расход: " + "; ".join(
            f"{k} ({v.get('model', '')}{', кэш' if v.get('cached') else ''}): {v.get('in', 0) + v.get('out', 0):,} ток."
            .replace(",", " ") for k, v in st.items())
            + (f" — итого {d.get('tokens', 0):,} при бюджете {d.get('budget', 0):,}".replace(",", " ")
               + (" — ПРЕВЫШЕН" if d.get("over_budget") else "") if "tokens" in d else ""))
    out.append("")
    if d.get("status") != "ok":
        return "\n".join(out + [f"Разбор не получился: {d.get('reason')}"])
    out += ["## Что случилось", d.get("summary", ""), "", d.get("profile_md", ""), "",
            "## Что проверить перед решением"] + [f"- {x}" for x in d.get("what_to_check", [])]
    out += ["", "## Чего не нашли"] + [f"- {x}" for x in d.get("not_found", [])]
    out += ["", "## Материалы (собраны кодом)"] + [f"- {m['kind']}: {m['title']} — {m['url']}" for m in d.get("materials", [])]         + [f"- не открылось/не нашлось: {x}" for x in d.get("materials_log", [])]
    out += ["", f"## Находки с дословными цитатами (отброшено без цитаты в тексте: {d.get('findings_dropped', 0)})"] + [f"- {f['claim']} — [{f.get('confidence')}] {f['url']}"
                                            + (f" (опубл. {f['published']})" if f.get("published") else "")
                                            + (f" «{f['quote']}»" if f.get("quote") else "")
                                            for f in d.get("findings", [])]
    g = d.get("graph") or {}
    out += ["", f"В граф влито: узлов {g.get('nodes', 0)}, рёбер {g.get('edges', 0)}; отброшено {len(g.get('rejected', []))}"
            + "".join(f"\n- {r}" for r in g.get("rejected", [])[:10])]
    c = d.get("critic") or {}
    out += ["", f"## Критик mOS: {c.get('verdict')}"]
    if c.get("verdict") == "error":
        out.append(f"Критик не отработал ({c.get('reason')}) — контр-взгляда нет; это не «замечаний нет».")
    else:
        out += [c.get("counterview") or "Существенных замечаний нет."]
        out += [f"- слабое: {x}" for x in c.get("weak_claims", [])] + [f"- без источника: {x}" for x in c.get("missing_source", [])]
    return "\n".join(out)


def main(argv=None):
    from db.store import Store

    ap = argparse.ArgumentParser()
    ap.add_argument("--inn", required=True)
    ap.add_argument("--db", default=str(ROOT / "data" / "riskpipe.db"))
    ap.add_argument("--why", default="разбор по запросу")
    a = ap.parse_args(argv)
    st = Store(a.db)
    row = st.db.execute("SELECT run_id, card_json FROM card WHERE inn=? ORDER BY rowid DESC LIMIT 1", (a.inn,)).fetchone()
    if not row:
        sys.exit(f"Карточки для ИНН {a.inn} в базе нет — сначала прогон pipeline.run")
    card, usage = json.loads(row["card_json"]), {}
    from collectors.pb_nalog import PbNalog
    from core.http import FileCache, Http
    from graph.frontier import wave
    from scoring.score import load_rules

    R = load_rules()                                   # сначала граф: волна фронтира по соседям-ЮЛ
    fw = wave(st, card["eid"], PbNalog(Http(), FileCache(ROOT / "data" / "cache")), R, row["run_id"],
              date.today().isoformat(), (R.get("frontier") or {}).get("per_company", 3))
    if fw["opened"]:
        print(f"фронтир: раскрыто {fw['opened']} — {', '.join(fw['nodes'])}")
    d = analyze(st, card, row["run_id"], usage, a.why)
    path = ROOT / "out" / row["run_id"] / "deep" / f"{a.inn}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(card, d), encoding="utf-8")
    print(f"{path}\nтокенов: {d.get('tokens', 0)} (бюджет {d.get('budget')}), ${usage.get('cost_usd', 0):.3f}")


if __name__ == "__main__":
    main()
