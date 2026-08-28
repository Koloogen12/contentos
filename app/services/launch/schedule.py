"""Раскладка запуска: из одной даты — календарь по этапам и слоты по дням.

Здесь нет ни базы, ни моделей, ни ИИ — только арифметика дат и
распределение рубрик. Так сделано намеренно: это ядро продукта, оно должно
быть воспроизводимым и проверяемым построчно. Всё, что требует смысла, а не
счёта, живёт в других модулях.

Три вещи, которые здесь решены не так, как в первоисточнике:

* Ось продолжается за дату открытия продаж (этап 7). В уроках её нет.
* Слоты в прошлом не создаются никогда. Если до продаж осталось меньше
  дней, чем требует методология, план сжимается по явному приоритету и
  сообщает, что именно выброшено, — вместо тихого обрезания.
* Распределение рубрик всегда даёт ровно столько единиц, сколько слотов.
  В исходном инструменте независимое округление по каждой ячейке давало
  расхождение (115 слотов против 114 распределённых), и одна «единица без
  идеи» была чистым артефактом арифметики.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from app.services.launch.methodology import (
    LAST_DAY_MIX,
    MIX,
    STAGE_BY_NUM,
    TIMED_STAGES,
)

# Каналы публикации. Ключ — то, что уходит в planned_posts.platform.
CHANNEL_TELEGRAM = "telegram"
CHANNEL_STORIES = "stories"
CHANNEL_REELS = "reels"

#: Порядок, в котором этапы жертвуются при нехватке дней. Первым режется
#: мягкий прогрев: он длинный и наименее критичный. Окно продаж и ажиотаж
#: не режутся никогда — без них запуска нет.
SACRIFICE_ORDER: tuple[int, ...] = (2, 3, 4, 5)

#: Насколько плотно публикуемся. Блогу на 500 подписчиков сорок единиц
#: контента не нужны — он на них выгорит и не дойдёт до продаж.
INTENSITY_LIGHT = "light"
INTENSITY_NORMAL = "normal"
INTENSITY_HEAVY = "heavy"


@dataclass(frozen=True)
class CadenceRule:
    """Как часто выходим на канале.

    `weekdays=None` — каждый день. Иначе кортеж дней недели (0 = понедельник).
    `from_stage` — правило включается начиная с этого этапа.
    """

    channel: str
    weekdays: tuple[int, ...] | None = None
    from_stage: int = 1


#: Дефолтный каденс. Перенесён из разобранного инструмента: сторис каждый
#: день, рилс трижды в неделю до вброса идеи и четырежды после, длинный
#: пост дважды в неделю.
DEFAULT_CADENCE: dict[str, tuple[CadenceRule, ...]] = {
    INTENSITY_LIGHT: (
        CadenceRule(CHANNEL_STORIES, (0, 2, 4)),
        CadenceRule(CHANNEL_REELS, (2,)),
        CadenceRule(CHANNEL_TELEGRAM, (4,)),
    ),
    INTENSITY_NORMAL: (
        CadenceRule(CHANNEL_STORIES, None),
        CadenceRule(CHANNEL_REELS, (0, 2, 4), from_stage=1),
        CadenceRule(CHANNEL_REELS, (0, 1, 3, 4), from_stage=4),
        CadenceRule(CHANNEL_TELEGRAM, (1, 4)),
    ),
    INTENSITY_HEAVY: (
        CadenceRule(CHANNEL_STORIES, None),
        CadenceRule(CHANNEL_REELS, (0, 1, 2, 3, 4), from_stage=1),
        CadenceRule(CHANNEL_TELEGRAM, (0, 2, 4)),
    ),
}


class LaunchDatesError(ValueError):
    """Даты запуска противоречат друг другу."""


@dataclass(frozen=True)
class StageWindow:
    stage: int
    key: str
    title: str
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass
class SchedulePlan:
    """Результат развёртки: окна этапов и то, чем пришлось пожертвовать."""

    windows: list[StageWindow]
    dropped: list[str] = field(default_factory=list)
    compressed: list[str] = field(default_factory=list)

    @property
    def start(self) -> date:
        return min(w.start for w in self.windows)

    @property
    def end(self) -> date:
        return max(w.end for w in self.windows)

    def stage_of(self, day: date) -> int | None:
        for w in self.windows:
            if w.contains(day):
                return w.stage
        return None


def validate_dates(
    *,
    sales_open: date,
    sales_close: date | None = None,
    key_event: date | None = None,
    today: date | None = None,
) -> None:
    """Проверить взаимный порядок ключевых дат.

    Ошибки формулируются по-человечески: пользователь должен понять, что
    именно он поставил не так, а не увидеть «invalid date».
    """
    if sales_close is not None and sales_close < sales_open:
        raise LaunchDatesError(
            "Продажи закрываются раньше, чем открываются. "
            "Проверьте даты открытия и закрытия."
        )
    if key_event is not None and key_event > sales_open:
        raise LaunchDatesError(
            "Ключевое событие стоит позже открытия продаж. "
            "Вебинар или бесплатник должны быть до старта продаж."
        )
    if today is not None and sales_open < today:
        raise LaunchDatesError(
            "Дата открытия продаж уже в прошлом. "
            "Выберите будущую дату или перенесите запуск."
        )


def plan_windows(
    *,
    sales_open: date,
    sales_close: date | None = None,
    key_event: date | None = None,
    today: date | None = None,
    durations: dict[int, int] | None = None,
) -> SchedulePlan:
    """Развернуть календарь назад от даты открытия продаж.

    `today` задаёт левую границу: раньше сегодняшнего дня слоты не
    создаются. Если методология не влезает в оставшееся время, этапы
    сжимаются до минимума, а затем выбрасываются по `SACRIFICE_ORDER` —
    и каждое такое решение попадает в `dropped` / `compressed`, чтобы
    пользователь увидел цену спешки.
    """
    validate_dates(
        sales_open=sales_open,
        sales_close=sales_close,
        key_event=key_event,
        today=today,
    )

    wanted: dict[int, int] = {
        s.num: (durations or {}).get(s.num, s.default_days) for s in TIMED_STAGES
    }
    # Окно продаж считается от заданной даты закрытия, а не от дефолта.
    if sales_close is not None:
        wanted[7] = (sales_close - sales_open).days + 1
    # Ключевое событие, если задано, притягивает к себе этап 5.
    if key_event is not None:
        gap = (sales_open - key_event).days
        wanted[5] = max(STAGE_BY_NUM[5].min_days, wanted[5])
        # ажиотаж занимает промежуток между событием и продажами
        wanted[6] = max(0, min(STAGE_BY_NUM[6].max_days, gap - 1))

    dropped: list[str] = []
    compressed: list[str] = []

    # Сколько дней доступно слева от продаж.
    pre_stages = [s.num for s in TIMED_STAGES if s.num < 7]
    if today is not None:
        available = (sales_open - today).days
        if available < 0:
            available = 0
        need = sum(wanted[n] for n in pre_stages)
        if need > available:
            # 1. Сжимаем до минимумов.
            for num in SACRIFICE_ORDER:
                if need <= available:
                    break
                floor = STAGE_BY_NUM[num].min_days
                if wanted[num] > floor:
                    delta = min(wanted[num] - floor, need - available)
                    wanted[num] -= delta
                    need -= delta
                    compressed.append(
                        f"{STAGE_BY_NUM[num].title}: сжат до {wanted[num]} дн."
                    )
            # 2. Если и минимумы не влезают — выбрасываем этапы целиком.
            for num in SACRIFICE_ORDER:
                if need <= available:
                    break
                if wanted[num] > 0:
                    need -= wanted[num]
                    dropped.append(
                        f"{STAGE_BY_NUM[num].title} — выброшен: до продаж "
                        f"осталось {available} дн."
                    )
                    wanted[num] = 0

    windows: list[StageWindow] = []

    # Этап 7 — окно продаж, единственный, что идёт вправо от опорной даты.
    close = sales_close or (sales_open + timedelta(days=wanted[7] - 1))
    windows.append(
        StageWindow(7, STAGE_BY_NUM[7].key, STAGE_BY_NUM[7].title, sales_open, close)
    )

    # Остальные разворачиваются влево, встык.
    cursor = sales_open - timedelta(days=1)
    for num in (6, 5, 4, 3, 2):
        length = wanted[num]
        if length <= 0:
            continue
        end = cursor
        start = end - timedelta(days=length - 1)
        if today is not None and start < today:
            start = today
            if start > end:
                continue
        windows.append(
            StageWindow(num, STAGE_BY_NUM[num].key, STAGE_BY_NUM[num].title, start, end)
        )
        cursor = start - timedelta(days=1)

    windows.sort(key=lambda w: w.start)
    return SchedulePlan(windows=windows, dropped=dropped, compressed=compressed)


@dataclass(frozen=True)
class Slot:
    """Пустое место в календаре: день, канал и рубрика."""

    day: date
    channel: str
    stage: int
    meaning: str
    is_last_day: bool = False


def _channels_for_day(
    day: date, stage: int, cadence: tuple[CadenceRule, ...]
) -> list[str]:
    """Какие каналы выходят в этот день.

    Правила с большим `from_stage` перекрывают более ранние для того же
    канала: у рилсов до вброса идеи одна частота, после — другая.
    """
    chosen: dict[str, CadenceRule] = {}
    for rule in cadence:
        if rule.from_stage > stage:
            continue
        current = chosen.get(rule.channel)
        if current is None or rule.from_stage >= current.from_stage:
            chosen[rule.channel] = rule
    out: list[str] = []
    for channel, rule in chosen.items():
        if rule.weekdays is None or day.weekday() in rule.weekdays:
            out.append(channel)
    # Стабильный порядок, чтобы раскладка была воспроизводимой.
    order = {CHANNEL_STORIES: 0, CHANNEL_REELS: 1, CHANNEL_TELEGRAM: 2}
    out.sort(key=lambda c: order.get(c, 99))
    return out


def allocate_meanings(stage: int, count: int, *, last_day: bool = False) -> list[str]:
    """Разложить `count` слотов этапа по целевым долям рубрик.

    Возвращает ровно `count` элементов — это важно: расхождение между
    числом слотов и числом распределённых рубрик рождает фантомный дефицит.

    Одинаковые рубрики не идут подряд: раскладываем по кругу, начиная с
    самой частой. Без этого получается пять дней подряд про учеников.
    """
    if count <= 0:
        return []
    mix = LAST_DAY_MIX if last_day else MIX.get(stage) or MIX[2]

    # Каждой рубрике из микса — минимум один слот, если слотов хватает.
    items = sorted(mix.items(), key=lambda kv: (-kv[1], kv[0]))
    quota: dict[str, int] = {}
    for key, share in items:
        quota[key] = max(1, round(count * share)) if count >= len(items) else 0

    if count < len(items):
        # Слотов меньше, чем рубрик: берём самые весомые.
        for key, _ in items[:count]:
            quota[key] = 1

    # Приводим сумму ровно к count, добирая или срезая с самой крупной доли.
    diff = count - sum(quota.values())
    idx = 0
    while diff != 0 and items:
        key = items[idx % len(items)][0]
        if diff > 0:
            quota[key] += 1
            diff -= 1
        elif quota.get(key, 0) > 0:
            quota[key] -= 1
            diff += 1
        idx += 1

    buckets = [[key] * n for key, n in quota.items() if n > 0]
    out: list[str] = []
    while any(buckets):
        buckets.sort(key=len, reverse=True)
        for bucket in buckets:
            if bucket:
                out.append(bucket.pop())
    return out[:count]


def build_slots(
    plan: SchedulePlan,
    *,
    intensity: str = INTENSITY_NORMAL,
    cadence: tuple[CadenceRule, ...] | None = None,
) -> list[Slot]:
    """Собрать пустые слоты по всему окну запуска.

    Рубрики назначаются поэтапно: внутри каждого этапа своё распределение,
    поэтому «ажиотаж» не может протечь в мягкий прогрев.
    """
    rules = cadence or DEFAULT_CADENCE.get(intensity, DEFAULT_CADENCE[INTENSITY_NORMAL])
    last_day = plan.end

    by_stage: dict[int, list[tuple[date, str]]] = {}
    for window in plan.windows:
        day = window.start
        while day <= window.end:
            for channel in _channels_for_day(day, window.stage, rules):
                by_stage.setdefault(window.stage, []).append((day, channel))
            day += timedelta(days=1)

    slots: list[Slot] = []
    for stage, pairs in sorted(by_stage.items()):
        # Последний день окна продаж живёт по своим правилам.
        regular = [p for p in pairs if not (stage == 7 and p[0] == last_day)]
        closing = [p for p in pairs if stage == 7 and p[0] == last_day]

        for (day, channel), meaning in zip(regular, allocate_meanings(stage, len(regular))):
            slots.append(Slot(day=day, channel=channel, stage=stage, meaning=meaning))
        for (day, channel), meaning in zip(
            closing, allocate_meanings(stage, len(closing), last_day=True)
        ):
            slots.append(
                Slot(day=day, channel=channel, stage=stage, meaning=meaning, is_last_day=True)
            )

    slots.sort(key=lambda s: (s.day, s.channel))
    return slots


def build_schedule(
    *,
    sales_open: date,
    sales_close: date | None = None,
    key_event: date | None = None,
    today: date | None = None,
    intensity: str = INTENSITY_NORMAL,
    durations: dict[int, int] | None = None,
) -> tuple[SchedulePlan, list[Slot]]:
    """Полная развёртка: окна этапов плюс слоты."""
    plan = plan_windows(
        sales_open=sales_open,
        sales_close=sales_close,
        key_event=key_event,
        today=today,
        durations=durations,
    )
    return plan, build_slots(plan, intensity=intensity)
