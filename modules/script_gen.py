"""Prompt construction and gap analysis for event anchoring scripts."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import llm

logger = logging.getLogger("ava.script_gen")

CLARIFICATION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "has_questions": {"type": "BOOLEAN"},
        "summary": {"type": "STRING"},
        "missing_aspects": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        },
        "questions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "field": {"type": "STRING"},
                    "question": {"type": "STRING"},
                    "placeholder": {"type": "STRING"},
                    "importance": {
                        "type": "STRING",
                        "enum": ["high", "medium", "optional"],
                    },
                },
                "required": ["id", "question", "placeholder"],
            },
        },
    },
    "required": ["has_questions", "summary", "questions"],
}


def analyze_event_gaps(
    event: dict[str, Any] | None = None,
    brief: dict[str, Any] | None = None,
    schedule: list[dict[str, Any]] | None = None,
    language: str = "en",
) -> dict[str, Any]:
    """Rule-based analyzer that detects missing or underspecified event details."""
    event_info = event or {}
    brief_info = brief or {}
    items = schedule or []
    is_ml = language == "ml"

    questions: list[dict[str, str]] = []
    missing_aspects: list[str] = []

    # 1. Chief guest details
    chief_guest = str(event_info.get("chief_guest") or "").strip()
    chief_guest_title = str(event_info.get("chief_guest_title") or "").strip()
    if chief_guest and not chief_guest_title:
        missing_aspects.append("Chief guest designation")
        questions.append({
            "id": "q_chief_guest_title",
            "field": "chief_guest_title",
            "question": (
                f"'{chief_guest}' എന്ന മുഖ്യാതിഥിയുടെ ഔദ്യോഗിക പദവിയും സ്ഥാപനവും എന്താണ്?"
                if is_ml
                else f"What is Chief Guest {chief_guest}'s official title, designation, or affiliation?"
            ),
            "placeholder": (
                "ഉദാ: പ്രിൻസിപ്പൽ / ഐഎസ്ആർഒ സയന്റിസ്റ്റ്"
                if is_ml
                else "e.g. Principal / Director of Research at XYZ Institute"
            ),
            "importance": "high",
        })

    # 2. Schedule items missing performers or speakers
    performance_keywords = (
        "dance", "song", "music", "speech", "talk", "presentation",
        "welcome", "vote of thanks", "skit", "drama", "address",
        "keynote", "felicitation", "വർത്തമാനം", "പ്രസംഗം", "നൃത്തം", "ഗാനം"
    )
    for index, item in enumerate(items):
        title = str(item.get("title") or "").strip()
        participant = str(
            item.get("participant") or item.get("person_name") or ""
        ).strip()
        if not title:
            continue
        title_lower = title.casefold()
        needs_performer = any(keyword in title_lower for keyword in performance_keywords)
        if needs_performer and not participant and len(questions) < 4:
            missing_aspects.append(f"Performer/Speaker for '{title}'")
            questions.append({
                "id": f"q_item_{index}_participant",
                "field": f"item_{index}_participant",
                "question": (
                    f"'{title}' എന്ന പരിപാടി അവതരിപ്പിക്കുന്നത് / സംസാരിക്കുന്നത് ആരാണ്?"
                    if is_ml
                    else f"Who will be presenting or delivering the '{title}' segment?"
                ),
                "placeholder": (
                    "ഉദാ: കോളേജ് ഡാൻസ് ക്ലബ് / അഞ്ജലി എസ്."
                    if is_ml
                    else "e.g. College Dance Troupe / Dr. Anjali S."
                ),
                "importance": "high",
            })

    # 3. Target Audience
    audience = str(brief_info.get("audience") or "").strip()
    if not audience and len(questions) < 4:
        missing_aspects.append("Audience profile")
        questions.append({
            "id": "q_audience",
            "field": "audience",
            "question": (
                "ഈ പരിപാടിയുടെ പ്രധാന സദസ്സ് ആരാണ്?"
                if is_ml
                else "Who is the primary audience for this event?"
            ),
            "placeholder": (
                "ഉദാ: കോളേജ് വിദ്യാർത്ഥികൾ, അധ്യാപകർ, വിശിഷ്ടാതിഥികൾ"
                if is_ml
                else "e.g. College students, faculty members, and alumni"
            ),
            "importance": "medium",
        })

    # 4. Event Theme / Special highlight
    topic = str(brief_info.get("topic") or "").strip()
    key_points = str(brief_info.get("key_points") or "").strip()
    if not topic and not key_points and len(questions) < 4:
        missing_aspects.append("Event theme / highlight")
        questions.append({
            "id": "q_topic",
            "field": "topic",
            "question": (
                "പരിപാടിയുടെ പ്രത്യേക പ്രമേയമോ പ്രധാന സന്ദേശമോ എന്താണ്?"
                if is_ml
                else "Is there a specific theme, slogan, or special milestone for this event?"
            ),
            "placeholder": (
                "ഉദാ: യുവത്വവും സാങ്കേതികവിദ്യയും"
                if is_ml
                else "e.g. Innovating for Tomorrow / Silver Jubilee celebration"
            ),
            "importance": "medium",
        })

    # 5. Venue or Timing if not provided
    venue = str(event_info.get("venue") or "").strip()
    start_time = str(event_info.get("event_start_time") or "").strip()
    if (not venue or not start_time) and len(questions) < 4:
        missing_aspects.append("Venue and timing")
        questions.append({
            "id": "q_venue_time",
            "field": "venue_time",
            "question": (
                "പരിപാടിയുടെ വേദിയും ആരംഭിക്കുന്ന സമയവും എവിടെയാണ്?"
                if is_ml
                else "Where will the event be held, and what is the starting time?"
            ),
            "placeholder": (
                "ഉദാ: മെയിൻ ഓഡിറ്റോറിയം, രാവിലെ 10:00 ന്"
                if is_ml
                else "e.g. Main Auditorium at 10:00 AM"
            ),
            "importance": "optional",
        })

    has_questions = len(questions) > 0
    if has_questions:
        summary = (
            f"നിങ്ങളുടെ വേദി കൂടുതൽ ആകർഷകമാക്കാൻ താഴെ പറയുന്ന {len(questions)} വിവരങ്ങൾ വ്യക്തമാക്കുമോ?"
            if is_ml
            else f"AVA identified {len(questions)} detail{'s' if len(questions) > 1 else ''} that will make your anchoring script significantly more stage-ready."
        )
    else:
        summary = (
            "പരിപാടിയുടെ വിവരങ്ങൾ പൂർണ്ണമാണ്! സ്ക്രിപ്റ്റ് നിർമ്മിക്കാൻ തയ്യാറാണ്."
            if is_ml
            else "Your event outline is comprehensive and stage-ready!"
        )

    return {
        "has_questions": has_questions,
        "summary": summary,
        "missing_aspects": missing_aspects,
        "questions": questions,
    }


def build_clarification_prompt(
    event: dict[str, Any] | None = None,
    brief: dict[str, Any] | None = None,
    schedule: list[dict[str, Any]] | None = None,
    language: str = "en",
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Build a prompt asking Gemini to review the event outline and formulate clarifying questions."""
    event_info = event or {}
    brief_info = brief or {}
    items = schedule or []
    is_ml = language == "ml"

    lang_instruction = (
        "Respond in Malayalam using Malayalam script."
        if is_ml
        else "Respond in natural English."
    )

    system_instruction = (
        "You are AVA, an expert stage anchor and live event coordinator. "
        "Your task is to review the organizer's event details, script brief, and schedule. "
        "Identify 2 to 4 vital missing details, ambiguities, or ceremonial elements that would "
        "significantly improve the spoken anchoring script (for example: missing performer/speaker names, "
        "chief guest designations or achievements, theme/motto, ceremony traditions like lamp lighting or prayer song, "
        "or specific announcements). "
        f"{lang_instruction} "
        "If the provided event details are already thoroughly specified and stage-ready, set 'has_questions' to false and return an empty questions list. "
        "Keep each question polite, specific, and easy for the organizer to answer quickly."
    )

    user_content = json.dumps(
        {
            "event_setup": event_info,
            "script_brief": brief_info,
            "schedule_items": items,
            "language": "ml" if is_ml else "en",
        },
        ensure_ascii=False,
        indent=2,
    )

    messages = [
        {"role": "system", "content": system_instruction},
        {
            "role": "user",
            "content": f"Review these event details and provide clarifying questions if needed:\n\n{user_content}",
        },
    ]

    return messages, CLARIFICATION_RESPONSE_SCHEMA


async def generate_clarification_questions(
    event: dict[str, Any] | None = None,
    brief: dict[str, Any] | None = None,
    schedule: list[dict[str, Any]] | None = None,
    language: str = "en",
) -> dict[str, Any]:
    """Generate intelligent clarifying questions using Gemini with robust rule-based fallback."""
    try:
        messages, schema = build_clarification_prompt(event, brief, schedule, language)
        response_text = await llm.llm_full(messages, response_schema=schema)
        parsed = json.loads(response_text)
        if isinstance(parsed, dict) and "has_questions" in parsed and "questions" in parsed:
            clean_questions = []
            for idx, q in enumerate(parsed.get("questions", [])):
                if isinstance(q, dict) and q.get("question"):
                    clean_questions.append({
                        "id": str(q.get("id") or f"q_{idx+1}"),
                        "field": str(q.get("field") or f"field_{idx+1}"),
                        "question": str(q.get("question")),
                        "placeholder": str(q.get("placeholder") or "Type your answer here..."),
                        "importance": str(q.get("importance") or "medium"),
                    })
            parsed["questions"] = clean_questions
            parsed["has_questions"] = bool(clean_questions)
            return parsed
    except Exception as exc:
        logger.warning(
            "Gemini clarification generation failed; falling back to rule-based gap analysis: %s",
            exc,
        )

    return analyze_event_gaps(event, brief, schedule, language)


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


SCRIPT_SEGMENT_TYPES = {"opening", "intro", "transition", "break", "closing"}


def build_structured_script_prompt(
    details: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    targets: list[dict[str, Any]] | None = None,
    existing_segments: list[dict[str, Any]] | None = None,
) -> str:
    """Build a fact-bound prompt for full or targeted anchoring script segments."""
    active_items = [
        item for item in items
        if item.get("status") not in {"absent", "skipped", "removed"}
    ]
    excluded_names = sorted({
        str(item.get("person_name") or "").strip()
        for item in items
        if item.get("status") in {"absent", "skipped", "removed"}
        and str(item.get("person_name") or "").strip()
    })
    safe_details = {key: value for key, value in details.items() if key != "items"}
    selected_targets = targets or [
        {"id": "opening", "type": "opening", "itemId": None},
        *[
            {
                "id": f"{'break' if item.get('type') == 'break' else 'intro'}-{item['id']}",
                "type": "break" if item.get("type") == "break" else "intro",
                "itemId": item["id"],
            }
            for item in active_items
        ],
        *[
            {
                "id": f"transition-{left['id']}-{right['id']}",
                "type": "transition",
                "itemId": right["id"],
            }
            for left, right in zip(active_items, active_items[1:])
        ],
        {"id": "closing", "type": "closing", "itemId": None},
    ]
    return (
        "Generate only the requested event anchoring script segments. Return strict JSON "
        "with a top-level segments array and no markdown. Each segment must contain "
        "id, type, itemId (number or null), text, and estimatedSeconds (integer).\n"
        "Allowed types: opening, intro, transition, break, closing. Preserve every requested "
        "segment id exactly. Never create segments for absent, skipped, or removed items, "
        f"and never mention these excluded people: {json.dumps(excluded_names, ensure_ascii=False)}. "
        "A target id beginning with recovery- means the participant returned late; include a "
        "brief recovery line before their introduction. Use only supplied facts; do not invent names, "
        "achievements, organizations, or event details. Transitions connect adjacent eligible "
        "items only. A restored late item should have a short recovery line when requested. "
        "Keep language natural for spoken delivery, match the selected tone, and estimate "
        "seconds using a realistic spoken pace.\n"
        f"Event details: {json.dumps(safe_details, ensure_ascii=False)}\n"
        f"Eligible running order: {json.dumps(active_items, ensure_ascii=False)}\n"
        f"Requested segments: {json.dumps(selected_targets, ensure_ascii=False)}\n"
        f"Existing script context (do not repeat or change unrequested content): "
        f"{json.dumps(existing_segments or [], ensure_ascii=False)}"
    )


def validate_script_segments(
    value: Any, expected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Validate Gemini's segment response against the requested ids and segment schema."""
    if isinstance(value, dict):
        value = value.get("segments")
    if not isinstance(value, list):
        raise ValueError("The script response must contain a segments list.")
    expected_by_id = {str(item["id"]): item for item in expected}
    if len(value) != len(expected_by_id):
        raise ValueError("The script response did not include every requested segment.")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for segment in value:
        if not isinstance(segment, dict):
            raise ValueError("Each script segment must be an object.")
        identifier = str(segment.get("id", "")).strip()
        expected_segment = expected_by_id.get(identifier)
        if not identifier or expected_segment is None or identifier in seen:
            raise ValueError("The script response contains an unexpected segment id.")
        kind = segment.get("type")
        if kind not in SCRIPT_SEGMENT_TYPES or kind != expected_segment.get("type"):
            raise ValueError("A script segment has an invalid type.")
        text = segment.get("text")
        seconds = segment.get("estimatedSeconds")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Every script segment needs non-empty text.")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
            raise ValueError("Estimated segment duration must be a non-negative integer.")
        expected_item = expected_segment.get("itemId")
        actual_item = segment.get("itemId")
        if actual_item != expected_item:
            raise ValueError("A script segment is linked to the wrong event item.")
        seen.add(identifier)
        validated.append({
            "id": identifier,
            "type": kind,
            "itemId": actual_item,
            "text": text.strip(),
            "estimatedSeconds": seconds,
            "edited": False,
        })
    return validated
