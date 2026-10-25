"""Output checks for spoken responses and language/script consistency."""

from __future__ import annotations

import re
from dataclasses import dataclass

MALAYALAM_RE = re.compile(r"[\u0D00-\u0D7F]")
LATIN_RE = re.compile(r"[A-Za-z]")
UNSAFE_RE = re.compile(
    r"\b(?:build|make|synthesize)\b.{0,30}\b(?:bomb|explosive|poison)\b"
    r"|\b(?:kill|hurt)\s+(?:yourself|someone)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ValidationResult:
    """Validation outcome with an explicit reason for rejected output."""

    valid: bool
    text: str
    reason: str = ""


class OutputValidator:
    """Reject empty, excessive, repetitive, unsafe, or script-mismatched speech."""

    def __init__(self, max_characters: int = 2400, max_words: int = 420) -> None:
        if max_characters < 40 or max_words < 5:
            raise ValueError("Spoken response limits are too small.")
        self.max_characters = max_characters
        self.max_words = max_words

    def validate(
        self, text: str, language: str, *, min_words: int = 1
    ) -> ValidationResult:
        """Validate generated text against response size, safety, and expected script."""
        normalized = text.strip()
        if not normalized:
            return ValidationResult(False, normalized, "empty")
        if len(normalized) > self.max_characters:
            return ValidationResult(False, normalized, "too_long")
        words = normalized.split()
        if len(words) < min_words or len(words) > self.max_words:
            return ValidationResult(False, normalized, "word_limit")
        if UNSAFE_RE.search(normalized):
            return ValidationResult(False, normalized, "unsafe")
        if self._is_repetitive(words):
            return ValidationResult(False, normalized, "repetitive")
        malayalam_characters = len(MALAYALAM_RE.findall(normalized))
        latin_characters = len(LATIN_RE.findall(normalized))
        if language == "ml" and malayalam_characters < max(2, latin_characters // 3):
            return ValidationResult(False, normalized, "wrong_language")
        if language == "en" and latin_characters < max(2, malayalam_characters // 3):
            return ValidationResult(False, normalized, "wrong_language")
        return ValidationResult(True, normalized)

    @staticmethod
    def _is_repetitive(words: list[str]) -> bool:
        if len(words) < 8:
            return False
        folded = [word.casefold().strip(".,!?;:") for word in words]
        if len(folded) >= 10 and len(set(folded)) <= max(3, len(folded) // 5):
            return True
        groups = [" ".join(folded[index : index + 4]) for index in range(len(folded) - 3)]
        return len(groups) != len(set(groups))
