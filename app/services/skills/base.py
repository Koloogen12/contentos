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

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Node

SkillFn = Callable[[AsyncSession, Node, str, dict[str, Any]], Awaitable[dict[str, Any]]]

_REGISTRY: dict[str, SkillFn] = {}


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


_FORMAT_PLATFORM_TO_SKILL = {
    "telegram": "telegram_creator",
    "linkedin": "linkedin_creator",
    "carousel": "carousel_creator",
    "reels": "reels_creator",
    "hooks": "hooks_creator",
}


def skill_for_node(node: Node) -> str:
    """Resolve which skill to run for a node based on its type + data."""
    if node.type == "extract":
        return "viral_talking_points"
    if node.type == "format":
        platform = (node.data or {}).get("platform", "telegram")
        skill = _FORMAT_PLATFORM_TO_SKILL.get(platform)
        if not skill:
            raise ValueError(f"Платформа {platform} пока не поддерживается")
        return skill
    raise ValueError(f"Cannot run a skill on node type {node.type}")


def list_registered() -> list[str]:
    return sorted(_REGISTRY.keys())
