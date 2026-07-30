"""Skill registry and dispatch.

A skill is an async callable that receives:
    - db session (for reads — writes happen in the worker after we return)
    - the Node being processed
    - the system prompt context (already assembled)
    - the input dict assembled from upstream (see brand_context.collect_input_for_skill)

It returns a dict with two keys:
    - node_data: dict that REPLACES node.data on success
    - meta: optional dict logged on the SkillRun (input_snapshot is logged separately)
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node

SkillFn = Callable[[AsyncSession, Node, str, dict[str, Any]], Awaitable[dict[str, Any]]]

_REGISTRY: dict[str, SkillFn] = {}


# Default output-language directive — all user-facing copy (talking points,
# posts, hooks, reels) must come back in Russian regardless of the source
# material's language. This handles the common case where the founder pulls
# a transcript or article in English and expects RU output. Quoted names,
# numbers, brand mentions stay in the original. Future enhancement: per-run
# choice "keep original / translate" surfaced in the UI tweaks panel.
OUTPUT_LANGUAGE_DIRECTIVE = (
    "ЯЗЫК ВЫВОДА: всегда русский, даже если исходный материал на другом языке. "
    "Имена собственные, бренды, числа и прямые цитаты в кавычках сохраняй как в "
    "оригинале — не транслитерируй и не переводи. Если исходник англоязычный, "
    "перескажи смысл по-русски своими словами.\n"
    # Без этого запрета модель тянет из англоязычного источника обрывки в
    # середину русской фразы — «интеллект начинает shape atoms», «решил
    # looked on the bright side». Читается это как машинный перевод и рушит
    # голос автора сильнее, чем любая стилистическая ошибка.
    "ВНУТРИ ПРЕДЛОЖЕНИЯ ЯЗЫКИ НЕ СМЕШИВАЙ. Английские слова и обороты в "
    "русской фразе недопустимы: «начинает shape atoms», «решил looked on the "
    "bright side» — брак, даже если в источнике было так. Исключения ровно "
    "два: общепринятые термины без русского эквивалента и прямая цитата "
    "целиком в кавычках. Цитату из англоязычного источника либо приводи "
    "полностью и в кавычках, либо пересказывай по-русски — но не вставляй "
    "её обрывок в свою фразу.\n"
    # Ответ приходит как JSON, и прямая кавычка внутри значения его рвёт.
    # Пока разделы были короткими пунктами, цитат в них почти не было; со
    # связным текстом они появились, и разбор начал падать через раз с
    # невнятным «AI returned invalid JSON» — причина не видна ни из
    # текста ошибки, ни из логов.
    "КАВЫЧКИ: внутри текста используй только ёлочки — «вот так». Прямые "
    "кавычки (\") не ставь нигде: ответ передаётся как JSON, и они его "
    "ломают."
)


# Voice-rule block — appended to every format-skill system prompt to stop
# the LLM from breaking character as a chatbot offering services.
# Empirically the most common drift is in the final CTA / closing line:
# "если хочешь, могу собрать ещё карусель ...". Public-facing copy must
# read like the FOUNDER talking TO the audience, never like an AI helper
# talking TO the founder.
#
# Apply at the end of each skill's system prompt via `.format(voice_rule=...)`.
_VOICE_RULES = """\
КРИТИЧЕСКИ ВАЖНО — ГОЛОС:
- Ты пишешь ОТ ЛИЦА АВТОРА (founder'а) К АУДИТОРИИ. Текст читает подписчик \
  на Telegram / LinkedIn / Instagram / X — он НЕ общается с AI.
- Никаких chatbot-фраз: «если хочешь, могу собрать», «хочешь, я подготовлю», \
  «напиши мне, и я отправлю», «могу сделать ещё», «если нужно, я …». \
  Это рушит иллюзию автора и убивает доверие.
- CTA — это призыв АУДИТОРИИ к действию (сохранить, поделиться, написать \
  слово в комменты, ответить на вопрос). НЕ предложение от автора \
  «сделать ещё что-то». Это запрещено.
- Особенно следи в финальных строках / последних слайдах / последнем \
  абзаце — именно там LLM чаще всего скатывается в chatbot-style."""


# Приметы машинного текста. Отдельный блок от голосовых правил: тот держит
# автора в кадре, этот — убирает то, по чему текст опознают как сгенерированный.
#
# Зачем это в системе, а не в пользовательских табу: список табу у автора —
# это его личные запреты по точному совпадению фразы. Приметы ниже про
# конструкции и ритм, их совпадением не поймать, и они одинаковы для всех.
#
# Продукт обещает «твоим языком, а не языком ChatGPT», а ключевая метрика —
# доля правок ниже 15–20%. До этого блока промпты защищались только от
# chatbot-фраз, то есть от одной приметы из десяти.
#
# Метод: skills/redaktura, проход 7 «Следы нейросети» (по практикам
# Людмилы Сарычевой).
MACHINE_TELLS_BLOCK = """\
ПРИМЕТЫ МАШИННОГО ТЕКСТА — ИЗБЕГАТЬ:
- Антитезы на автопилоте: «не X, а Y», «не просто X, а Y», «Не X. Y», \
  «формально X, на деле Y». Максимум две на весь текст, и ни одна не \
  начинает абзац. Обороты «это про…» и «это не про…» запрещены совсем. \
  Вместо противопоставления — прямое утверждение с конкретикой.
- Псевдоглубина: цепочки коротких предложений-кивков («Коротко. Точно. \
  По делу.»), вопрос-сам-ответ, тройки прилагательных («быстро, удобно \
  и надёжно»), анонсы-указки («а самое интересное дальше», «и вот что \
  важно»), мораль в финале («в конечном итоге всё сводится к…»), \
  метафора-бантик в последнем абзаце.
- Дежурные заходы и связки: «в современном мире», «в эпоху цифровизации», \
  «сегодня как никогда», «стоит отметить», «более того», «важно понимать», \
  «давайте разберёмся».
- Хеджирование каскадом: «может», «способен», «во многом», «в определённой \
  степени», «как правило» в каждом предложении. Либо утверждай и отвечай \
  за слова, либо один раз честно скажи, что не знаешь.
- Псевдозабота не по адресу: «и это нормально», «вы не одиноки», \
  «ты справишься» — терапевтический тон там, где его не просили.
- Равномерность: одинаковая длина абзацев, одинаковая плотность, каждый \
  абзац закрыт мини-выводом. Живой текст дышит — где-то густо фактами, \
  где-то одна фраза.
- Живые частицы «же», «ведь», «вот», «-то» машина почти не ставит, \
  а человек ими дышит. Ставь там, где они естественны.

НЕ ЯВЛЯЕТСЯ приметой машины, не трогай: длинное тире, кавычки-ёлочки, \
буква «ё», отсутствие опечаток. Портить типографику ради «человечности» \
запрещено."""


# Единый блок, который подмешивают все форматные навыки через {voice_rule}.
# Склеен из двух, чтобы правила разошлись по девяти генераторам без правок
# в каждом из них.
VOICE_RULE_BLOCK = _VOICE_RULES + "\n\n" + MACHINE_TELLS_BLOCK


# Regex-based safety net. The system prompt above is the primary defence,
# but LLMs occasionally ignore voice rules under specific topical pressure
# (e.g. a tezis about "AI tools" puts the model in chatbot mode). These
# patterns nuke the most common AI-as-helper sentences in any generated
# string. Designed to be surgical: only matches phrases where the SPEAKER
# is the AI offering services, not legitimate audience-facing language.
_META_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # "если хочешь, могу собрать/подготовить/прислать/сделать ..."
        r"если\s+хочешь[,\s]+могу\b[^.!?]*[.!?]?",
        r"хочешь[,\s]+(?:я\s+)?собер[уё]\b[^.!?]*[.!?]?",
        r"хочешь[,\s]+(?:я\s+)?подготовлю?\b[^.!?]*[.!?]?",
        r"хочешь[,\s]+(?:я\s+)?пришлю?\b[^.!?]*[.!?]?",
        # "если нужно, я ..."
        r"если\s+нужно[,\s]+я\b[^.!?]*[.!?]?",
        # "напиши, и я ..." — common AI-style follow-up offer
        r"напиши[,\s]+и\s+я\b[^.!?]*[.!?]?",
        # "могу собрать/подготовить/сделать следующую ..."
        r"могу\s+собрать\b[^.!?]*[.!?]?",
        r"могу\s+подготовить\b[^.!?]*[.!?]?",
        r"могу\s+сделать\s+(?:еще|ещё|следующ\w+)\b[^.!?]*[.!?]?",
        # "если интересно, я расскажу/пришлю/покажу..."
        r"если\s+интересно[,\s]+я\b[^.!?]*[.!?]?",
        # "хотите, я ..." (формальный вариант)
        r"хотите[,\s]+я\b[^.!?]*[.!?]?",
    ]
]


# Счётчик примет машинного текста. Не правит и не режет — только считает.
#
# Резать нельзя: антитеза «не X, а Y» это осмысленное предложение, вырезав
# его, мы потеряем содержание. Поэтому промпт (MACHINE_TELLS_BLOCK) остаётся
# основной защитой, а это — измеритель: сколько примет просочилось.
#
# Зачем измерять: продукт обещает долю правок ниже 15–20%, и до сих пор у нас
# не было ни одного числа, по которому видно, насколько текст машинный.
# Счётчик пишется в meta прогона, так что качество генерации можно наблюдать
# по истории, а не по ощущениям.
_MACHINE_TELLS: dict[str, re.Pattern[str]] = {
    # «это про…» / «это не про…» — запрещены безусловно
    "eto_pro": re.compile(r"\bэто\s+(?:не\s+)?про\b", re.IGNORECASE),
    # «не X, а Y» и родня
    "antithesis": re.compile(
        r"\bне\s+просто\s+[^,.!?]{2,40},\s*а\b|\bне\s+[^,.!?]{2,40},\s*а\s+",
        re.IGNORECASE,
    ),
    # дежурные заходы и связки
    "filler_opener": re.compile(
        r"\b(?:в\s+современном\s+мире|в\s+эпоху\s+цифровизации|сегодня\s+как\s+никогда"
        r"|стоит\s+отметить|более\s+того|важно\s+понимать|давайте\s+разбер[её]мся)\b",
        re.IGNORECASE,
    ),
    # каскад страховок
    "hedging": re.compile(
        r"\b(?:во\s+многом|в\s+определ[её]нной\s+степени|как\s+правило|в\s+целом)\b",
        re.IGNORECASE,
    ),
    # терапевтический тон не по адресу
    "pseudo_care": re.compile(
        r"\b(?:и\s+это\s+нормально|вы\s+не\s+одиноки|ты\s+справишься)\b",
        re.IGNORECASE,
    ),
}


def count_machine_tells(text: str) -> dict[str, int]:
    """Сколько примет машинного текста в строке. Пустой словарь — чисто."""
    if not text:
        return {}
    found = {
        name: len(pat.findall(text)) for name, pat in _MACHINE_TELLS.items()
    }
    return {k: v for k, v in found.items() if v}


def strip_meta_offers(text: str) -> str:
    """Remove obvious AI-chatbot offers from generated body text.

    Surgical: matches sentences where the SPEAKER is the AI offering to
    "сделать ещё что-то" for the user. Doesn't touch legitimate
    audience-facing CTAs like "напиши в комментах кодовое слово".

    Returns the text with matched sentences excised. The caller is
    responsible for length validation (some bodies are short enough that
    losing one sentence makes the slide empty — that's by design, the
    chatbot-leak slide was bad anyway).
    """
    cleaned = text
    for p in _META_PATTERNS:
        cleaned = p.sub("", cleaned)
    # Tidy up: collapse double-spaces and double-newlines we may have left.
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n\s*\n", "\n\n", cleaned)
    return cleaned.strip()


def register(name: str) -> Callable[[SkillFn], SkillFn]:
    def deco(fn: SkillFn) -> SkillFn:
        if name in _REGISTRY:
            raise RuntimeError(f"Skill already registered: {name}")
        _REGISTRY[name] = fn
        return fn

    return deco


def get(name: str) -> SkillFn:
    fn = _REGISTRY.get(name)
    if fn is None:
        raise KeyError(f"Unknown skill: {name}")
    return fn


FORMAT_PLATFORM_TO_SKILL = {
    "telegram": "telegram_creator",
    "linkedin": "linkedin_creator",
    "carousel": "carousel_creator",
    "reels": "reels_creator",
    "hooks": "hooks_creator",
    "article": "article_creator",
    # Рецензия на весь материал, а не на один тезис — см. review_creator.
    "review": "review_creator",
    # Материал для vc.ru с требованиями модерации площадки.
    "vc": "vc_creator",
    "twitter": "twitter_creator",
    "instagram": "instagram_creator",
}


def skill_for_node(node: Node) -> str:
    """Resolve which skill to run for a node based on its type + data."""
    if node.type == "extract":
        # extract has three output modes:
        #   "talking_points" (default) — viral_talking_points
        #   "summary"                  — content_summarizer
        #   "story_arc"                — story_arc_planner
        # The user toggles between them in the UI; the choice is persisted
        # in `node.data.extract_mode`.
        mode = (node.data or {}).get("extract_mode", "talking_points")
        if mode == "summary":
            return "content_summarizer"
        if mode == "story_arc":
            return "story_arc_planner"
        return "viral_talking_points"
    if node.type == "format":
        platform = (node.data or {}).get("platform", "telegram")
        skill = FORMAT_PLATFORM_TO_SKILL.get(platform)
        if not skill:
            raise ValueError(f"Платформа {platform} пока не поддерживается")
        return skill
    raise ValueError(f"Cannot run a skill on node type {node.type}")


def list_registered() -> list[str]:
    return sorted(_REGISTRY.keys())
