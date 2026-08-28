"""Проверки прогрева: что пропущено и чем это грозит.

Это главная часть модуля. План умеет строить кто угодно; сказать «ты ни
разу не показал, что у тебя не получалось, и аудитория не поверит, что
получится у неё» — не умеет никто.

Два правила, которым здесь подчинено всё:

1. **Находка называет последствие, а не процент.** «Покрытие 87%» не
   говорит человеку ничего. «Не закрыт смысл „получится у меня“ — люди
   решат, что это не для них» говорит.

2. **Автоматическая разметка не засчитывается в покрытие.** Разметка идей
   правилами и моделью — черновик. Если считать её подтверждённой, продукт
   начнёт уверенно врать: скажет «всё закрыто», человек пойдёт в запуск и
   узнает правду по провалу конверсии через месяц. Поэтому у покрытия две
   шкалы: заявленное и подтверждённое, и зелёный горит только по второй.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.services.launch.methodology import (
    CHECKPOINT_BY_KEY,
    CHECKPOINTS,
    CROSS_STAGE_TRIGGERS,
    MEANING_BY_KEY,
    QUESTIONS,
    TRIGGER_BY_KEY,
    TRIGGERS,
)

# Кто проставил разметку. От этого зависит, идёт ли она в зачёт.
ORIGIN_RULE = "rule"
ORIGIN_LLM = "llm"
ORIGIN_HUMAN = "human"
CONFIRMED_ORIGINS = frozenset({ORIGIN_HUMAN})

SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"

_SEVERITY_ORDER = {SEVERITY_CRITICAL: 0, SEVERITY_HIGH: 1, SEVERITY_MEDIUM: 2}


@dataclass(frozen=True)
class SlotView:
    """Слот в том виде, в каком его видит проверка.

    Намеренно не ORM: проверки не ходят в базу, поэтому их можно гонять
    на выдуманных данных и в тестах.
    """

    day: date
    stage: int
    meaning: str
    channel: str
    idea_id: str | None = None
    #: какие смыслы-галочки закрывает эта единица
    checkpoints: tuple[str, ...] = field(default_factory=tuple)
    triggers: tuple[str, ...] = field(default_factory=tuple)
    #: кто проставил разметку: rule / llm / human
    markup_origin: str = ORIGIN_RULE
    #: есть ли под утверждением событие, кейс или цифра
    has_proof: bool = False
    is_published: bool = False

    @property
    def confirmed(self) -> bool:
        return self.markup_origin in CONFIRMED_ORIGINS


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    #: что пропущено и чем это грозит — человеческим языком
    message: str
    #: в какие дни это чинится
    fix_days: tuple[date, ...] = field(default_factory=tuple)
    #: сколько единиц затронуто
    affected: int = 0
    #: подтверждена ли находка выверенными данными
    verified: bool = True


@dataclass
class Report:
    findings: list[Finding]
    #: закрыто человеком / заявлено разметкой / всего
    checkpoints_confirmed: int = 0
    checkpoints_claimed: int = 0
    checkpoints_total: int = 0
    triggers_used: int = 0
    triggers_total: int = 0
    slots_total: int = 0
    slots_with_idea: int = 0
    #: Идей в банке всего / из них с проставленной рубрикой.
    #: Нужно интерфейсу: без этого первый шаг сценария нечем показать.
    bank_total: int = 0
    bank_marked: int = 0

    @property
    def ready(self) -> bool:
        """Готов ли прогрев к запуску: нет критичных находок."""
        return not any(f.severity == SEVERITY_CRITICAL for f in self.findings)

    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.code),
        )


def _days_for_question(slots: list[SlotView], question: int) -> tuple[date, ...]:
    """Дни, в которые уместно закрыть смысл этого вопроса.

    Смотрим на рубрику слота: у каждой рубрики есть свой вопрос покупателя.
    Так подсказка получается конкретной — не «добавьте контент», а «вот
    эти три дня уже про экспертность, поставьте туда».
    """
    out = [
        s.day
        for s in slots
        if not s.is_published
        and MEANING_BY_KEY.get(s.meaning)
        and MEANING_BY_KEY[s.meaning].question == question
    ]
    return tuple(sorted(set(out))[:5])


def check_missing_checkpoints(slots: list[SlotView]) -> list[Finding]:
    """Смыслы, которые прогрев обязан поставить, но не ставит.

    Самая дорогая ошибка первоисточника: «потеряли огромную часть продаж
    людей, которые подумали, что это сложно для них, хотя это было не так».
    """
    confirmed: set[str] = set()
    claimed: set[str] = set()
    for slot in slots:
        for key in slot.checkpoints:
            claimed.add(key)
            if slot.confirmed:
                confirmed.add(key)

    findings: list[Finding] = []
    for question in QUESTIONS:
        missing = [
            c for c in CHECKPOINTS
            if c.question == question.num and c.key not in confirmed
        ]
        if not missing:
            continue
        # Разделяем «нет вообще» и «есть, но не выверено» — это разные
        # разговоры с пользователем.
        absent = [c for c in missing if c.key not in claimed]
        unverified = [c for c in missing if c.key in claimed]

        if absent:
            findings.append(
                Finding(
                    code=f"checkpoint_missing_q{question.num}",
                    severity=SEVERITY_CRITICAL if question.num in (1, 3) else SEVERITY_HIGH,
                    title=f"Не закрыт вопрос «{question.title}»",
                    message=(
                        "Ни одна единица прогрева не закрывает: "
                        + ", ".join(c.title.lower() for c in absent[:4])
                        + (" и ещё несколько" if len(absent) > 4 else "")
                        + ". "
                        + question.why
                    ),
                    fix_days=_days_for_question(slots, question.num),
                    affected=len(absent),
                )
            )
        if unverified:
            findings.append(
                Finding(
                    code=f"checkpoint_unverified_q{question.num}",
                    severity=SEVERITY_MEDIUM,
                    title=f"Не проверено вручную: «{question.title}»",
                    message=(
                        f"{len(unverified)} смыслов размечены автоматически и "
                        "не подтверждены человеком. Пока вы их не подтвердите, "
                        "проверка не считает их закрытыми — автоматическая "
                        "разметка ошибается и уверенное «всё готово» дороже "
                        "честного «не проверено»."
                    ),
                    affected=len(unverified),
                    verified=False,
                )
            )
    return findings


def check_unused_triggers(slots: list[SlotView]) -> list[Finding]:
    """Психологические рычаги, не задействованные ни разу.

    Сквозные триггеры вынесены отдельно: в первоисточнике семь из
    восемнадцати не привязаны ни к одному этапу и потому выпадали из
    проверки целиком.
    """
    used: set[str] = set()
    for slot in slots:
        if slot.confirmed:
            used.update(slot.triggers)

    missing = [t for t in TRIGGERS if t.key not in used]
    if not missing:
        return []

    cross = [t for t in missing if t.cross_stage]
    staged = [t for t in missing if not t.cross_stage]

    findings: list[Finding] = []
    if staged:
        findings.append(
            Finding(
                code="triggers_unused",
                severity=SEVERITY_HIGH,
                title="Не задействованы рычаги на своих этапах",
                message=(
                    "Ни разу не использованы: "
                    + ", ".join(t.title.lower() for t in staged[:5])
                    + ". Нельзя перепрыгнуть пропасть на 99%: не дожали "
                    "рычагом — человек уходит думать на следующий поток."
                ),
                affected=len(staged),
            )
        )
    if cross:
        findings.append(
            Finding(
                code="triggers_cross_unused",
                severity=SEVERITY_MEDIUM,
                title="Не задействованы сквозные рычаги",
                message=(
                    "Работают на всём прогреве, а не на одном этапе, и "
                    "поэтому теряются чаще других: "
                    + ", ".join(t.title.lower() for t in cross[:5])
                    + ". Гарантия и реалистичность снимают страх потерять "
                    "деньги — без них дожим упирается в недоверие."
                ),
                affected=len(cross),
            )
        )
    return findings


def check_stage_order(slots: list[SlotView]) -> list[Finding]:
    """Ажиотаж и продажи раньше, чем раскрыты тема и эксперт.

    Прямое правило методологии: нельзя создавать ажиотаж до того, как
    прошёл фоновый контент и раскрыта тема.
    """
    early = [
        s for s in slots
        if s.meaning in ("hype", "sales") and s.stage <= 3
    ]
    if not early:
        return []
    return [
        Finding(
            code="stage_order_violated",
            severity=SEVERITY_CRITICAL,
            title="Ажиотаж раньше, чем раскрыта тема",
            message=(
                f"{len(early)} единиц про дефицит и продажу стоят на ранних "
                "этапах. Аудитория узнает цену раньше, чем поймёт, зачем ей "
                "это. Перенесите их ближе к продажам."
            ),
            fix_days=tuple(sorted({s.day for s in early})[:5]),
            affected=len(early),
        )
    ]


def check_bridge(slots: list[SlotView]) -> list[Finding]:
    """Мостик: история старта раньше объявления результатов.

    Без него сильные цифры не вдохновляют, а отталкивают — «он не верит,
    что сможет так же».
    """
    ordered = sorted(slots, key=lambda s: s.day)
    bridge_day: date | None = None
    results_day: date | None = None
    for slot in ordered:
        if not slot.confirmed:
            continue
        if bridge_day is None and "q3_bridge" in slot.checkpoints:
            bridge_day = slot.day
        if results_day is None and (
            "q1_results" in slot.checkpoints or "q2_credentials" in slot.checkpoints
        ):
            results_day = slot.day

    if bridge_day is None:
        return [
            Finding(
                code="bridge_missing",
                severity=SEVERITY_CRITICAL,
                title="Нет мостика между вами и аудиторией",
                message=(
                    "В прогреве ни разу не показано, с чего вы начинали и что "
                    "у вас не получалось. Аудитория видит только вершину и "
                    "решает, что у неё так не выйдет. Нужна подробная история "
                    "старта из той точки, где сейчас находится зритель."
                ),
                fix_days=_days_for_question(slots, 3),
            )
        ]
    if results_day is not None and bridge_day > results_day:
        return [
            Finding(
                code="bridge_too_late",
                severity=SEVERITY_HIGH,
                title="Мостик стоит позже результатов",
                message=(
                    f"Результаты объявлены {results_day:%d.%m}, а история "
                    f"старта только {bridge_day:%d.%m}. К моменту, когда вы "
                    "объясняете, что были на месте зрителя, он уже решил, "
                    "что это не про него."
                ),
                fix_days=(bridge_day,),
            )
        ]
    return []


def check_proof(slots: list[SlotView]) -> list[Finding]:
    """Смысл заявлен словами, но не показан событием.

    Шкала убедительности методологии: слова внизу, события и факты наверху.
    """
    unproven = [
        s for s in slots
        if s.checkpoints and not s.has_proof and not s.is_published
    ]
    if len(unproven) < 3:
        return []
    return [
        Finding(
            code="telling_not_showing",
            severity=SEVERITY_HIGH,
            title="Смыслы заявлены, но не показаны",
            message=(
                f"{len(unproven)} единиц несут смысл, под которым нет события, "
                "кейса или цифры. Слова — самый неубедительный вид "
                "доказательства: под каждый смысл нужен ответ на вопрос "
                "«как это показать»."
            ),
            fix_days=tuple(sorted({s.day for s in unproven})[:5]),
            affected=len(unproven),
        )
    ]


def check_bank_coverage(
    slots: list[SlotView], bank_by_meaning: dict[str, int]
) -> list[Finding]:
    """Банк идей не закрывает план.

    Здесь две разные величины, которые первоисточник смешивал: сколько
    идей лежит в банке и сколько реально поставлено в план. Инструмент,
    который мы разбирали, сверял спрос с размером банка и показывал
    «идея нашлась для 106», хотя в план не была поставлена ни одна.
    """
    demand: dict[str, int] = {}
    assigned: dict[str, int] = {}
    for slot in slots:
        demand[slot.meaning] = demand.get(slot.meaning, 0) + 1
        if slot.idea_id:
            assigned[slot.meaning] = assigned.get(slot.meaning, 0) + 1

    gaps: list[tuple[str, int, int, int]] = []
    for meaning, need in demand.items():
        got = assigned.get(meaning, 0)
        in_bank = bank_by_meaning.get(meaning, 0)
        if got < need:
            gaps.append((meaning, need, got, in_bank))

    if not gaps:
        return []

    gaps.sort(key=lambda g: g[2] - g[1])
    lines = []
    for meaning, need, got, in_bank in gaps[:5]:
        title = MEANING_BY_KEY[meaning].title if meaning in MEANING_BY_KEY else meaning
        lines.append(
            f"«{title}» — нужно {need}, поставлено {got}, в банке {in_bank}"
        )
    total_missing = sum(need - got for _, need, got, _ in gaps)
    return [
        Finding(
            code="bank_not_covering",
            severity=SEVERITY_HIGH if total_missing > 3 else SEVERITY_MEDIUM,
            title=f"Без идеи осталось {total_missing} единиц",
            message=(
                "; ".join(lines)
                + ". Пустой слот — это день, в который нечего снимать: "
                "либо дописать идеи в банк, либо сократить план."
            ),
            fix_days=tuple(
                sorted({s.day for s in slots if not s.idea_id and not s.is_published})[:5]
            ),
            affected=total_missing,
        )
    ]


def check_idea_reuse(slots: list[SlotView]) -> list[Finding]:
    """Одна идея занимает несколько слотов.

    В разобранном инструменте одну идею можно было поставить в пять дней
    подряд, и ни один счётчик этого не замечал.
    """
    seen: dict[str, list[date]] = {}
    for slot in slots:
        if slot.idea_id:
            seen.setdefault(slot.idea_id, []).append(slot.day)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    if not dupes:
        return []
    return [
        Finding(
            code="idea_reused",
            severity=SEVERITY_HIGH,
            title=f"Идей, стоящих дважды: {len(dupes)}",
            message=(
                "Одна и та же идея занимает несколько слотов. Аудитория "
                "считывает повтор как «ему нечего сказать» — ровно в тот "
                "момент, когда решается покупка."
            ),
            fix_days=tuple(sorted({d for days in dupes.values() for d in days[1:]})[:5]),
            affected=len(dupes),
        )
    ]


def check_readiness(readiness: dict[str, bool] | None) -> list[Finding]:
    """Вторая дорожка: готовность продукта, а не контента.

    Все восемь ошибок первоисточника — про контент. Ни одна не про то, что
    продавать будет нечего. Прогрев может отработать идеально, а платёжка
    окажется не подключена.
    """
    if readiness is None:
        return []
    labels = {
        "program": "программа собрана",
        "offer": "оффер и цена зафиксированы",
        "payments": "приём оплаты подключён",
        "access": "выдача доступов проверена",
        "support": "поддержка на время потока назначена",
    }
    missing = [labels[k] for k, ok in readiness.items() if not ok and k in labels]
    if not missing:
        return []
    return [
        Finding(
            code="product_not_ready",
            severity=SEVERITY_CRITICAL,
            title="Продукт не готов к дате продаж",
            message=(
                "Не закрыто: " + ", ".join(missing) + ". "
                "Прогрев может отработать идеально, а продавать будет нечего — "
                "и это дороже любой ошибки в контенте."
            ),
            affected=len(missing),
        )
    ]


def build_report(
    slots: list[SlotView],
    *,
    bank_by_meaning: dict[str, int] | None = None,
    readiness: dict[str, bool] | None = None,
    bank_total: int = 0,
) -> Report:
    """Прогнать все проверки и собрать отчёт."""
    findings: list[Finding] = []
    findings += check_missing_checkpoints(slots)
    findings += check_unused_triggers(slots)
    findings += check_stage_order(slots)
    findings += check_bridge(slots)
    findings += check_proof(slots)
    findings += check_bank_coverage(slots, bank_by_meaning or {})
    findings += check_idea_reuse(slots)
    findings += check_readiness(readiness)

    confirmed: set[str] = set()
    claimed: set[str] = set()
    triggers_used: set[str] = set()
    for slot in slots:
        claimed.update(slot.checkpoints)
        if slot.confirmed:
            confirmed.update(slot.checkpoints)
            triggers_used.update(slot.triggers)

    return Report(
        findings=findings,
        checkpoints_confirmed=len(confirmed & set(CHECKPOINT_BY_KEY)),
        checkpoints_claimed=len(claimed & set(CHECKPOINT_BY_KEY)),
        checkpoints_total=len(CHECKPOINTS),
        triggers_used=len(triggers_used & set(TRIGGER_BY_KEY)),
        triggers_total=len(TRIGGERS),
        slots_total=len(slots),
        slots_with_idea=sum(1 for s in slots if s.idea_id),
        bank_total=bank_total,
        bank_marked=sum((bank_by_meaning or {}).values()),
    )
