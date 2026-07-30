"""Skill registry — importing the modules registers them via @register(...)."""
from app.services.skills import (  # noqa: F401
    article_creator,
    carousel_creator,
    content_summarizer,
    fetch_url_article,
    hooks_creator,
    instagram_creator,
    linkedin_creator,
    reels_script_writer,
    review_creator,
    story_arc_planner,
    telegram_creator,
    transcribe_audio,
    transcribe_youtube,
    tweak,
    twitter_creator,
    viral_talking_points,
)
from app.services.skills.base import get, list_registered, register, skill_for_node

__all__ = ["get", "list_registered", "register", "skill_for_node"]
