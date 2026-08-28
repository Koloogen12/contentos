"""Тесты проверок прогрева.

Главный класс случаев здесь — не «валидатор упал», а «валидатор уверенно
соврал». Поэтому большая часть тестов проверяет, что автоматическая
разметка НЕ засчитывается как покрытие.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.launch.methodology import CHECKPOINTS, TRIGGERS
from app.services.launch.validators import (
    ORIGIN_HUMAN,
    ORIGIN_LLM,
    ORIGIN_RULE,
    SEVERITY_CRITICAL,
    SlotView,
    build_report,
    check_bank_coverage,
    check_bridge,
    check_idea_reuse,
    check_missing_checkpoints,
    check_readiness,
    check_stage_order,
    check_unused_triggers,
)

D0 = date(2026, 10, 1)


def slot(day_offset=0, **kw):
    base = dict(
        day=D0 + timedelta(days=day_offset),
        stage=kw.pop("stage", 4),
        meaning=kw.pop("meaning", "expertise"),
        channel=kw.pop("channel", "stories"),
    )
    base.update(kw)
    return SlotView(**base)


def full_coverage_slots():
    """Слоты, закрывающие все смыслы и все рычаги, подтверждённые человеком."""
    out = []
    # мостик и результаты ставим в правильном порядке
    out.append(
        slot(0, checkpoints=("q3_bridge",), markup_origin=ORIGIN_HUMAN, has_proof=True)
    )
    for i, cp in enumerate(CHECKPOINTS):
        if cp.key == "q3_bridge":
            continue
        out.append(
            slot(
                i + 1,
                checkpoints=(cp.key,),
                triggers=tuple(t.key for t in TRIGGERS),
                markup_origin=ORIGIN_HUMAN,
                has_proof=True,
                idea_id=f"idea-{i}",
            )
        )
    return out


# --------------------------------------------------------------------------
# Непокрытые смыслы
# --------------------------------------------------------------------------


def test_empty_plan_reports_every_question_as_missing():
    findings = check_missing_checkpoints([])
    codes = {f.code for f in findings}
    assert codes == {f"checkpoint_missing_q{n}" for n in (1, 2, 3, 4)}


def test_full_confirmed_coverage_has_no_missing_findings():
    findings = check_missing_checkpoints(full_coverage_slots())
    assert findings == []


def test_rule_markup_does_not_count_as_coverage():
    """Ключевая защита: разметка правилами не закрывает смысл."""
    slots = [
        slot(i, checkpoints=(cp.key,), markup_origin=ORIGIN_RULE)
        for i, cp in enumerate(CHECKPOINTS)
    ]
    findings = check_missing_checkpoints(slots)
    assert findings, "автоматическая разметка не должна давать зелёный свет"
    assert any(f.code.startswith("checkpoint_unverified") for f in findings)


def test_llm_markup_also_does_not_count():
    slots = [
        slot(i, checkpoints=(cp.key,), markup_origin=ORIGIN_LLM)
        for i, cp in enumerate(CHECKPOINTS)
    ]
    findings = check_missing_checkpoints(slots)
    assert any(f.code.startswith("checkpoint_unverified") for f in findings)


def test_unverified_finding_is_flagged_as_not_verified():
    slots = [slot(0, checkpoints=("q1_results",), markup_origin=ORIGIN_RULE)]
    findings = check_missing_checkpoints(slots)
    unverified = [f for f in findings if f.code.startswith("checkpoint_unverified")]
    assert unverified and unverified[0].verified is False


def test_missing_finding_names_fix_days():
    slots = [
        slot(0, meaning="expertise", markup_origin=ORIGIN_HUMAN),
        slot(1, meaning="students", markup_origin=ORIGIN_HUMAN),
    ]
    findings = check_missing_checkpoints(slots)
    q2 = next(f for f in findings if f.code == "checkpoint_missing_q2")
    assert q2.fix_days, "находка обязана называть, в какие дни это чинится"


def test_message_has_no_percentage():
    findings = check_missing_checkpoints([])
    assert all("%" not in f.message for f in findings)


# --------------------------------------------------------------------------
# Рычаги
# --------------------------------------------------------------------------


def test_all_triggers_missing_on_empty_plan():
    findings = check_unused_triggers([])
    codes = {f.code for f in findings}
    assert "triggers_unused" in codes
    assert "triggers_cross_unused" in codes, "сквозные рычаги проверяются отдельно"


def test_cross_stage_triggers_reported_separately():
    """Сквозные рычаги в первоисточнике выпадали из проверки совсем."""
    staged = [t.key for t in TRIGGERS if not t.cross_stage]
    slots = [slot(0, triggers=tuple(staged), markup_origin=ORIGIN_HUMAN)]
    findings = check_unused_triggers(slots)
    codes = {f.code for f in findings}
    assert codes == {"triggers_cross_unused"}


def test_no_trigger_findings_when_all_used():
    slots = [
        slot(0, triggers=tuple(t.key for t in TRIGGERS), markup_origin=ORIGIN_HUMAN)
    ]
    assert check_unused_triggers(slots) == []


def test_unconfirmed_triggers_do_not_count():
    slots = [
        slot(0, triggers=tuple(t.key for t in TRIGGERS), markup_origin=ORIGIN_RULE)
    ]
    assert check_unused_triggers(slots), "невыверенные рычаги не идут в зачёт"


# --------------------------------------------------------------------------
# Порядок этапов
# --------------------------------------------------------------------------


def test_hype_on_early_stage_is_critical():
    findings = check_stage_order([slot(0, stage=2, meaning="hype")])
    assert findings and findings[0].severity == SEVERITY_CRITICAL


def test_hype_on_late_stage_is_fine():
    assert check_stage_order([slot(0, stage=6, meaning="hype")]) == []


def test_sales_meaning_early_is_flagged():
    assert check_stage_order([slot(0, stage=3, meaning="sales")])


# --------------------------------------------------------------------------
# Мостик
# --------------------------------------------------------------------------


def test_missing_bridge_is_critical():
    findings = check_bridge([slot(0, checkpoints=("q1_results",), markup_origin=ORIGIN_HUMAN)])
    assert findings and findings[0].code == "bridge_missing"
    assert findings[0].severity == SEVERITY_CRITICAL


def test_bridge_after_results_is_flagged():
    slots = [
        slot(0, checkpoints=("q1_results",), markup_origin=ORIGIN_HUMAN),
        slot(5, checkpoints=("q3_bridge",), markup_origin=ORIGIN_HUMAN),
    ]
    findings = check_bridge(slots)
    assert findings and findings[0].code == "bridge_too_late"


def test_bridge_before_results_passes():
    slots = [
        slot(0, checkpoints=("q3_bridge",), markup_origin=ORIGIN_HUMAN),
        slot(5, checkpoints=("q1_results",), markup_origin=ORIGIN_HUMAN),
    ]
    assert check_bridge(slots) == []


def test_unconfirmed_bridge_does_not_count():
    slots = [slot(0, checkpoints=("q3_bridge",), markup_origin=ORIGIN_RULE)]
    findings = check_bridge(slots)
    assert findings and findings[0].code == "bridge_missing"


# --------------------------------------------------------------------------
# Банк идей
# --------------------------------------------------------------------------


def test_bank_coverage_distinguishes_bank_from_assigned():
    """В банке идей много, но в план не поставлено ни одной."""
    slots = [slot(i, meaning="hype", stage=6) for i in range(5)]
    findings = check_bank_coverage(slots, {"hype": 100})
    assert findings, "полный банк не означает заполненный план"
    assert "поставлено 0" in findings[0].message
    assert "в банке 100" in findings[0].message


def test_bank_coverage_silent_when_all_assigned():
    slots = [slot(i, idea_id=f"i{i}") for i in range(3)]
    assert check_bank_coverage(slots, {"expertise": 3}) == []


def test_bank_coverage_counts_total_missing():
    slots = [slot(i, meaning="hype", stage=6) for i in range(4)]
    findings = check_bank_coverage(slots, {})
    assert findings[0].affected == 4


# --------------------------------------------------------------------------
# Переиспользование идей
# --------------------------------------------------------------------------


def test_same_idea_twice_is_flagged():
    slots = [slot(0, idea_id="x"), slot(1, idea_id="x")]
    findings = check_idea_reuse(slots)
    assert findings and findings[0].affected == 1


def test_distinct_ideas_pass():
    assert check_idea_reuse([slot(0, idea_id="a"), slot(1, idea_id="b")]) == []


# --------------------------------------------------------------------------
# Готовность продукта
# --------------------------------------------------------------------------


def test_unready_product_is_critical():
    findings = check_readiness({"program": True, "payments": False, "access": False})
    assert findings and findings[0].severity == SEVERITY_CRITICAL
    assert "приём оплаты" in findings[0].message


def test_ready_product_passes():
    assert check_readiness({"program": True, "payments": True}) == []


def test_readiness_absent_is_not_checked():
    assert check_readiness(None) == []


# --------------------------------------------------------------------------
# Отчёт целиком
# --------------------------------------------------------------------------


def test_empty_plan_is_not_ready():
    report = build_report([])
    assert not report.ready
    assert report.checkpoints_confirmed == 0
    assert report.checkpoints_total == len(CHECKPOINTS)


def test_full_plan_is_ready():
    report = build_report(
        full_coverage_slots(),
        bank_by_meaning={"expertise": 100},
        readiness={"program": True, "offer": True, "payments": True,
                   "access": True, "support": True},
    )
    assert report.ready, [f.code for f in report.findings]
    assert report.checkpoints_confirmed == len(CHECKPOINTS)
    assert report.triggers_used == len(TRIGGERS)


def test_claimed_and_confirmed_are_tracked_separately():
    slots = [slot(i, checkpoints=(cp.key,), markup_origin=ORIGIN_RULE)
             for i, cp in enumerate(CHECKPOINTS)]
    report = build_report(slots)
    assert report.checkpoints_claimed == len(CHECKPOINTS)
    assert report.checkpoints_confirmed == 0, "заявлено ≠ подтверждено"


def test_findings_sorted_critical_first():
    report = build_report([slot(0, stage=2, meaning="hype")])
    severities = [f.severity for f in report.sorted_findings()]
    assert severities[0] == SEVERITY_CRITICAL


def test_report_counts_slots_with_idea():
    report = build_report([slot(0, idea_id="a"), slot(1)])
    assert report.slots_total == 2
    assert report.slots_with_idea == 1
