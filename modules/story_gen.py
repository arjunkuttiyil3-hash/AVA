"""Continuity-aware story prompt and structured state extraction."""

from __future__ import annotations

import json
import re
from typing import Any

MALAYALAM_INSTRUCTION = (
    "സ്വാഭാവികമായി സംസാരിക്കുന്ന മലയാളത്തിൽ മലയാളം ലിപിയിൽ മാത്രം എഴുതുക. "
    "കഥയുടെ സംഭാഷണങ്ങളും വിവരണവും സ്വാഭാവികമാക്കുക."
)


def build_story_prompt(
    request: str,
    *,
    language: str,
    story: dict[str, Any] | None = None,
) -> str:
    """Build an initial or follow-up story request with saved continuity state."""
    language_instruction = (
        MALAYALAM_INSTRUCTION
        if language == "ml"
        else "Write in natural spoken English."
        if language == "en"
        else "Write in natural Malayalam using Malayalam script; preserve familiar names."
    )
    if story:
        continuity = (
            "Continue the same story. Keep the established characters, setting, and "
            "plot consistent. Begin immediately after the last paragraph; do not repeat it.\n"
            f"Title: {story.get('title', '')}\n"
            f"Characters: {story.get('characters_json', '[]')}\n"
            f"Setting: {story.get('setting', '')}\n"
            f"Plot so far: {story.get('plot_summary', '')}\n"
            f"Last paragraph (continuity reference only): {story.get('last_paragraph', '')}"
        )
    else:
        continuity = (
            "Create a coherent story with memorable characters, a clear setting, "
            "and a satisfying opening."
        )
    return (
        "You are AVA, a creative storyteller.\n"
        f"Language: {language_instruction}\n{continuity}\n"
        f"User request: {request}\n"
        "Write 2-5 short paragraphs of natural spoken prose. Do not add markdown or emojis."
    )


def update_story_state(
    previous: dict[str, Any] | None, paragraph: str, request: str
) -> dict[str, Any]:
    """Update deterministic continuity fields while retaining established state."""
    state = dict(previous or {})
    words = re.findall(r"[\w\u0D00-\u0D7F]+", paragraph)
    proper_names = [word for word in words if word[:1].isupper() and len(word) > 1]
    characters = state.get("characters_json", "[]")
    existing_names = json.loads(characters) if isinstance(characters, str) else characters
    if not isinstance(existing_names, list):
        raise ValueError("Saved story characters must be a JSON list.")
    for name in proper_names:
        if name not in existing_names:
            existing_names.append(name)
    paragraphs = [
        item.strip()
        for item in re.split(r"\n\s*\n", paragraph.strip())
        if item.strip()
    ]
    last_paragraph = paragraphs[-1] if paragraphs else paragraph.strip()
    state.update(
        {
            "title": state.get("title") or request[:80],
            "characters": existing_names,
            "setting": state.get("setting") or _infer_setting(request),
            "plot_summary": (
                f"{state.get('plot_summary', '')} {paragraph}".strip()[-1200:]
            ),
            "last_paragraph": last_paragraph,
            "status": "active",
        }
    )
    return state


def _infer_setting(text: str) -> str:
    """Extract a simple location cue when user-provided."""
    match = re.search(r"\b(?:in|at|near)\s+([A-Z][\w-]+(?:\s+[A-Z][\w-]+)*)", text)
    return match.group(1) if match else ""
