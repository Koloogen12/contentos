"""Тесты подбора идей и черновой разметки."""
from __future__ import annotations

import pytest

from app.services.launch.matching import (
    IdeaView,
    assign_ideas,
    bank_histogram,
    draft_markup,
    guess_checkpoints,
    guess_meaning,
    guess_triggers,
)
from app.services.launch.validators import ORIGIN_RULE


def idea(id_, meaning=None, fmt="any", score=None, used_at=None, title="идея", body=""):
    return IdeaView(id=id_, title=title, body=body, meaning=meaning, fmt=fmt,
                    score=score, used_at=used_at)


# --------------------------------------------------------------------------
# Подбор
# --------------------------------------------------------------------------


def test_each_idea_used_once():
    ideas = [idea("a", "expertise"), idea("b", "expertise")]
    got = assign_ideas([("expertise", "stories")] * 3, ideas)
    used = [a.idea_id for a in got if a.idea_id]
    assert len(used) == len(set(used)), "идея не может занимать два слота"
    assert len(used) == 2


def test_empty_slot_has_human_readable_reason():
    got = assign_ideas([("hype", "stories")], [])
    assert got[0].idea_id is None
    assert "нет идей рубрики" in got[0].reason


def test_exhausted_bucket_says_so():
    got = assign_ideas([("expertise", "stories")] * 2, [idea("a", "expertise")])
    assert got[1].idea_id is None
    assert "закончились" in got[1].reason


def test_format_mismatch_is_explained():
    # Идея под длинный пост не должна попасть в сторис.
    got = assign_ideas([("expertise", "stories")], [idea("a", "expertise", fmt="post")])
    assert got[0].idea_id is None
    assert "не подходят под этот формат" in got[0].reason


def test_format_match_respected():
    got = assign_ideas([("expertise", "telegram")], [idea("a", "expertise", fmt="post")])
    assert got[0].idea_id == "a"


def test_higher_score_first():
    ideas = [idea("low", "expertise", score=1), idea("high", "expertise", score=9)]
    got = assign_ideas([("expertise", "stories")], ideas)
    assert got[0].idea_id == "high"


def test_unused_ideas_preferred_over_used():
    ideas = [
        idea("used", "expertise", score=9, used_at="2026-01-01"),
        idea("fresh", "expertise", score=1),
    ]
    got = assign_ideas([("expertise", "stories")], ideas)
    assert got[0].idea_id == "fresh", "свежая идея важнее высокой оценки"


def test_excluded_ideas_are_skipped():
    got = assign_ideas(
        [("expertise", "stories")],
        [idea("a", "expertise")],
        exclude_ids=frozenset({"a"}),
    )
    assert got[0].idea_id is None


def test_ideas_without_meaning_are_not_used():
    got = assign_ideas([("expertise", "stories")], [idea("a", None)])
    assert got[0].idea_id is None


def test_assignment_is_deterministic():
    ideas = [idea("a", "expertise"), idea("b", "expertise")]
    specs = [("expertise", "stories")] * 2
    assert assign_ideas(specs, ideas) == assign_ideas(specs, ideas)


def test_assignment_covers_every_slot():
    specs = [("expertise", "stories"), ("hype", "stories"), ("students", "reels")]
    got = assign_ideas(specs, [idea("a", "expertise")])
    assert len(got) == len(specs)
    assert [a.slot_index for a in got] == [0, 1, 2]


# --------------------------------------------------------------------------
# Черновая разметка
# --------------------------------------------------------------------------


def test_guess_meaning_returns_none_when_unsure():
    assert guess_meaning("зззз") is None


def test_guess_meaning_finds_students_topic():
    assert guess_meaning("разбор кейса ученика с выпускного") is not None


def test_guess_checkpoints_detects_bridge_words():
    got = guess_checkpoints("рассказываю про свой провал, было тяжело")
    assert "q3_bridge" in got


def test_guess_triggers_detects_scarcity():
    got = guess_triggers("осталось 5 мест, дедлайн завтра")
    assert "scarcity" in got


def test_draft_markup_is_always_rule_origin():
    draft = draft_markup(idea("a", title="осталось 5 мест"))
    assert draft.origin == ORIGIN_RULE, "черновик не может выдавать себя за факт"


def test_draft_markup_keeps_existing_meaning():
    draft = draft_markup(idea("a", meaning="hype", title="что-то про учеников"))
    assert draft.meaning == "hype", "явная разметка человека не перетирается"


# --------------------------------------------------------------------------
# Гистограмма банка
# --------------------------------------------------------------------------


def test_bank_histogram_counts_by_meaning():
    got = bank_histogram([idea("a", "hype"), idea("b", "hype"), idea("c", "sales")])
    assert got == {"hype": 2, "sales": 1}


def test_bank_histogram_ignores_unmarked():
    assert bank_histogram([idea("a", None)]) == {}
