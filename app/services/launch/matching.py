"""Подбор идей под слоты и черновая разметка банка.

В инструменте, с которого мы начинали, подбор идеи под слот был написан
наполовину и выключен флагом: функция объявлена и ни разу не вызвана, все
сто с лишним назначений делались руками. Это и есть разница между красивой
сеткой и планом, по которому можно снимать.

Разметка здесь черновая и честно об этом говорит. Правила по ключевым
словам ошибаются — в разобранном банке из-за них половина идей уехала в
«экспертизу», а «ажиотаж» остался с четырьмя записями, и весь дефицит
плана оказался артефактом регулярки, а не реальной нехваткой материала.
Поэтому у каждой метки есть источник, и проверки засчитывают только то,
что подтвердил человек.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.launch.methodology import (
    CHECKPOINTS,
    MEANINGS,
    MEANING_BY_KEY,
    TRIGGERS,
)
from app.services.launch.validators import ORIGIN_RULE

#: Канал слота → форматы, которые в него ложатся.
CHANNEL_FORMATS: dict[str, frozenset[str]] = {
    "stories": frozenset({"stories", "any"}),
    "reels": frozenset({"reels", "any"}),
    "telegram": frozenset({"post", "any"}),
}


@dataclass(frozen=True)
class IdeaView:
    """Идея банка в том виде, в каком её видит подбор."""

    id: str
    title: str
    body: str = ""
    meaning: str | None = None
    #: формат, под который идея написана: stories / reels / post / any
    fmt: str = "any"
    score: int | None = None
    used_at: str | None = None
    markup_origin: str = ORIGIN_RULE

    @property
    def text(self) -> str:
        return f"{self.title} {self.body}".lower()


@dataclass
class Assignment:
    slot_index: int
    idea_id: str | None
    #: почему не нашлось — показывается пользователю дословно
    reason: str = ""


def _words(*groups: tuple[str, ...]) -> re.Pattern[str]:
    parts = [re.escape(w) for group in groups for w in group]
    return re.compile("|".join(parts), re.IGNORECASE) if parts else re.compile(r"(?!x)x")


_MEANING_PATTERNS: dict[str, re.Pattern[str]] = {}
for _m in MEANINGS:
    _kw = tuple(
        t.keywords for t in TRIGGERS if t.key == _m.trigger_key
    )
    _MEANING_PATTERNS[_m.key] = _words(*_kw)

_CHECKPOINT_PATTERNS: dict[str, re.Pattern[str]] = {
    c.key: _words(c.keywords) for c in CHECKPOINTS
}
_TRIGGER_PATTERNS: dict[str, re.Pattern[str]] = {
    t.key: _words(t.keywords) for t in TRIGGERS
}


def guess_meaning(text: str) -> str | None:
    """Черновая рубрика идеи по ключевым словам.

    Возвращает None, когда уверенности нет вообще — это лучше, чем свалить
    всё в самую широкую рубрику и получить фантомное покрытие.
    """
    low = (text or "").lower()
    best: tuple[int, str] | None = None
    for key, pattern in _MEANING_PATTERNS.items():
        hits = len(pattern.findall(low))
        if hits and (best is None or hits > best[0]):
            best = (hits, key)
    return best[1] if best else None


def guess_checkpoints(text: str) -> tuple[str, ...]:
    """Какие смыслы-галочки идея потенциально закрывает."""
    low = (text or "").lower()
    return tuple(
        key for key, pattern in _CHECKPOINT_PATTERNS.items() if pattern.search(low)
    )


def guess_triggers(text: str) -> tuple[str, ...]:
    low = (text or "").lower()
    return tuple(
        key for key, pattern in _TRIGGER_PATTERNS.items() if pattern.search(low)
    )


@dataclass
class MarkupDraft:
    meaning: str | None
    checkpoints: tuple[str, ...] = field(default_factory=tuple)
    triggers: tuple[str, ...] = field(default_factory=tuple)
    origin: str = ORIGIN_RULE


def draft_markup(idea: IdeaView) -> MarkupDraft:
    """Разметить идею правилами. Результат — черновик, не факт."""
    text = idea.text
    return MarkupDraft(
        meaning=idea.meaning or guess_meaning(text),
        checkpoints=guess_checkpoints(text),
        triggers=guess_triggers(text),
        origin=ORIGIN_RULE,
    )


def _fits_channel(idea: IdeaView, channel: str) -> bool:
    allowed = CHANNEL_FORMATS.get(channel)
    return True if allowed is None else idea.fmt in allowed


def assign_ideas(
    slot_specs: list[tuple[str, str]],
    ideas: list[IdeaView],
    *,
    exclude_ids: frozenset[str] = frozenset(),
) -> list[Assignment]:
    """Разложить идеи по слотам.

    `slot_specs` — список пар (рубрика, канал) в порядке слотов.

    Правила, которые здесь соблюдаются буквально:

    * одна идея занимает не более одного слота — повтор в пределах запуска
      аудитория считывает как «ему нечего сказать»;
    * формат идеи должен ложиться в канал слота — выпуск на YouTube нельзя
      поставить в сторис;
    * выше оценённые идут первыми;
    * если под рубрику ничего нет, слот остаётся пустым **с причиной**, а не
      затыкается чем попало.
    """
    pool: dict[str, list[IdeaView]] = {}
    for idea in ideas:
        if idea.id in exclude_ids or not idea.meaning:
            continue
        pool.setdefault(idea.meaning, []).append(idea)
    for bucket in pool.values():
        # Сначала неиспользованные, потом по убыванию оценки, потом стабильно по id.
        bucket.sort(key=lambda i: (i.used_at is not None, -(i.score or 0), i.id))

    taken: set[str] = set()
    out: list[Assignment] = []
    for index, (meaning, channel) in enumerate(slot_specs):
        bucket = pool.get(meaning) or []
        chosen: IdeaView | None = None
        blocked_by_format = False
        for idea in bucket:
            if idea.id in taken:
                continue
            if not _fits_channel(idea, channel):
                blocked_by_format = True
                continue
            chosen = idea
            break

        if chosen is None:
            title = MEANING_BY_KEY[meaning].title if meaning in MEANING_BY_KEY else meaning
            if not bucket:
                reason = f"в банке нет идей рубрики «{title}»"
            elif blocked_by_format:
                reason = f"идеи рубрики «{title}» есть, но не подходят под этот формат"
            else:
                reason = f"идеи рубрики «{title}» закончились — все уже стоят в плане"
            out.append(Assignment(slot_index=index, idea_id=None, reason=reason))
            continue

        taken.add(chosen.id)
        out.append(Assignment(slot_index=index, idea_id=chosen.id))
    return out


def bank_histogram(ideas: list[IdeaView]) -> dict[str, int]:
    """Сколько идей лежит в банке по каждой рубрике."""
    out: dict[str, int] = {}
    for idea in ideas:
        if idea.meaning:
            out[idea.meaning] = out.get(idea.meaning, 0) + 1
    return out
