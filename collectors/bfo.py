"""ГИР БО (bo.nalog.gov.ru): бухгалтерская отчётность. Выручка (2110), чистая прибыль (2400),
капитал и резервы (1300 ≈ чистые активы) за последний опубликованный год и предыдущий.

id организации в ГИР БО берётся из карточки pb.nalog.ru (bourl). Суммы в отчётности — тыс. ₽.
"""
from __future__ import annotations

from collectors.base import CollectResult, Statement
from core.http import FileCache, Http, Unavailable

BASE = "https://bo.nalog.gov.ru/nbo"
CH = "bfo"
CLS = "official"
J = {"Accept": "application/json"}


class Bfo:
    def __init__(self, http: Http, cache: FileCache, interval: float = 1.5):
        self.http = http
        self.cache = cache
        self.http.min_interval.setdefault("bo.nalog.gov.ru", interval)

    def _get(self, path: str):
        r = self.http.request("GET", BASE + path, headers=J)
        if r.status_code != 200 or not r.text.strip().startswith(("{", "[")):
            raise Unavailable(f"bo.nalog.gov.ru: HTTP {r.status_code}")
        return r.json()

    def fetch(self, bo_id: str) -> dict:
        hit = self.cache.get("bfo", bo_id, 30)
        if hit is not None:
            return hit
        org = self._get(f"/organizations/{bo_id}")
        periods = sorted({b["period"] for b in org.get("bfo") or [] if b.get("published", True)}, reverse=True)
        out = {"inn": org.get("inn"), "name": org.get("shortName"), "period": None, "details": None}
        if periods:
            lst = self._get(f"/organizations/{bo_id}/bfo/")
            match = [b for b in lst if b.get("period") == periods[0]] or lst[:1]
            if match:
                det = self._get(f"/bfo/{match[0]['id']}/details")
                out["period"] = match[0].get("period")
                out["details"] = det[0] if isinstance(det, list) and det else det
        self.cache.put("bfo", bo_id, out)
        return out

    def collect(self, inn: str, bo_id: str | None) -> CollectResult:
        if not bo_id:
            return CollectResult(channel=CH, ok=True, inn=inn, page_inn=inn, not_found=["отчётность ГИР БО"])
        try:
            d = self.fetch(bo_id)
        except Unavailable as e:
            return CollectResult.fail(CH, inn, str(e))
        url = f"https://bo.nalog.gov.ru/organizations-card/{bo_id}"
        res = CollectResult(channel=CH, ok=True, inn=inn, page_inn=d.get("inn"), name=d.get("name"))
        det, y = d.get("details"), d.get("period")
        if not det or not y:
            res.not_found.append("отчётность ГИР БО")
            return res
        y = int(y)
        bal, fin = det.get("balance") or {}, det.get("financialResult") or {}

        def put(prop, src, code):
            for pref, yy in (("current", y), ("previous", y - 1)):
                v = src.get(f"{pref}{code}")
                if v is not None:
                    res.statements.append(Statement(prop, str(float(v) * 1000), url, CLS, year=yy))

        put("revenue", fin, "2110")
        put("net_profit", fin, "2400")
        put("equity", bal, "1300")
        return res
