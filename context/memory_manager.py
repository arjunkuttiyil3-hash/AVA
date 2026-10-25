"""Assemble bounded conversation context from persistent memory."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from memory.repository import MemoryRepository, RowData

Summarizer = Callable[[str, list[RowData]], str]


@dataclass(frozen=True)
class ConversationContext:
    """Relevant state and history selected for one user message."""

    session_id: str
    preferences: RowData
    summary: str
    retrieved: list[RowData]
    recent: list[RowData]
    active_event: RowData | None
    active_story: RowData | None
    continues_story: bool
    continues_event: bool


class MemoryManager:
    """Coordinate short-term history, FTS retrieval, and persistent state."""

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        short_term_turns: int = 12,
        retrieval_limit: int = 5,
        token_budget: int = 4096,
        summarizer: Summarizer | None = None,
    ) -> None:
        if short_term_turns < 1 or retrieval_limit < 0 or token_budget < 128:
            raise ValueError("Memory limits must be positive and token budget at least 128.")
        self.repository = repository
        self.short_term_turns = short_term_turns
        self.retrieval_limit = retrieval_limit
        self.token_budget = token_budget
        self.summarizer = summarizer

    def resolve_followup(self, session_id: str, message: str) -> ConversationContext:
        """Load story or event state only when the user refers to it."""
        state = self.repository.get_active_state(session_id)
        recent = self.repository.get_recent_messages(
            session_id, limit=self.short_term_turns * 2
        )
        normalized = message.casefold().strip()
        story_cues = (
            "story", "continue", "next part", "what happens", "make it",
            "meet a", "meet the", "same character", "same story", "then",
        )
        event_cues = (
            "next item", "next event", "schedule", "current event", "announcement",
            "speaker", "event",
        )
        def matches(cues: tuple[str, ...]) -> bool:
            return any(
                re.search(
                    rf"(?<!\w){re.escape(cue)}(?!\w)",
                    normalized,
                    flags=re.IGNORECASE,
                )
                is not None
                for cue in cues
            )

        has_story = state["story"] is not None
        has_event = state["event"] is not None
        continues_story = has_story and matches(story_cues)
        continues_event = has_event and matches(event_cues)
        summary = self.repository.get_summary(session_id)
        retrieved = (
            self.repository.search_messages(session_id, message, self.retrieval_limit)
            if self.retrieval_limit
            else []
        )
        return ConversationContext(
            session_id=session_id,
            preferences=self.repository.get_preferences(session_id),
            summary=summary,
            retrieved=retrieved,
            recent=recent,
            active_event=state["event"] if continues_event else None,
            active_story=state["story"] if continues_story else None,
            continues_story=continues_story,
            continues_event=continues_event,
        )

    def build_prompt(
        self,
        context: ConversationContext,
        system_prompt: str,
        latest_message: str,
    ) -> list[dict[str, str]]:
        """Build a compact chat prompt within the configured approximate budget."""
        chunks: list[tuple[str, str]] = [
            ("system", system_prompt),
            ("system", f"User preferences: {context.preferences}"),
        ]
        if context.active_event:
            chunks.append(("system", f"Active event state: {context.active_event}"))
        if context.active_story:
            chunks.append(("system", f"Active story state: {context.active_story}"))
        if context.summary:
            chunks.append(("system", f"Conversation summary: {context.summary}"))
        for snippet in context.retrieved:
            chunks.append(
                ("system", f"Relevant older message ({snippet['role']}): {snippet['content']}")
            )
        chunks.extend(
            (message["role"], message["content"])
            for message in context.recent[-self.short_term_turns * 2 :]
        )
        chunks.append(("user", latest_message))

        budget_chars = self.token_budget * 4
        if len(latest_message) + 16 > budget_chars:
            raise ValueError("Latest message exceeds the configured prompt budget.")
        selected: list[tuple[str, str]] = []
        used_chars = 0
        for role, content in reversed(chunks):
            cost = len(content) + 16
            if selected and used_chars + cost > budget_chars:
                continue
            selected.append((role, content))
            used_chars += cost
        selected.reverse()
        if not selected or selected[-1] != ("user", latest_message):
            raise ValueError("Latest message does not fit the configured prompt budget.")
        return [{"role": role, "content": content} for role, content in selected]

    def compact_history(self, session_id: str) -> str:
        """Summarize older messages when history exceeds the configured budget."""
        summary = self.repository.get_summary(session_id)
        if (
            not self.summarizer
            or self.repository.get_message_character_count(session_id)
            <= self.token_budget * 4
        ):
            return summary

        cursor = self.repository.get_summary_cursor(session_id)
        recent = self.repository.get_recent_messages(
            session_id, limit=self.short_term_turns * 2
        )
        if not recent:
            return summary
        through_id = int(recent[0]["id"]) - 1
        old_messages = self.repository.get_messages_after(
            session_id, cursor, limit=40
        )
        old_messages = [item for item in old_messages if int(item["id"]) <= through_id]
        if not old_messages:
            return summary
        updated = self.summarizer(summary, old_messages)
        if not updated.strip():
            raise ValueError("Conversation summarizer returned an empty summary.")
        self.repository.set_summary(
            session_id, updated, int(old_messages[-1]["id"])
        )
        return updated


def estimate_tokens(text: str) -> int:
    """Estimate token use conservatively for text prompt budgeting."""
    return max(1, len(re.findall(r"\S+", text)) * 4 // 3)
