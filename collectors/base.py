"""Контракт коннектора: collect(inn) -> CollectResult. Ошибка источника -> ok=False, а не исключение."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Statement:
    prop: str
    value: object
    src_url: str
    src_class: str
    year: int | None = None
    quote: str | None = None
    published: str | None = None

    def as_dict(self):
        return asdict(self)


@dataclass
class Link:
    rel: str                   # учредитель | руководитель | управляющий | конкурсный_управляющий | дочерняя | материнская
    dir: str                   # in: other -> компания (other владеет/руководит); out: компания -> other
    other_name: str | None
    other_inn: str | None
    other_type: str | None     # ЮЛ | ФЛ | ИП
    src_url: str
    src_class: str
    share_pct: float | None = None
    other_status: str | None = None
    other_props: dict | None = None


@dataclass
class CollectResult:
    channel: str
    ok: bool
    inn: str | None = None
    page_inn: str | None = None        # ИНН, который реально стоит на странице источника
    name: str | None = None
    region: str | None = None
    entity_type: str | None = None
    statements: list[Statement] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    error: str | None = None
    extra: dict = field(default_factory=dict)   # служебное: токены, id в источнике

    @classmethod
    def fail(cls, channel, inn, error):
        return cls(channel=channel, ok=False, inn=inn, error=error)
