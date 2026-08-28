"""Модель запуска.

Новая сущность здесь ровно одна — сам запуск. Слот календаря это
`planned_posts` с проставленным `launch_id`, идея — `knowledge_items`.
Так сделано намеренно: в исходной системе банк идей и календарь были
двумя таблицами, и карточка идеи оказывалась разрезанной пополам —
провалившись в день календаря, исходную заметку было не увидеть. У нас
единица без даты это банк, единица с датой — календарь, и это одна и та
же запись на разных стадиях.
"""
import uuid
from datetime import date as Date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date as SaDate,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid_pk

#: Состояния запуска. Удаления нет: запуск архивируется, потому что вместе
#: с ним иначе исчезают опубликованные единицы и вся фактура для разбора
#: следующего потока.
LAUNCH_STATUSES = ("draft", "active", "paused", "done", "archived")

#: Плотность публикаций. Блогу на 500 подписчиков сорок единиц контента не
#: нужны — он на них выгорит и не дойдёт до продаж.
LAUNCH_INTENSITIES = ("light", "normal", "heavy")

#: Что должно быть готово, кроме контента. Прогрев может отработать
#: идеально, а продавать будет нечего.
READINESS_KEYS = ("program", "offer", "payments", "access", "support")


class Launch(Base, TimestampMixin):
    __tablename__ = "launches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','active','paused','done','archived')",
            name="ck_launches_status",
        ),
        CheckConstraint(
            "intensity IN ('light','normal','heavy','dense')",
            name="ck_launches_intensity",
        ),
        # Даты, которые противоречат друг другу, ломают всю раскладку —
        # ловим на уровне базы, а не только в форме.
        CheckConstraint(
            "sales_close IS NULL OR sales_close >= sales_open",
            name="ck_launches_sales_window",
        ),
        CheckConstraint(
            "key_event_date IS NULL OR key_event_date <= sales_open",
            name="ck_launches_key_event_before_sales",
        ),
        Index("idx_launches_org", "organization_id"),
        Index("idx_launches_status", "status"),
        Index("idx_launches_sales_open", "sales_open"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Единственная обязательная дата — открытие продаж. Всё остальное
    # разворачивается от неё назад.
    sales_open: Mapped[Date] = mapped_column(SaDate, nullable=False)
    sales_close: Mapped[Date | None] = mapped_column(SaDate, nullable=True)
    key_event_date: Mapped[Date | None] = mapped_column(SaDate, nullable=True)
    key_event_type: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: Номер потока. На первом запуске вбрасывают идею и собирают реакцию,
    #: на повторном анонсируют дату открытия — это разные сценарии.
    launch_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    intensity: Mapped[str] = mapped_column(String(20), default="normal", nullable=False)

    #: Часовой пояс запуска.
    #:
    #: Раскладка сейчас работает на календарных датах и на неё не влияет:
    #: день считается днём независимо от пояса, и разъехаться там нечему.
    #: Поле заведено под время публикации — как только у слота появится
    #: время, дедлайн-контент «за два часа до закрытия продаж» обязан
    #: считаться в поясе автора, иначе самый дорогой слот запуска выйдет
    #: после дедлайна. До этого момента поле только хранится.
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow", nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)

    #: Вторая дорожка: готовность продукта, а не контента.
    readiness: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    #: Переопределения длительностей этапов, {номер этапа: дни}.
    durations: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    #: Накопительная цель: сколько человек хотим собрать в лист ожидания к
    #: моменту открытия. Продажи собираются лестницей, а не одной кнопкой.
    waitlist_goal: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Рамка запуска: то, что автор держит в голове и обязан видеть на экране.
    price: Mapped[str | None] = mapped_column(String(100), nullable=True)
    audience: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Как собираем заявки — бот, ключевое слово, форма.
    collect: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Факт против цели. Обе пары нужны раздельно: до окна считаем заявки,
    #: внутри окна — оплаты, и это разные воронки.
    waitlist: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paid_goal: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: День, когда план развернули.
    #:
    #: Единственная часть статуса, которую нельзя вывести из дат: «черновик»
    #: отличается от «прогрев идёт» именно наличием плана. Остальные состояния
    #: (окно продаж, закрыт, в архиве) считаются из sales_open / sales_close /
    #: archived_at — хранить их копию значит гарантировать расхождение при
    #: первом же переносе окна.
    unrolled_on: Mapped[Date | None] = mapped_column(SaDate, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LaunchStoryLine(Base, TimestampMixin):
    """Сюжетная линия запуска.

    Механика из разбора реального запуска, которой нет ни в уроках, ни в
    инструменте: три-четыре линии заявляются авансом в начале и
    закрываются событием через недели. В разобранном кейсе линия «получу
    ключи от квартиры» была заявлена 6 января и закрыта 30-го — на ней
    держался весь месяц.
    """

    __tablename__ = "launch_story_lines"
    __table_args__ = (
        Index("idx_story_lines_launch", "launch_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    launch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("launches.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Чем линия закрывается — событие, которое должно случиться.
    payoff: Mapped[str | None] = mapped_column(Text, nullable=True)
    announced_on: Mapped[Date | None] = mapped_column(SaDate, nullable=True)
    closes_on: Mapped[Date | None] = mapped_column(SaDate, nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: Посты, которыми линия открывается и закрывается.
    #:
    #: Линия считается закрытой, только когда под раскрытие стоит конкретный
    #: слот: дата в календаре ничего не обещает аудитории, пост обещает.
    #: SET NULL при удалении слота — обещание остаётся, просто снова
    #: становится незакрытым.
    announce_slot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("planned_posts.id", ondelete="SET NULL"),
        nullable=True,
    )
    close_slot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("planned_posts.id", ondelete="SET NULL"),
        nullable=True,
    )


class LaunchEvidence(Base):
    """Фактура запуска: чем закрывается конкретный смысл покупателя.

    Одна строка на пару (запуск, смысл). Отдельная таблица, а не JSONB на
    запуске: главный вопрос к этим данным — «где у нас нет пруфов по третьему
    вопросу», и он должен отвечаться индексом, а не разбором документа на
    стороне приложения.

    Задачи на добычу здесь не хранятся. Задача — это ровно состояние `none`
    плюс дедлайн, посчитанный от дня, где смысл понадобится по плану. Держать
    её отдельной записью значит завести вторую копию того же факта и
    расхождение между ними. Персистится только то, что не вычисляется:
    `task_dismissed`.
    """

    __tablename__ = "launch_evidence"
    __table_args__ = (
        CheckConstraint(
            "state IN ('proof','claimed','none')", name="ck_launch_evidence_state"
        ),
        Index(
            "idx_launch_evidence_open",
            "launch_id",
            "state",
            postgresql_where=text("state <> 'proof'"),
        ),
    )

    launch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("launches.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: Ключ смысла из справочника методологии (сорок штук).
    meaning_key: Mapped[str] = mapped_column(String(50), primary_key=True)

    #: `proof` — есть чем показать · `claimed` — заявлено словами ·
    #: `none` — нечем доказать. В покрытие идёт только `proof`.
    state: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    proof_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    proof_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    task_dismissed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
