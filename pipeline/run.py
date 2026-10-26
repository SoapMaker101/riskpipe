"""Утренний прогон: очередь ИНН с чекпойнтом на каждую компанию -> сбор -> граф -> скоринг -> карточки.

Статусы: new -> collected | partial (источник не ответил / бюджет исчерпан) -> scored.
Перезапуск того же run_id продолжает с чекпойнта; повторные факты не дублируются (Store.add_statement).

    python -m pipeline.run --input input/example.csv --run-id run-2026-10-26
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cards.export_md import ORDER, compare, report  # noqa: E402
from cards.validate import validate_card  # noqa: E402
from core.ids import entity_id, inn_valid  # noqa: E402
from db.store import Store  # noqa: E402
from graph.cases import court_picture, ingest_cases  # noqa: E402
from graph.expand import expand, group_status  # noqa: E402
from scoring.explain import explain  # noqa: E402
from scoring.score import Flag, load_rules, score_company  # noqa: E402


class Pipeline:
    def __init__(self, store: Store, run_id: str, today: str, R: dict | None = None,
                 pb=None, bfo=None, snapshot: list | None = None, unavailable: dict | None = None,
                 budget_sec: float = 900, log=print, checko=None, news=None, classify=None):
        self.st = store
        self.run_id = run_id
        self.today = today
        self.R = R or load_rules()
        self.pb = pb                    # PbNalog | фейк с collect/person_companies/resolve
        self.bfo = bfo
        self.checko = checko            # суды с ролями, ФССП, ЕФРСБ без ключа (агрегатор)
        self.news = news                # заголовки СМИ (RSS)
        self.classify = classify        # llm.media_classify.classify или None
        self.usage: dict = {}
        self.snapshot = snapshot or []
        self.unavailable = unavailable or {}
        self.budget_sec = budget_sec
        self.log = log
        self.journal: list[str] = []
        self.snap_by_query = {}
        for r in self.snapshot:
            if r.extra.get("query"):
                self.snap_by_query[r.extra["query"]] = r

    # ---------- вход ----------
    def load(self, rows: list[dict]):
        self.st.run_start(self.run_id, len(rows))
        self.st.queue_load(self.run_id, rows)
        for r in self.snapshot:                          # снимок — ещё один источник, идемпотентно
            eid = self.st.ingest(r, self.run_id, r.extra.get("observed") or self.today,
                                 requested_inn=r.inn if r.channel == "snapshot" else None)
            if not eid and r.channel == "snapshot":
                self.journal.append(f"ОТБРОШЕНО (снимок): запрос «{r.extra.get('query')}» — на странице ИНН "
                                    f"{r.page_inn}, не сходится с запросом/контрольной суммой")

    def resolve(self, row) -> tuple[str | None, str]:
        if row["inn"] and inn_valid(row["inn"]):
            return row["inn"], "ИНН из входа"
        snap = self.snap_by_query.get(row["query"])
        if snap and inn_valid(snap.page_inn) and not (snap.inn and snap.inn != snap.page_inn):
            return snap.page_inn, "по названию в снимке теста"
        if self.pb is None:
            return None, "во входе нет ИНН"
        try:
            inn, cands = self.pb.resolve(row["name"], self.R["identification"]["industry_okved"],
                                         row["region"] if "region" in row.keys() else None)
        except Exception as e:                           # noqa: BLE001 — источник не ответил
            return None, f"поиск по названию не открылся ({e})"
        if inn:
            c = cands[0] if cands else {}
            if c.get("hint"):
                return inn, (f"по подсказке: из нескольких тёзок выбрана {c.get('name')} ({c.get('region') or '?'}, "
                             f"{c['hint']}) — гипотеза, сверить ИНН с извещением")
            return inn, (f"по названию в ЕГРЮЛ — единственное действующее совпадение ({c.get('region') or '?'}, "
                         f"ОКВЭД {c.get('okved') or '?'}); сверить с извещением")
        if cands:
            return None, "в ЕГРЮЛ несколько кандидатов: " + "; ".join(
                f"{c['name']} ИНН {c['inn']} ({c.get('region') or '?'})" for c in cands[:4])
        return None, "в ЕГРЮЛ по названию не найдена"

    # ---------- одна компания ----------
    def process(self, row) -> str:
        t0 = time.monotonic()
        inn, how = self.resolve(row)
        if not inn:
            self.st.queue_set(self.run_id, row["pos"], "collected", eid=entity_id(None, row["name"], "ЮЛ"), note=how)
            return "collected"
        eid = f"ul:{inn}"
        self.st.upsert_entity(eid, "ЮЛ", row["name"], None, self.today)
        self.st.db.commit()              # не держать блокировку базы во время сетевых запросов
        partial = []
        bo_id = None
        if self.pb is not None:
            if self.st.fresh(eid, "pb_nalog", self.today):
                r = self.st.latest(eid).get("bo_id")
                bo_id = r[0]["value"] if r else None
            else:
                res = self.pb.collect(inn)
                got = self.st.ingest(res, self.run_id, self.today, requested_inn=inn)
                if not res.ok:
                    partial.append(f"ФНС: {res.error}")
                elif not got:
                    partial.append("ФНС: чужая карточка")
                bo_id = res.extra.get("bo_id")
                if res.ok and "address" in res.extra:          # канал «адрес» пройден, результат записан
                    self.st.log_check(eid, "адрес", "нашлось" if "соседей по адресу:" in res.extra["address"]
                                      else "пусто", self.run_id, self.today, res.extra["address"])
                if bo_id:
                    self.st.add_statement(eid, {"prop": "bo_id", "value": bo_id, "src_class": "official",
                                                "src_url": f"https://bo.nalog.gov.ru/organizations-card/{bo_id}"},
                                          self.run_id, self.today)
                    self.st.db.commit()
        if self.bfo is not None and not self.st.fresh(eid, "bfo", self.today) and time.monotonic() - t0 < self.budget_sec:
            res = self.bfo.collect(inn, bo_id)
            self.st.ingest(res, self.run_id, self.today, requested_inn=inn)
            if not res.ok:
                partial.append(f"ГИР БО: {res.error}")
        if self.pb is not None and time.monotonic() - t0 < self.budget_sec:
            stat = expand(self.st, eid, self.pb.person_companies, self.R, self.run_id, self.today, self.log)
            if stat["failed"]:
                partial.append(f"связи ФЛ: не открылось {stat['failed']}")
        elif self.pb is not None:
            partial.append("бюджет времени на компанию исчерпан")
        covered = set()
        agg = ("checko_api", "companium", "checko")
        if self.checko is not None and not any(self.st.fresh(eid, ch, self.today) for ch in agg):
            res = self.checko.collect(inn)
            got = self.st.ingest(res, self.run_id, self.today, requested_inn=inn)
            if got and res.extra.get("cases"):               # суды по mOS: дело — узел графа, стороны — рёбра
                ingest_cases(self.st, eid, res.extra["cases"], self.run_id, self.today)
            if not res.ok:
                partial.append(f"суды/ФССП: {res.error}")
            elif not got:
                partial.append(f"{res.channel}: чужая карточка — отброшена")
        if any(self.st.checks(eid).get(ch) == "нашлось" for ch in agg):
            covered = {"КАД (суды)", "ФССП", "ЕФРСБ (банкротство)"}    # закрыты через агрегатор
        if self.news is not None and self.classify is not None and not self.st.fresh(eid, "media", self.today):
            self.media(eid, inn)
        for ch, why in self.unavailable.items():
            if why and ch not in covered:
                self.st.log_check(eid, ch, "не открылось", self.run_id, self.today, why)
        self.st.db.commit()
        status = "partial" if partial else "collected"
        self.st.queue_set(self.run_id, row["pos"], status, eid=eid, inn=inn,
                          note=how + ("; " + "; ".join(partial) if partial else ""))
        return status

    def media(self, eid, inn):
        from collectors.base import CollectResult, Statement

        lt = self.st.latest(eid)
        val = lambda p: lt[p][0]["value"] if lt.get(p) else None
        name = self.st.name(eid)
        try:
            heads = self.news.headlines(name, self.st.db.execute("SELECT region FROM entity WHERE id=?",
                                                                 (eid,)).fetchone()["region"])
            found = self.classify({"name": name, "inn": inn, "okved": val("okved_main"),
                                   "region": self.st.db.execute("SELECT region FROM entity WHERE id=?",
                                                                (eid,)).fetchone()["region"]}, heads, self.usage)
        except Exception as e:                           # noqa: BLE001 — СМИ не обязательный канал
            if type(e).__name__ == "LimitReached":
                self.classify = None
                self.journal.append(f"LLM-шаг СМИ выключен до конца прогона: {e}")
                self.log(f"  {e} — дальше СМИ только из снимка")
            self.st.log_check(eid, "media", "не открылось", self.run_id, self.today, str(e)[:200])
            self.st.db.commit()
            return
        res = CollectResult("media", True, inn, inn, statements=[Statement(**f) for f in found],
                            not_found=[] if heads else ["СМИ"])
        self.st.ingest(res, self.run_id, self.today, requested_inn=inn)

    def collect_all(self):
        todo = self.st.queue(self.run_id, statuses=("new",))
        for i, row in enumerate(todo, 1):
            t0 = time.monotonic()
            st = self.process(row)
            self.log(f"[{i}/{len(todo)}] {row['name']}: {st} ({time.monotonic() - t0:.0f} с)")

    # ---------- волна фронтира (mOS): раскрыть соседей-ЮЛ кодом ----------
    def frontier_wave(self, cards: list[dict]) -> int:
        from graph.frontier import wave

        F = self.R.get("frontier") or {}
        opened = 0
        for c in cards:
            if c["color"] not in F.get("colors", ["red", "yellow"]) or c["eid"].startswith("ul?"):
                continue
            st = wave(self.st, c["eid"], self.pb, self.R, self.run_id, self.today, F.get("per_company", 3))
            if st["opened"] or st["failed"]:
                self.log(f"  фронтир {c['name']}: раскрыто {st['opened']} ({', '.join(st['nodes'])})"
                         + (f", не открылось {st['failed']}" if st["failed"] else ""))
            opened += st["opened"]
        return opened

    # ---------- глубокий разбор красных (методика mOS) ----------
    def deep_run(self, cards: list[dict], limit: int | None, out_dir: Path):
        """Глубокий разбор в пределах бюджета прогона: сначала красные, затем жёлтые по тяжести флагов.
        Разбор из кэша бюджет не тратит; следующий берётся, только если в остатке есть оценка на одну компанию."""
        from deep.analyze import analyze, deep_cfg, render
        from llm.media_classify import LimitReached
        from scoring.score import deep_queue

        cfg = deep_cfg()
        budget, est = cfg["run_budget_tokens"], cfg["est_tokens_per_company"]
        queue = deep_queue(cards, self.R)[: (limit or cfg["max_companies"])]
        spent, done = 0, []
        for c, sev in queue:
            if budget - spent < est:
                rest = [x["name"] for x, _ in queue[len(done):]]
                self.journal.append(f"Глубокий разбор: бюджет прогона {budget:,} исчерпан ({spent:,} потрачено); "
                                    .replace(",", " ") + f"без разбора: {', '.join(rest)}")
                break
            why = "красная карточка" if c["color"] == "red" else f"жёлтая карточка, тяжесть флагов {sev:g}"
            self.log(f"  глубокий разбор (mOS): {c['name']} — {why}")
            try:
                d = analyze(self.st, c, self.run_id, self.usage, why)
            except LimitReached as e:
                self.journal.append(f"Глубокий разбор остановлен: {e}")
                break
            except Exception as e:                       # noqa: BLE001 — разбор не блокирует карточку
                d = {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:150]}"}
            spent += d.get("tokens", 0)
            done.append(c["name"])
            path = out_dir / "deep" / f"{c['inn']}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render(c, d), encoding="utf-8")
            crit = d.get("critic") or {}
            c["deep"] = {"status": d.get("status"), "summary": d.get("summary"), "what_to_check": d.get("what_to_check"),
                         "critic": crit.get("verdict"), "counterview": crit.get("counterview"),
                         "file": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)}
            if d.get("status") == "ok":
                c["explain"] += (f"\nГлубокий разбор (методика mOS): {d.get('summary', '').strip()}"
                                 + "".join(f"\n  — проверить: {x}" for x in d.get("what_to_check", [])[:5])
                                 + f"\nКритик: {crit.get('verdict')}"
                                 + (f" — {crit.get('counterview')}" if crit.get("counterview") else "")
                                 + f"\nПолный разбор: {c['deep']['file']}")
            else:
                c["explain"] += f"\nГлубокий разбор не получился: {d.get('reason')}"
            self.st.save_card(self.run_id, c)
        if done:
            self.journal.append(f"Глубокий разбор: {len(done)} компаний, {spent:,} токенов из бюджета прогона {budget:,}"
                                .replace(",", " "))

    # ---------- скоринг и карточки ----------
    def group_of(self, eid):
        group = set()
        for n in self.st.neighbours(eid):
            if n["other"].startswith("fl") and not any(x["rel"] == "конкурсный_управляющий"
                                                       for x in self.st.neighbours(n["other"])):
                for n2 in self.st.neighbours(n["other"]):
                    if n2["other"] != eid and n2["other"].startswith("ul:"):
                        group.add(self.st.name(n2["other"]))
        return sorted(group)

    def score_all(self) -> list[dict]:
        cache, cards = {}, []
        today = date.fromisoformat(self.today)
        for row in self.st.queue(self.run_id):
            eid = row["eid"] or entity_id(row["inn"], row["name"], "ЮЛ")
            c = score_company(self.st, eid, self.R, cache, today)
            if not c.resolved:
                c.name = row["name"]
            if c.resolved and (row["note"] or "").startswith("по подсказке"):
                c.flags.append(Flag("I_resolved_hint", "info", row["note"], "hypothesis",
                                    f"https://pb.nalog.ru/search.html#mode=search-all&queryAll={c.inn}",
                                    self.today, "official"))
                c.action += "; компания опознана по подсказке — сверить ИНН с извещением"
            d = c.to_dict()
            closed, gaps = group_status(self.st, eid, self.R) if c.resolved else (False, ["компания не опознана"])
            d.update(run_id=self.run_id, origin=row["origin"] or "", query=row["query"],
                     resolve_note=row["note"], group=self.group_of(eid) if c.resolved else [],
                     group_closed=closed, group_gaps=gaps,
                     courts=court_picture(self.st, eid, today) if c.resolved else [],
                     tender={"source": row["origin"]} if row["origin"] else None)
            d["explain"] = explain(c, row["note"]) + "".join(f"\nСуды: {x}" for x in d["courts"]) + (
                f"\nГруппа (общие владельцы/руководители): {', '.join(d['group'])}" if d["group"] else "") + (
                ("\nСостав группы: закрыт (учредители, руководитель, адрес, суды пройдены; ФЛ раскрыты)" if closed
                 else f"\nСостав группы не закрыт, не проверено: {', '.join(gaps)}") if c.resolved else "")
            errs = validate_card(d)
            if errs:
                self.journal.append(f"ВАЛИДАТОР {d['name']}: " + "; ".join(errs))
            self.st.save_card(self.run_id, d)
            self.st.queue_set(self.run_id, row["pos"], "scored")
            cards.append(d)
        cards.sort(key=lambda c: (ORDER[c["color"]], -(c["revenue"] or 0)))
        return cards


def read_input(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [{"name": r["name"], "inn": r.get("inn") or None, "query": r.get("query") or r["name"],
                 "origin": r.get("origin")} for r in csv.DictReader(f)]


def main(argv=None):
    from collectors.bfo import Bfo
    from collectors.checko import Checko
    from collectors.checko_api import CheckoApi, load_key
    from collectors.companium import CachedOnly, Companium, FirstAvailable
    from collectors.news import GoogleNews
    from collectors.pb_nalog import PbNalog
    from collectors.snapshot import load_llm, load_rp
    from collectors.unavailable import probe
    from core.http import FileCache, Http
    from llm import media_classify

    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(ROOT / "input" / "example.csv"))
    ap.add_argument("--run-id", default=f"run-{date.today().isoformat()}")
    ap.add_argument("--db", default=str(ROOT / "data" / "riskpipe.db"))
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--offline", action="store_true", help="без сети: только снимок теста и кэш")
    ap.add_argument("--no-snapshot", action="store_true", help="только официальные источники")
    ap.add_argument("--budget-sec", type=float, default=900)
    ap.add_argument("--no-aggregators", action="store_true", help="без Checko и СМИ")
    ap.add_argument("--web-aggregators", action="store_true",
                    help="разрешить чтение веб-страниц Companium/Checko по списку (по умолчанию выключено)")
    ap.add_argument("--no-deep", action="store_true", help="без глубокого разбора (методика mOS)")
    ap.add_argument("--no-frontier", action="store_true", help="без волны фронтира (раскрытие соседей-ЮЛ)")
    ap.add_argument("--deep-limit", type=int, default=None,
                    help="максимум компаний на глубокий разбор (по умолчанию deep.max_companies из rules.yaml)")
    a = ap.parse_args(argv)

    out_dir = ROOT / "out" / a.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    logf = open(out_dir / "run.log", "a", encoding="utf-8")

    def log(msg):
        print(msg, flush=True)
        logf.write(msg + "\n")
        logf.flush()

    st = Store(a.db)
    http = Http(log=log)
    cache = FileCache(ROOT / "data" / "cache")
    raw = ROOT / "data" / "raw"
    snapshot = [] if a.no_snapshot else load_rp(raw) + load_llm(raw)
    pb = bfo = checko = news = None
    unavailable = {}
    if not a.offline:
        pb, bfo = PbNalog(http, cache, log=log), Bfo(http, cache)
        key = load_key(ROOT)
        # правило mOS: витрины не выкачиваются циклом по списку ИНН — веб-сбор только по явному флагу
        web = [Companium(http, cache, log=log), Checko(http, cache, log=log)]
        chain = ([CheckoApi(http, cache, key, date.fromisoformat(a.today), log=log)] if key else []) +             (web if a.web_aggregators else [CachedOnly(w) for w in web])
        log("Суды/ФССП: " + ("Checko API по ключу" if key else "ключа CHECKO_API_KEY нет")
            + ("; запасной канал — веб-страницы Companium/Checko (--web-aggregators)" if a.web_aggregators
               else "; веб-витрины выключены (правило mOS), из них — только ранее скачанное"))
        checko, news = (None, None) if a.no_aggregators else (FirstAvailable(*chain, log=log), GoogleNews(http, cache))
        unavailable = probe(http)
        log("Недоступные каналы: " + "; ".join(f"{k}: {v}" for k, v in unavailable.items() if v))
    p = Pipeline(st, a.run_id, a.today, pb=pb, bfo=bfo, snapshot=snapshot, unavailable=unavailable,
                 budget_sec=a.budget_sec, log=log, checko=checko, news=news,
                 classify=media_classify.classify if media_classify.available() else None)
    rows = read_input(Path(a.input))
    p.load(rows)
    t0 = time.monotonic()
    p.collect_all()
    cards = p.score_all()
    if pb is not None and not a.no_frontier:
        before = {c["eid"]: c["color"] for c in cards}
        if p.frontier_wave(cards):
            cards = p.score_all()                         # перенос риска пересчитан по раскрытому графу
            for c in cards:
                if before.get(c["eid"]) != c["color"]:
                    p.journal.append(f"После волны фронтира: {c['name']} {before.get(c['eid'])} → {c['color']}")
    if not a.no_deep and not a.offline and media_classify.backend():
        p.deep_run(cards, a.deep_limit, out_dir)
    be = media_classify.backend()
    llm_note = ("LLM-классификатор СМИ: " + {"api": "Anthropic API", "cli": "Claude Code CLI (подписка, без API-ключа)"}[be]
                + f" — вызовов {p.usage.get('calls', 0)} (из кэша {p.usage.get('cached', 0)}), токенов {p.usage.get('tokens_in', 0)} вход / "
                  f"{p.usage.get('tokens_out', 0)} выход, ${p.usage.get('cost_usd', 0):.3f}") if be else         "LLM-классификатор СМИ: недоступен — СМИ из снимка теста"
    run = {"run_id": a.run_id,
           "notes": f"Сбор {time.monotonic() - t0:.0f} с; запросов: {json.dumps(http.calls, ensure_ascii=False)}; "
                    f"капч pb.nalog.ru: {getattr(pb, 'n_captcha', 0)}. {llm_note}. Источники: "
                    + ("ФНС pb.nalog.ru и ГИР БО (официальные) + " if pb else "")
                    + (("Checko API по ключу (суды за 12 мес, ФССП) + " if load_key(ROOT) else "")
                       + "Companium/Checko веб (санкции, блокировки, ЕФРСБ; запасной канал судов) + RSS Google News + "
                       if checko else "")
                    + ("снимок из data/raw." if snapshot else "без снимка.")}
    st.run_update(a.run_id, finished=time.strftime("%Y-%m-%dT%H:%M:%S"), n_done=len(cards),
                  tokens_in=p.usage.get("tokens_in", 0), tokens_out=p.usage.get("tokens_out", 0),
                  errors=sum(1 for r in st.queue(a.run_id) if r["note"] and "не открыл" in r["note"]),
                  http_calls=http.calls, notes=run["notes"])
    json.dump(cards, open(out_dir / "cards.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    (out_dir / "report.md").write_text(report(cards, run, p.journal), encoding="utf-8")
    old = ROOT / "out" / "cards.json"
    if old.exists():
        (out_dir / "compare.md").write_text(compare(cards, json.loads(old.read_text(encoding="utf-8"))),
                                            encoding="utf-8")
    json.dump(http.journal, open(out_dir / "http_journal.json", "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    ids = [c["eid"] for c in cards if not c["eid"].startswith("ul?")]
    plan = st.refresh_plan(ids, a.today)
    cnt = {k: sum(1 for c in cards if c["color"] == k) for k in ORDER}
    log(f"Итог: {cnt}; карточки: {out_dir / 'cards.json'}; план на завтра: "
        f"{sum(1 for v in plan.values() if v == ['*'])} компаний целиком (аудит), остальные — быстрые каналы и протухшее")
    for j in p.journal:
        log(j)


if __name__ == "__main__":
    main()
