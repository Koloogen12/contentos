"""Тесты ядра раскладки запуска.

Ядро не ходит в базу и не зовёт модель, поэтому проверяется целиком и
быстро. Здесь же зафиксированы граничные случаи, на которых сломался
разобранный инструмент-первоисточник: расхождение числа слотов и рубрик,
слоты в прошлом, схлопывание коротких этапов.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.launch.methodology import MEANING_BY_KEY, MIX, STAGE_BY_NUM
from app.services.launch.schedule import (
    CHANNEL_REELS,
    CHANNEL_STORIES,
    CHANNEL_TELEGRAM,
    INTENSITY_HEAVY,
    INTENSITY_LIGHT,
    LaunchDatesError,
    allocate_meanings,
    build_schedule,
    build_slots,
    plan_windows,
    validate_dates,
)

TODAY = date(2026, 9, 1)
SALES_OPEN = date(2026, 10, 15)
SALES_CLOSE = date(2026, 10, 21)


# --------------------------------------------------------------------------
# Валидация дат
# --------------------------------------------------------------------------


def test_close_before_open_rejected():
    with pytest.raises(LaunchDatesError, match="раньше"):
        validate_dates(sales_open=SALES_OPEN, sales_close=SALES_OPEN - timedelta(days=1))


def test_key_event_after_sales_rejected():
    with pytest.raises(LaunchDatesError, match="ключевое|Ключевое"):
        validate_dates(sales_open=SALES_OPEN, key_event=SALES_OPEN + timedelta(days=1))


def test_sales_in_the_past_rejected():
    with pytest.raises(LaunchDatesError, match="прошлом"):
        validate_dates(sales_open=TODAY - timedelta(days=1), today=TODAY)


def test_valid_dates_pass():
    validate_dates(
        sales_open=SALES_OPEN,
        sales_close=SALES_CLOSE,
        key_event=SALES_OPEN - timedelta(days=5),
        today=TODAY,
    )


# --------------------------------------------------------------------------
# Развёртка окон
# --------------------------------------------------------------------------


def test_windows_are_ordered_and_contiguous():
    plan = plan_windows(sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY)
    assert plan.windows, "должно быть хотя бы одно окно"
    for prev, nxt in zip(plan.windows, plan.windows[1:]):
        assert prev.end < nxt.start, "окна не должны пересекаться"
        assert (nxt.start - prev.end).days == 1, "между этапами не должно быть дыр"


def test_sales_window_is_last_and_matches_dates():
    plan = plan_windows(sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY)
    sales = [w for w in plan.windows if w.stage == 7]
    assert len(sales) == 1
    assert sales[0].start == SALES_OPEN
    assert sales[0].end == SALES_CLOSE
    assert plan.end == SALES_CLOSE, "ось продолжается за открытие продаж"


def test_sales_window_defaults_when_close_missing():
    plan = plan_windows(sales_open=SALES_OPEN, today=TODAY)
    sales = next(w for w in plan.windows if w.stage == 7)
    assert sales.days == STAGE_BY_NUM[7].default_days


def test_nothing_is_scheduled_before_today():
    plan = plan_windows(sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY)
    assert plan.start >= TODAY


def test_stage_of_covers_every_day_in_window():
    plan = plan_windows(sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY)
    day = plan.start
    while day <= plan.end:
        assert plan.stage_of(day) is not None, f"день {day} вне этапов"
        day += timedelta(days=1)


def test_key_event_pulls_hype_between_event_and_sales():
    key_event = SALES_OPEN - timedelta(days=3)
    plan = plan_windows(
        sales_open=SALES_OPEN, sales_close=SALES_CLOSE, key_event=key_event, today=TODAY
    )
    hype = [w for w in plan.windows if w.stage == 6]
    assert hype, "ажиотаж должен существовать при заданном событии"
    assert hype[0].end < SALES_OPEN


def test_launch_without_key_event_still_builds():
    # Запуск без вебинара и бесплатника — валидный сценарий.
    plan = plan_windows(sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY)
    assert plan.stage_of(SALES_OPEN) == 7


# --------------------------------------------------------------------------
# Сжатие при нехватке времени
# --------------------------------------------------------------------------


def test_short_runway_compresses_and_reports():
    # До продаж всего 12 дней — методология целиком не влезает.
    plan = plan_windows(
        sales_open=TODAY + timedelta(days=12), sales_close=None, today=TODAY
    )
    assert plan.start >= TODAY
    assert plan.compressed or plan.dropped, "сжатие должно быть объяснено"


def test_very_short_runway_drops_stages_explicitly():
    plan = plan_windows(sales_open=TODAY + timedelta(days=3), today=TODAY)
    assert plan.start >= TODAY
    assert plan.dropped, "при трёх днях часть этапов обязана быть выброшена явно"
    # Окно продаж остаётся всегда.
    assert any(w.stage == 7 for w in plan.windows)


def test_sales_window_never_sacrificed():
    plan = plan_windows(sales_open=TODAY + timedelta(days=1), today=TODAY)
    assert any(w.stage == 7 for w in plan.windows)


# --------------------------------------------------------------------------
# Распределение рубрик
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stage", sorted(MIX.keys()))
@pytest.mark.parametrize("count", [0, 1, 2, 3, 7, 12, 40, 137])
def test_allocation_returns_exactly_count(stage, count):
    got = allocate_meanings(stage, count)
    assert len(got) == count, "число рубрик обязано совпадать с числом слотов"


@pytest.mark.parametrize("stage", sorted(MIX.keys()))
def test_allocation_uses_only_known_meanings(stage):
    for key in allocate_meanings(stage, 20):
        assert key in MEANING_BY_KEY, f"неизвестная рубрика {key}"
        assert key in MIX[stage], "рубрика не из микса этапа"


def test_allocation_avoids_repeating_same_meaning_back_to_back():
    got = allocate_meanings(4, 24)
    repeats = sum(1 for a, b in zip(got, got[1:]) if a == b)
    assert repeats == 0, f"одинаковые рубрики подряд: {got}"


def test_allocation_respects_proportions_roughly():
    got = allocate_meanings(6, 20)  # ажиотаж 70 / ученики 30
    hype = got.count("hype")
    assert 12 <= hype <= 16, f"доля ажиотажа выбилась из ожидания: {hype}/20"


def test_allocation_with_fewer_slots_than_meanings():
    got = allocate_meanings(4, 2)
    assert len(got) == 2
    assert len(set(got)) == 2, "при двух слотах берём две разные рубрики"


def test_last_day_mix_is_closing_only():
    got = allocate_meanings(7, 4, last_day=True)
    assert set(got) <= {"sales", "hype"}, "в последний день только дефицит и продажа"


# --------------------------------------------------------------------------
# Слоты
# --------------------------------------------------------------------------


def test_slots_cover_every_day_of_the_window():
    plan, slots = build_schedule(
        sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY
    )
    days_with_slots = {s.day for s in slots}
    day = plan.start
    while day <= plan.end:
        assert day in days_with_slots, f"день {day} остался без единиц контента"
        day += timedelta(days=1)


def test_slot_meaning_belongs_to_its_stage_mix():
    plan, slots = build_schedule(
        sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY
    )
    for slot in slots:
        allowed = set(MIX.get(slot.stage, {}))
        if slot.is_last_day:
            allowed |= {"sales", "hype"}
        assert slot.meaning in allowed, (
            f"рубрика {slot.meaning} протекла в этап {slot.stage}"
        )


def test_no_slot_before_today():
    _, slots = build_schedule(sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY)
    assert all(s.day >= TODAY for s in slots)


def test_last_day_slots_are_marked():
    plan, slots = build_schedule(
        sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY
    )
    closing = [s for s in slots if s.is_last_day]
    assert closing, "последний день продаж должен быть размечен отдельно"
    assert all(s.day == plan.end for s in closing)


def test_reels_cadence_widens_after_idea_drop():
    plan, slots = build_schedule(
        sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY
    )
    early = {s.day for s in slots if s.channel == CHANNEL_REELS and s.stage <= 2}
    late = {s.day for s in slots if s.channel == CHANNEL_REELS and s.stage >= 4}
    # На поздних этапах рилсы выходят чаще: 4 дня в неделю против 3.
    early_rate = len(early) / max(1, len({s.day for s in slots if s.stage <= 2}))
    late_rate = len(late) / max(1, len({s.day for s in slots if s.stage >= 4}))
    assert late_rate >= early_rate, "после вброса идеи частота рилсов не должна падать"


def test_light_intensity_produces_fewer_slots_than_heavy():
    _, light = build_schedule(
        sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY,
        intensity=INTENSITY_LIGHT,
    )
    _, heavy = build_schedule(
        sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY,
        intensity=INTENSITY_HEAVY,
    )
    assert len(light) < len(heavy), "блогу поменьше — план полегче"


def test_channels_are_known():
    _, slots = build_schedule(sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY)
    assert {s.channel for s in slots} <= {
        CHANNEL_STORIES, CHANNEL_REELS, CHANNEL_TELEGRAM
    }


def test_schedule_is_deterministic():
    a = build_schedule(sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY)[1]
    b = build_schedule(sales_open=SALES_OPEN, sales_close=SALES_CLOSE, today=TODAY)[1]
    assert a == b, "одинаковый вход обязан давать одинаковый план"
