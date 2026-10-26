"""База утверждений со сроком годности (FollowTheMoney-подобная) + журнал прогонов и очередь.

Перенос store.py из прототипа. Новое:
- антиподмена в коде: ИНН на странице = ИНН запроса и проходит контрольную сумму,
  иначе запись в check_log «чужая карточка» и ни одного утверждения;
- «нет src_url — нет строки» для утверждений и рёбер;
- таблицы run, card, queue (чекпойнт на каждую компанию);
- fresh(): канал не перезапрашивается, пока его последнее «нашлось» не протухло.
"""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from core.ids import entity_id, entity_type, inn_valid
from core.source_class import KIND_RANK, kind_of

# срок годности утверждения, дней (по свойству)
TTL_DAYS = {
    "bankruptcy": 1, "arb_total_count": 1, "arb_total_sum": 1, "arb_recent_years": 1,
    "arb_defendant_count": 1, "arb_defendant_sum": 1, "arb_plaintiff_count": 1,
    "enforcement_count": 3, "enforcement_sum": 3, "account_blocks": 1,
    "status": 7, "unreliable_info": 7, "mass_address": 30,
    "mass_director": 30, "mass_founder": 30, "disqualified": 7, "sanctions": 1,
    "director_changes": 7, "tax_debt": 30, "companies_count": 7,
    "revenue": 365, "net_profit": 365, "equity": 365, "employees": 365,
    "okved_main": 30, "okopf": 365, "reg_date": 3650, "gov_contracts_count": 7,
    "gov_contracts_sum": 7, "media_negative": 7, "link": 7,
}
ALWAYS_REFRESH = {"bankruptcy", "arb_defendant_sum", "account_blocks", "status"}
AUDIT_SHARE = 0.10
# срок годности канала: сколько дней не перезапрашивать после «нашлось»
CHANNEL_TTL = {"pb_nalog": 1, "bfo": 30, "pb_person": 7, "snapshot": 3650, "checko": 1, "media": 7}

SCHEMA = """
CREATE TABLE IF NOT EXISTS entity(
  id TEXT PRIMARY KEY, type TEXT, name TEXT, region TEXT, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS statement(
  id INTEGER PRIMARY KEY, entity TEXT, prop TEXT, value TEXT, year INTEGER,
  src_url TEXT, src_class TEXT, kind TEXT, quote TEXT,
  published TEXT, observed TEXT, expires TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS edge(
  id INTEGER PRIMARY KEY, src TEXT, dst TEXT, rel TEXT, share_pct REAL,
  src_url TEXT, src_class TEXT, kind TEXT, observed TEXT, expires TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS check_log(
  entity TEXT, channel TEXT, result TEXT, observed TEXT, run_id TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS run(
  run_id TEXT PRIMARY KEY, started TEXT, finished TEXT, n_input INTEGER, n_done INTEGER,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
  http_calls TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS card(
  run_id TEXT, inn TEXT, eid TEXT, color TEXT, coverage REAL, flags_json TEXT, action TEXT,
  tender_json TEXT, card_json TEXT, PRIMARY KEY(run_id, eid));
CREATE TABLE IF NOT EXISTS queue(
  run_id TEXT, pos INTEGER, query TEXT, inn TEXT, name TEXT, origin TEXT, eid TEXT,
  status TEXT, attempts INTEGER DEFAULT 0, updated TEXT, note TEXT, PRIMARY KEY(run_id, pos));
CREATE INDEX IF NOT EXISTS ix_st ON statement(entity, prop);
CREATE INDEX IF NOT EXISTS ix_e1 ON edge(src); CREATE INDEX IF NOT EXISTS ix_e2 ON edge(dst);
CREATE INDEX IF NOT EXISTS ix_cl ON check_log(entity, channel);
"""


def _exp(today: str, days: int) -> str:
    return (date.fromisoformat(today) + timedelta(days=days)).isoformat()


class Store:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---------- запись ----------
    def upsert_entity(self, eid, typ, name, region=None, today=None):
        today = today or date.today().isoformat()
        cur = self.db.execute("SELECT name FROM entity WHERE id=?", (eid,)).fetchone()
        if cur:
            self.db.execute("UPDATE entity SET last_seen=?, name=COALESCE(name, ?), region=COALESCE(region, ?) "
                            "WHERE id=?", (today, name, region, eid))
        else:
            self.db.execute("INSERT INTO entity VALUES(?,?,?,?,?,?)", (eid, typ, name, region, today, today))

    def add_statement(self, eid, st: dict, run_id, today) -> bool:
        if not st.get("src_url"):                 # нет источника — нет утверждения
            return False
        prop = st["prop"]
        cls = st.get("src_class") or "aggregator"
        val = str(st.get("value"))
        # идемпотентность в пределах прогона: тот же факт из того же источника не дублируется
        dup = self.db.execute("SELECT 1 FROM statement WHERE entity=? AND prop=? AND value=? AND "
                              "COALESCE(year,0)=? AND src_url=? AND run_id=?",
                              (eid, prop, val, st.get("year") or 0, st["src_url"], run_id)).fetchone()
        if dup:
            return False
        self.db.execute(
            "INSERT INTO statement(entity,prop,value,year,src_url,src_class,kind,quote,"
            "published,observed,expires,run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, prop, val, st.get("year"), st["src_url"], cls, kind_of(cls), st.get("quote"),
             st.get("published"), today, _exp(today, TTL_DAYS.get(prop, 7)), run_id))
        return True

    def add_edge(self, src, dst, rel, share, url, cls, run_id, today, kind=None):
        """kind — явная пометка (граф mOS: conf «гипотеза»); иначе выводится из класса источника."""
        if not url:
            return
        dup = self.db.execute("SELECT 1 FROM edge WHERE src=? AND dst=? AND rel=? AND run_id=?",
                              (src, dst, rel, run_id)).fetchone()
        if dup:
            return
        self.db.execute(
            "INSERT INTO edge(src,dst,rel,share_pct,src_url,src_class,kind,observed,expires,run_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (src, dst, rel, share, url, cls, kind or kind_of(cls), today, _exp(today, TTL_DAYS["link"]), run_id))

    def log_check(self, eid, channel, result, run_id, today, note=None):
        self.db.execute("INSERT INTO check_log VALUES(?,?,?,?,?,?)", (eid, channel, result, today, run_id, note))

    def ingest(self, res, run_id: str, today: str, requested_inn: str | None = None) -> str | None:
        """Принять CollectResult (collectors.base). Возвращает id компании или None."""
        req = requested_inn or res.inn
        if not res.ok:
            if req and inn_valid(req):
                eid = f"ul:{req}"
                self.upsert_entity(eid, "ЮЛ", res.name, None, today)
                self.log_check(eid, res.channel, "не открылось", run_id, today, res.error)
                self.db.commit()
            return None
        # антиподмена: ИНН страницы проходит контрольную сумму и совпадает с запросом
        page = res.page_inn or res.inn
        if not inn_valid(page) or (req and page != req):
            if req and inn_valid(req):
                self.log_check(f"ul:{req}", res.channel, "чужая карточка", run_id, today,
                               f"на странице ИНН {page}, запрашивали {req}")
                self.db.commit()
            return None
        eid = entity_id(page, res.name or page, entity_type(page, res.entity_type))
        self.upsert_entity(eid, entity_type(page, res.entity_type), res.name, res.region, today)
        for st in res.statements:
            self.add_statement(eid, st.as_dict(), run_id, today)
        for ln in res.links:
            typ = entity_type(ln.other_inn, ln.other_type)
            oid = entity_id(ln.other_inn, ln.other_name or "?", typ)
            self.upsert_entity(oid, typ, ln.other_name, None, today)
            # статус соседа из чужой выдачи не перебивает свежую карточку самой компании
            own_fresh = self.db.execute("SELECT 1 FROM statement WHERE entity=? AND prop='status' AND kind='fact' "
                                        "AND observed>=?", (oid, today)).fetchone()
            if ln.other_status and not own_fresh:
                self.add_statement(oid, {"prop": "status", "value": ln.other_status, "src_url": ln.src_url,
                                         "src_class": ln.src_class}, run_id, today)
            for prop, val in (ln.other_props or {}).items():
                self.add_statement(oid, {"prop": prop, "value": val, "src_url": ln.src_url,
                                         "src_class": ln.src_class}, run_id, today)
            # ребро всегда «кто -> над кем»
            if ln.dir == "out":
                self.add_edge(eid, oid, ln.rel, ln.share_pct, ln.src_url, ln.src_class, run_id, today)
            else:
                self.add_edge(oid, eid, ln.rel, ln.share_pct, ln.src_url, ln.src_class, run_id, today)
        for ch in res.not_found:
            self.log_check(eid, ch, "пусто", run_id, today)
        for ch in res.unavailable:
            self.log_check(eid, ch, "не открылось", run_id, today)
        self.log_check(eid, res.channel, "нашлось", run_id, today)
        self.db.commit()
        return eid

    # ---------- чтение ----------
    def latest(self, eid: str) -> dict[str, list[sqlite3.Row]]:
        """Все наблюдения по свойствам; порядок: факт > агрегатор > гипотеза, затем свежесть."""
        rows = self.db.execute("SELECT * FROM statement WHERE entity=?", (eid,)).fetchall()
        out: dict[str, list] = {}
        for r in rows:
            out.setdefault(r["prop"], []).append(r)
        for v in out.values():
            v.sort(key=lambda r: (KIND_RANK.get(r["kind"], 3), -(r["year"] or 0),
                                  -_ts(r["observed"]), -r["id"]))
        return out

    def neighbours(self, eid: str):
        q = ("SELECT 'out' d, dst other, rel, share_pct, src_url, src_class, kind FROM edge WHERE src=? "
             "UNION ALL SELECT 'in' d, src other, rel, share_pct, src_url, src_class, kind FROM edge WHERE dst=?")
        seen, out = set(), []
        for r in self.db.execute(q, (eid, eid)).fetchall():
            k = (r["d"], r["other"], r["rel"])
            if k not in seen:
                seen.add(k)
                out.append(r)
        return out

    def name(self, eid):
        r = self.db.execute("SELECT name FROM entity WHERE id=?", (eid,)).fetchone()
        return (r["name"] if r else None) or eid

    def checks(self, eid: str) -> dict[str, str]:
        """Последний результат по каждому каналу."""
        out = {}
        for r in self.db.execute("SELECT channel, result FROM check_log WHERE entity=? ORDER BY rowid", (eid,)):
            out[r["channel"]] = r["result"]
        return out

    def fresh(self, eid: str, channel: str, today: str) -> bool:
        r = self.db.execute("SELECT MAX(observed) o FROM check_log WHERE entity=? AND channel=? AND result='нашлось'",
                            (eid, channel)).fetchone()
        if not r or not r["o"]:
            return False
        return date.fromisoformat(r["o"]) + timedelta(days=CHANNEL_TTL.get(channel, 1)) > date.fromisoformat(today)

    # ---------- прогон, очередь, карточки ----------
    def run_start(self, run_id, n_input):
        self.db.execute("INSERT OR IGNORE INTO run(run_id, started, n_input, n_done) VALUES(?,?,?,0)",
                        (run_id, datetime.now().isoformat(timespec="seconds"), n_input))
        self.db.commit()

    def run_update(self, run_id, **kw):
        for k, v in kw.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            self.db.execute(f"UPDATE run SET {k}=? WHERE run_id=?", (v, run_id))
        self.db.commit()

    def queue_load(self, run_id, rows: list[dict]):
        for i, r in enumerate(rows):
            self.db.execute("INSERT OR IGNORE INTO queue(run_id,pos,query,inn,name,origin,status,updated)"
                            " VALUES(?,?,?,?,?,?,'new',?)",
                            (run_id, i, r.get("query") or r.get("name"), r.get("inn"), r.get("name"),
                             r.get("origin"), datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def queue(self, run_id, statuses=None):
        q = "SELECT * FROM queue WHERE run_id=?"
        rows = self.db.execute(q + " ORDER BY pos", (run_id,)).fetchall()
        return [r for r in rows if statuses is None or r["status"] in statuses]

    def queue_set(self, run_id, pos, status, **kw):
        sets = ["status=?", "updated=?"] + [f"{k}=?" for k in kw]
        self.db.execute(f"UPDATE queue SET {', '.join(sets)} WHERE run_id=? AND pos=?",
                        (status, datetime.now().isoformat(timespec="seconds"), *kw.values(), run_id, pos))
        self.db.commit()

    def save_card(self, run_id, card: dict):
        self.db.execute("INSERT OR REPLACE INTO card VALUES(?,?,?,?,?,?,?,?,?)",
                        (run_id, card.get("inn"), card["eid"], card["color"], card["coverage"],
                         json.dumps(card["flags"], ensure_ascii=False), card["action"],
                         json.dumps(card.get("tender"), ensure_ascii=False),
                         json.dumps(card, ensure_ascii=False)))
        self.db.commit()

    # ---------- план следующего прогона ----------
    def refresh_plan(self, company_ids: list[str], today: str, seed: int = 0) -> dict:
        """Что перезапросить завтра: протухшее + быстрые каналы + аудит-выборка."""
        if not company_ids:
            return {}
        rnd = random.Random(seed)
        audit = set(rnd.sample(company_ids, max(1, int(len(company_ids) * AUDIT_SHARE))))
        plan = {}
        for eid in company_ids:
            if eid in audit:
                plan[eid] = ["*"]
                continue
            stale = [r["prop"] for r in self.db.execute(
                "SELECT prop, MAX(expires) e FROM statement WHERE entity=? GROUP BY prop", (eid,))
                if r["e"] <= today]
            plan[eid] = sorted(set(stale) | ALWAYS_REFRESH)
        return plan


def _ts(s: str | None) -> float:
    try:
        return datetime.fromisoformat(s).timestamp() if s else 0
    except ValueError:
        return 0
