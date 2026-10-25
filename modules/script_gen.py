"""Prompt construction for event anchoring scripts."""

from __future__ import annotations

from typing import Any


def detect_script_task(message: str) -> str:
    """Select a ceremonial script subtype from a clear user cue."""
    text = message.casefold()
    patterns = (
        (("vote of thanks", "thank everyone", "thank you speech"), "vote_of_thanks"),
        (("closing", "close the event", "farewell"), "closing"),
        (("introduce the speaker", "speaker introduction", "introduce our speaker"), "speaker_introduction"),
        (("transition", "move on", "next performance"), "transition"),
        (("announcement", "announce"), "announcement"),
        (("welcome", "greet the audience"), "welcome"),
    )
    for cues, script_task in patterns:
        if any(cue in text for cue in cues):
            return script_task
    return "anchoring_script"


def build_anchoring_prompt(
    task: str,
    language: str,
    *,
    event: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
    current: dict[str, Any] | None = None,
    next_item: dict[str, Any] | None = None,
    tone: str = "Professional",
    user_prompt: str = "",
) -> str:
    """Create fact-constrained anchoring instructions in the selected language."""
    event_data = event or {}
    if language == "ml":
        language_instruction = (
            "എല്ലാ മറുപടിയും സ്വാഭാവികമായി സംസാരിക്കുന്ന മലയാളത്തിൽ മലയാളം ലിപിയിൽ എഴുതുക. "
            "ഇംഗ്ലീഷ് അക്ഷരങ്ങൾ ഉപയോഗിക്കരുത്."
        )
    elif language == "mixed":
        language_instruction = (
            "Write natural spoken Malayalam using Malayalam script, keeping proper names "
            "and technical terms in the user's mixed style when necessary."
        )
    else:
        language_instruction = "Write natural, concise spoken English."

    task_instructions = {
        "welcome": "Welcome the audience and introduce the supplied event.",
        "speaker_introduction": "Introduce the supplied speaker using only known facts.",
        "transition": "Transition briefly from the previous to the current schedule item.",
        "announcement": "Deliver the supplied announcement clearly and warmly.",
        "vote_of_thanks": "Deliver a concise, respectful vote of thanks using supplied names.",
        "closing": "Close the event warmly without inventing event details.",
        "schedule_next": "Announce the next scheduled item and its participant, if supplied.",
        "anchoring_script": "Introduce the current schedule item and transition naturally.",
    }
    selected_task = task_instructions.get(task, task_instructions["anchoring_script"])
    details = {
        "event": event_data,
        "previous_item": previous,
        "current_item": current,
        "next_item": next_item,
        "tone": tone,
        "user_request": user_prompt,
    }
    return (
        "You are AVA, an AI virtual event anchor. Create spoken words only.\n"
        f"Task: {selected_task}\n"
        f"Language requirement: {language_instruction}\n"
        f"Provided event facts (use only these; do not invent names or achievements): {details}\n"
        "Keep the script to 2-4 short sentences, about 20-70 words. "
        "Do not use headings, markdown, emoji, stage directions, or unsupported facts."
    )
