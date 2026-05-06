"""Skill registry — importing the modules registers them via @register(...)."""
from app.services.skills import (  # noqa: F401
    article_creator,
    carousel_creator,
    hooks_creator,
    instagram_creator,
    linkedin_creator,
    reels_script_writer,
    telegram_creator,
    transcribe_audio,
    transcribe_youtube,
    tweak,
    twitter_creator,
    viral_talking_points,
)
from app.services.skills.base import get, list_registered, register, skill_for_node

__all__ = ["get", "list_registered", "register", "skill_for_node"]
