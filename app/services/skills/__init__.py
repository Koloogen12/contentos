"""Skill registry — importing the modules registers them via @register(...)."""
from app.services.skills import (  # noqa: F401
    linkedin_creator,
    telegram_creator,
    transcribe_audio,
    transcribe_youtube,
    viral_talking_points,
)
from app.services.skills.base import get, list_registered, register, skill_for_node

__all__ = ["get", "list_registered", "register", "skill_for_node"]
