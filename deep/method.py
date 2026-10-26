"""Методика глубокого разбора (правила поиска mOS).

Откуда берётся:
- по умолчанию — из папки `methodology/` репозитория (правила mOS, очищенные от устройства бота);
- если задана переменная MOS_ROOT (папка с `mOS_vault/` и `mos/`) — из оригинальных файлов mOS, чтобы правки
  методики в mOS сразу доходили до конвейера.

Части: rules — группа и граф, идентификация, правило источника, правила веба, маршрут (пп. 1–8),
«думать до ответа», запреты, самопроверка; graph — методика графа связей; courts — судебные дела по составу;
sources — источники и правовые рамки; research / research_company — правила исследователя; critic — критик.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from collectors.mos_graph import mos_rels, mos_root

METHOD_DIR = Path(__file__).resolve().parent.parent / "methodology"
SECTIONS = ["### Кейс — это чаще группа", "### Связи живут в графе", "## Идентификация юрлица",
            "### Правило источника", "### Правила для веба", "## Маршрут по активу",
            "## Думать до того, как отвечать", "## Категорически запрещено", "## Самопроверка"]
# часть методики -> (файл в репозитории, путь в mOS, константа в .py mOS)
PARTS = {
    "rules": ("rules.md", "mOS_vault/CLAUDE.md", None),
    "graph": ("graph.md", "mOS_vault/25_Методики/граф_связей.md", None),
    "courts": ("courts.md", "mOS_vault/25_Методики/судебные_дела.md", None),
    "sources": ("sources.md", "mOS_vault/99_System/sources.md", None),
    "research": ("research.md", "mos/vault/.claude/skills/research.py", "BASE_RULES"),
    "research_company": ("research_company.md", "mos/vault/.claude/skills/research.py", "MODES"),
    "critic": ("critic.md", "mos/vault/.claude/skills/critic.py", "SYSTEM"),
}

ADAPT = """\
РЕЖИМ: ты — исследователь, вызванный из пакетного конвейера riskpipe. Вопрос не о покупке актива, а о риске
заказчика для поставщика: не заплатит, затянет, обанкротится, окажется не тем, кем выглядит. Маршрут (пп. 1–8)
применяй к этому вопросу; поиск покупателей и оценку стоимости НЕ делай. Спрашивать некого: чего не нашёл,
пиши в not_found и what_to_check. Записи графа возвращай в ответе (поле graph) в формате graph.jsonl.
Сводку и сведение делает код.
"""

FALLBACK = """\
Нет источника — нет утверждения: у каждого факта url и две даты (опубликовано / посмотрено).
Агрегатор — не первоисточник; одна цифра с одного сайта — гипотеза. Сниппет — не источник.
Не выкачивай витрины и не обходи капчу. «Не нашёл» — валидный ответ.
Суды — не количество, а состав: роль, кто напротив, предмет, сумма, исход; из акта — записи графа.
Связи — закрытым словарём, с conf факт/гипотеза; тёзок и адреса БЦ не тянуть.
"""


def _const(py: Path, name: str) -> str:
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == name:
                val = ast.literal_eval(node.value)
                return val if isinstance(val, str) else (val.get("company", "") if isinstance(val, dict) else "")
    except (OSError, SyntaxError, ValueError):
        pass
    return ""


def doc(part: str) -> tuple[str, str]:
    """(текст, откуда взят). Пустой текст — части нет ни в mOS, ни в репозитории."""
    local, mos_path, const = PARTS[part]
    root = mos_root()
    if root is not None:
        p = root / mos_path
        text = _const(p, const) if const else (p.read_text(encoding="utf-8") if p.exists() else "")
        if text:
            return re.sub(r"^---\n.*?\n---\s*", "", text, flags=re.S), f"mOS: {mos_path}" + (f":{const}" if const else "")
    p = METHOD_DIR / local
    if p.exists():
        return p.read_text(encoding="utf-8"), f"methodology/{local}"
    return "", ""


def _section(text: str, head: str) -> str:
    i = text.find(head)
    if i < 0:
        return ""
    level = head.split(" ")[0]
    rest = text[i + len(head):]
    m = re.search(rf"\n#{{1,{len(level)}}} ", rest)
    body = text[i:i + len(head) + (m.start() if m else len(rest))]
    if head.startswith("## Маршрут"):
        body = body.split("9. **Гипотезы покупателей**")[0] + "\n(пп. 9–11 о покупателях в этом режиме не применяются)\n"
    return body.strip()


def research_system() -> tuple[str, list[str]]:
    """Полная методика (для ручного глубокого разбора и сводки) и список подтянутых частей."""
    used, parts = [], [ADAPT]
    rules, src = doc("rules")
    for h in SECTIONS:
        s = _section(rules, h)
        if s:
            parts.append(s)
            used.append(f"{src}: {h.strip('# ')}")
    for name in ("graph", "courts", "sources"):
        text, src = doc(name)
        if text:
            parts.append(text)
            used.append(src)
    text, src = doc("research")
    if text:
        parts.append("ПРАВИЛА ИССЛЕДОВАТЕЛЯ:\n" + text.split("ФОРМАТ ОТВЕТА")[0])
        used.append(src)
    text, src = doc("research_company")
    if text:
        parts.append(text)
        used.append(src)
    if len(used) < 3:
        parts.append(FALLBACK)
        used.append("запасной свод (методика не найдена)")
    parts.append("ЗАКРЫТЫЙ СЛОВАРЬ СВЯЗЕЙ: " + ", ".join(sorted(mos_rels())))
    return "\n\n".join(parts), used


ADAPT_SHORT = """\
Ты — исследователь в пакетном конвейере riskpipe. Вопрос: риск заказчика для поставщика (не заплатит,
затянет, обанкротится, окажется не тем). Ищешь только то, чего нет в переданных фактах. Без выводов о сделках.
"""


def research_system_compact() -> tuple[str, list[str]]:
    """Сжатая методика для дешёвого чтения материалов (~3 тыс. символов): правила исследователя,
    «что вынимается из акта», «чего не делать» в графе, закрытый словарь связей."""
    parts, used = [ADAPT_SHORT], []
    text, src = doc("research")
    if text:
        body = text.split("ЖЁСТКИЕ ПРАВИЛА:")[-1].split("ФОРМАТ ОТВЕТА")[0].strip()
        parts.append("ПРАВИЛА ИССЛЕДОВАТЕЛЯ:\n" + body)
        used.append(f"{src}: правила")
    for name, head in (("courts", "## Что вынимается из акта"), ("graph", "## Чего не делать")):
        text, src = doc(name)
        s = _section(text, head)
        if s:
            parts.append(s)
            used.append(f"{src}: {head.strip('# ')}")
    if not used:
        parts.append(FALLBACK)
        used.append("запасной свод (методика не найдена)")
    parts.append("Связи — только из словаря: " + ", ".join(sorted(mos_rels())))
    return "\n\n".join(parts), used


def writer_system() -> str:
    """Сводка: правило источника + форма ответа «выводы, а не пересказ»."""
    src = _section(doc("rules")[0], "### Правило источника")
    return ("Ты — аналитик. По фактам и находкам со ссылками пишешь короткий разбор риска заказчика для "
            "поставщика: что случилось, что из этого следует, что проверить перед решением. Новых фактов не "
            "добавляешь — только из переданного; у каждого факта сноска [url]. Гипотезу помечаешь гипотезой.\n\n"
            + (src or FALLBACK))


def critic_system() -> str:
    s, _ = doc("critic")
    if s:
        return s.replace("аналитика по сделкам M&A", "аналитика по риску заказчика") + (
            "\nВ этом режиме «кандидат» — заказчик, а «довод против» — довод, что риск переоценён или недооценён.")
    return ("Ты — независимый критик. Найди в черновике: утверждения без источника, гипотезы, поданные как факт, "
            "отсутствующий довод против, вывод на правдоподобном звучании. Ответ — JSON "
            '{"verdict":"pushback|clean","counterview":"","weak_claims":[],"missing_source":[]}.')
