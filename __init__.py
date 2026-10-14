"""alfred-brain: Alfred's second brain in an Obsidian vault (Hermes memory provider ``brain``)."""

from __future__ import annotations

from pathlib import Path

from .host import AUX_TASK
from .provider import BrainProvider

__version__ = "0.3.0"


def register(ctx) -> None:
    ctx.register_memory_provider(BrainProvider())
    skill = Path(__file__).parent / "skills" / "brain" / "SKILL.md"
    if skill.exists():
        ctx.register_skill("brain", skill)
    ctx.register_auxiliary_task(
        AUX_TASK, display_name="Secondo cervello (consolidamento)",
        description="Model that files conversations, recordings and voice memos into client, person and project notes.",
        inherit_from="compression")
