"""Классификация заголовков СМИ малой моделью (шаг 5 конвейера).

Два бэкенда, одна схема выхода:
- api — Anthropic SDK с ANTHROPIC_API_KEY (tool use, кэш системного промпта; в проде — Batch API в 05:00);
- cli — установленный Claude Code (`claude -p`) на подписке пользователя, без API-ключа. Свой системный
  промпт, без инструментов и настроек проекта: ~300–1500 токенов на компанию вместо 38 тыс. по умолчанию.

Модели не доверяем: цитата должна дословно встречаться в переданном заголовке, url — быть из переданного
списка, иначе пункт отбрасывается. Выход — утверждения media_negative класса media (гипотеза, максимум жёлтый).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "llm"
MODEL_API = "claude-haiku-5-5"
MODEL_CLI = "haiku"
TAGS = ["bankruptcy", "nonpayment", "blocks", "criminal", "production_stop", "owner_change", "sanctions", "other"]
SYSTEM = (
    "Ты классифицируешь заголовки новостей о российской компании для оценки риска неоплаты поставщику. "
    "Отмечай только события именно этой компании (сверяй название, город, отрасль; тёзки и однофамильцы — не она): "
    "банкротство, иски поставщиков о неоплате, аресты счетов, уголовные дела руководства, остановка производства "
    "(атаки, пожары, аварии с остановкой самого предприятия), изъятие/национализация или продажа самой компании, санкции. "
    "НЕ отмечай: плановые и аварийные отключения коммунальных услуг (это обычная работа сетей), покупку компанией "
    "чужих активов, рост и инвестиции, награды, нейтральные новости, события тёзок. Сомневаешься — не отмечай. "
    "quote — дословный фрагмент заголовка (до 25 слов), url — ссылка ровно из списка. "
    'Ответ — только JSON без пояснений: {"items":[{"n":номер,"tag":"' + "|".join(TAGS) +
    '","quote":"...","url":"...","harmed":true|false,"confidence":0..1}]}; если ничего нет — {"items":[]}. '
    "harmed=true только если событие ухудшает платёжеспособность ИМЕННО этой компании: она должник или ответчик, "
    "её изымают или продают, её предприятие остановлено, её руководство обвиняют. Если компания истец, покупатель, "
    "кредитор или победитель — harmed=false."
)


def claude_exe() -> str | None:
    """Путь к claude.exe: на Windows обёртка claude.cmd через cmd.exe ломает аргументы с | и кавычками."""
    w = shutil.which("claude")
    if not w:
        return None
    exe = os.path.join(os.path.dirname(w), "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
    return exe if w.lower().endswith(".cmd") and os.path.exists(exe) else w


class LimitReached(RuntimeError):
    """Лимит подписки/API исчерпан — шаг СМИ выключается до конца прогона."""


def backend() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            return "api"
        except ImportError:
            pass
    if claude_exe():
        return "cli"
    return None


def available() -> bool:
    return backend() is not None


def _user(company: dict, headlines: list[dict]) -> str:
    lines = [f"{i}. {h['date']} | {h['title']} | {h['url']}" for i, h in enumerate(headlines, 1)]
    return (f"Компания: {company['name']}, ИНН {company['inn']}, регион: {company.get('region') or '?'}, "
            f"ОКВЭД: {company.get('okved') or '?'}\nЗаголовки:\n" + "\n".join(lines))


def _call_cli(user: str, usage: dict) -> dict:
    cmd = [claude_exe(), "-p", "--model", MODEL_CLI, "--output-format", "json", "--max-turns", "1",
           "--system-prompt", SYSTEM, "--tools", "", "--setting-sources", "", "--strict-mcp-config"]
    env = {**os.environ, "MAX_THINKING_TOKENS": "0"}   # без «размышления»: иначе ~5 тыс. выходных токенов на вызов
    r = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=180, cwd=os.environ.get("TEMP") or None, env=env)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude -p: код {r.returncode}: {(r.stderr or r.stdout or '')[:200]}")
    if d.get("is_error"):
        msg = str(d.get("result") or d.get("terminal_reason"))
        if d.get("api_error_status") == 429 or "limit" in msg.lower():
            raise LimitReached(f"claude -p: {msg}")
        raise RuntimeError(f"claude -p: {msg[:200]}")
    u = d.get("usage") or {}
    usage["tokens_in"] = usage.get("tokens_in", 0) + u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) \
        + u.get("cache_creation_input_tokens", 0)
    usage["tokens_out"] = usage.get("tokens_out", 0) + u.get("output_tokens", 0)
    usage["cost_usd"] = usage.get("cost_usd", 0) + (d.get("total_cost_usd") or 0)
    txt = d.get("result") or ""
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group()) if m else {"items": []}


def _call_api(user: str, usage: dict) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL_API, max_tokens=800,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}])
    usage["tokens_in"] = usage.get("tokens_in", 0) + msg.usage.input_tokens
    usage["tokens_out"] = usage.get("tokens_out", 0) + msg.usage.output_tokens
    txt = "".join(b.text for b in msg.content if b.type == "text")
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group()) if m else {"items": []}


def accept(items: list[dict], headlines: list[dict], min_conf: float = 0.6) -> list[dict]:
    """Проверка кодом: url из списка, цитата дословно из заголовка этого url, известный тег."""
    by_url = {h["url"]: h for h in headlines}
    out = []
    for it in items or []:
        h = by_url.get(it.get("url"))
        if h is None and isinstance(it.get("n"), int) and 1 <= it["n"] <= len(headlines):
            h = headlines[it["n"] - 1]
        q = (it.get("quote") or "").strip()
        norm = lambda s: re.sub(r"\s+", " ", s.replace("«", '"').replace("»", '"')).lower()
        if not h or not q or norm(q) not in norm(h["title"]) or it.get("tag") not in TAGS:
            continue
        if it.get("harmed") is not True or (it.get("confidence") or 0) < min_conf:
            continue
        out.append({"prop": "media_negative", "value": f"{it['tag']}: {h['title'][:160]}",
                    "src_url": h["url"], "src_class": "media", "quote": q, "published": h.get("date")})
    return out


def classify(company: dict, headlines: list[dict], usage: dict) -> list[dict]:
    """headlines: [{title, url, date}] -> утверждения media_negative."""
    be = backend()
    if not headlines or be is None:
        return []
    user = _user(company, headlines)
    # стабильность: те же заголовки -> тот же ответ (повторный прогон не переспрашивает модель 7 дней)
    key = hashlib.sha1((SYSTEM + "\n" + user).encode("utf-8")).hexdigest()
    path = CACHE_DIR / f"{key}.json"
    if path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
        raw = json.loads(path.read_text(encoding="utf-8"))
        usage["cached"] = usage.get("cached", 0) + 1
    else:
        raw = _call_api(user, usage) if be == "api" else _call_cli(user, usage)
        usage["calls"] = usage.get("calls", 0) + 1
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return accept(raw.get("items", []), headlines)
