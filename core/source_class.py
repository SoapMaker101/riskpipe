"""Единый класс источника для всех коннекторов: домен -> official | aggregator | company_site | media.

Вид утверждения (факт / вторичный факт / гипотеза) выводится отсюда кодом, а не моделью.
"""
from __future__ import annotations

from urllib.parse import urlparse

SRC_KIND = {
    "official": "fact",        # ЕГРЮЛ, ФНС, ГИР БО, КАД, ЕФРСБ, ЕИС, ФССП
    "licensed": "fact",        # СПАРК / Контур.Фокус / Checko API по договору
    "aggregator": "fact2",     # вторичный факт: годится, но красное — подтвердить
    "company_site": "fact2",
    "media": "hypothesis",     # СМИ, Telegram, «жёлтая пресса»
}
KIND_RANK = {"fact": 0, "fact2": 1, "hypothesis": 2}

OFFICIAL = ("nalog.ru", "nalog.gov.ru", "kad.arbitr.ru", "arbitr.ru", "fedresurs.ru", "zakupki.gov.ru",
            "fssp.gov.ru", "sudrf.ru", "vestnik-gosreg.ru", "gov.ru", "cbr.ru")
AGGREGATOR = ("rusprofile.ru", "checko.ru", "list-org.com", "companium.ru", "audit-it.ru",
              "zachestnyibiznes.ru", "sbis.ru", "saby.ru", "kontur.ru", "vbankcenter.ru",
              "rostender.info", "bidzaar.com", "dadata.ru", "ofdata.ru", "damia.ru")
MEDIA = ("t.me", "telegram", "vk.com", "dzen.ru", "pikabu.ru")


def host(url: str | None) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def classify(url: str | None, default: str = "media") -> str:
    h = host(url)
    if not h:
        return default
    if any(h == d or h.endswith("." + d) for d in OFFICIAL):
        return "official"
    if any(h == d or h.endswith("." + d) for d in AGGREGATOR):
        return "aggregator"
    if any(d in h for d in MEDIA):
        return "media"
    return default


def kind_of(src_class: str | None) -> str:
    return SRC_KIND.get(src_class or "", "fact2")
