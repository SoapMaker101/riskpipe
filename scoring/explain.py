"""Пояснение карточки собирается шаблоном из флагов, без LLM."""
from __future__ import annotations

from scoring.score import Card

EMOJI = {"red": "🔴", "yellow": "🟡", "green": "🟢", "grey": "⚪"}
KIND_RU = {"fact": "факт", "fact2": "факт (агрегатор)", "hypothesis": "гипотеза"}


def flag_line(f) -> str:
    src = f" [{KIND_RU.get(f.kind, f.kind)}" + (f", {f.url}" if f.url else "") + "]"
    chk = f" — {f.check}" if getattr(f, "check", None) else ""
    return f"{f.text}{src}{chk}"


def explain(card: Card, resolve_note: str | None = None) -> str:
    lines = [f"{EMOJI[card.color]} {card.name}" + (f", ИНН {card.inn}" if card.inn else "")
             + (f" ({card.profile})" if card.profile != "коммерческое" else "")]
    reds = [f for f in card.flags if f.level == "red"]
    yel = [f for f in card.flags if f.level == "yellow"]
    if card.color == "grey":
        if not card.resolved:
            lines.append("Почему серый: компания не опознана однозначно" + (f" — {resolve_note}" if resolve_note else ""))
        else:
            lines.append(f"Почему серый: стоп-факторов не найдено, но не проверено: {', '.join(card.missing)} "
                         "— «не нашли» ещё не значит «чисто»")
    if reds:
        lines.append("Почему красный:")
        lines += [f"  — {flag_line(f)}" for f in reds]
    if yel:
        lines.append("Что настораживает:" if reds else "Почему жёлтый:")
        lines += [f"  — {flag_line(f)}" for f in yel]
    if card.color == "green":
        lines.append("Почему зелёный: стоп-факторов и настораживающих признаков не найдено "
                     f"(проверено {card.coverage:.0%} каналов"
                     + (f"; не проверено: {', '.join(card.missing)}" if card.missing else "") + ")")
    info = [f for f in card.flags if f.level == "info"]
    if info:
        lines.append("Для сведения:")
        lines += [f"  — {flag_line(f)}" for f in info]
    if card.resolved and resolve_note and "по названию" in resolve_note:
        lines.append(f"Опознание: {resolve_note}")
    if card.unavailable:
        lines.append("Не открылось: " + ", ".join(f"{k} ({v})" for k, v in card.unavailable.items()))
    lines.append(f"Решение: {card.action}")
    return "\n".join(lines)
