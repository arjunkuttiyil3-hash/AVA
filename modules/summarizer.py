"""Evidence-grounded speaker summarization with map-reduce for long transcripts."""

from __future__ import annotations

from collections.abc import Callable
import re
from typing import Any

from validation.output_validator import OutputValidator, ValidationResult


class SpeakerSummarizer:
    """Summarize only transcript facts, validating spoken length and language."""

    def __init__(
        self,
        router: Any,
        validator: OutputValidator | None = None,
        chunk_characters: int = 6000,
    ) -> None:
        self.router = router
        self.validator = validator or OutputValidator(max_characters=1200, max_words=100)
        self.chunk_characters = chunk_characters

    def summarize(
        self,
        transcript: str,
        *,
        language: str,
        speaker_name: str = "",
        map_reduce: Callable[[list[str]], str] | None = None,
    ) -> str:
        """Create a short factual summary or an explicit empty-transcript notice."""
        if len(transcript.split()) < 8:
            return self._unavailable(language)
        chunks = [
            transcript[index : index + self.chunk_characters]
            for index in range(0, len(transcript), self.chunk_characters)
        ]
        if len(chunks) > 1:
            partials = [self._generate(chunk, language, speaker_name, partial=True) for chunk in chunks]
            source = map_reduce(partials) if map_reduce else "\n".join(partials)
        else:
            source = transcript
        summary = self._generate(source, language, speaker_name, partial=False)
        validation = self._validate_summary(summary, language)
        if not validation.valid:
            corrective = self._generate(
                source,
                language,
                speaker_name,
                partial=False,
                correction_reason=validation.reason,
            )
            validation = self._validate_summary(corrective, language)
        return validation.text if validation.valid else self._unavailable(language)

    def _generate(
        self,
        source: str,
        language: str,
        speaker: str,
        *,
        partial: bool,
        correction_reason: str = "",
    ) -> str:
        language_label = "natural spoken Malayalam in Malayalam script" if language == "ml" else "natural spoken English"
        task = "Extract factual key points for a later combined summary." if partial else "Write exactly 2–3 short sentences summarizing the main points, thanks, announcements, and action items."
        correction = (
            f"The previous draft did not meet the output requirement ({correction_reason}). "
            "Correct the format using only the transcript below; do not reuse unsupported claims. "
            if correction_reason
            else ""
        )
        prompt = (
            "Summarize only the information explicitly present in the transcript. "
            "Do not invent details, interpretations, or quotes. "
            f"Write in {language_label}. Speaker: {speaker or 'not specified'}. "
            f"{correction}{task}\nTranscript:\n{source}"
        )
        chunks, _ = self.router.stream(
            "speaker_summary",
            language,
            [{"role": "user", "content": prompt}],
        )
        return "".join(chunks).strip()

    def _validate_summary(self, text: str, language: str) -> ValidationResult:
        validation = self.validator.validate(text, language, min_words=5)
        if not validation.valid:
            return validation
        if not 2 <= _sentence_count(validation.text) <= 3:
            return ValidationResult(False, validation.text, "sentence_count")
        return validation

    @staticmethod
    def _unavailable(language: str) -> str:
        if language == "ml":
            return "ട്രാൻസ്ക്രിപ്റ്റ് വളരെ ചുരുങ്ങിയതിനാൽ സംഗ്രഹം തയ്യാറാക്കാനായില്ല."
        return "The summary could not be generated."


def _sentence_count(text: str) -> int:
    """Count spoken sentences while ignoring common honorific abbreviations."""
    protected = re.sub(
        r"\b(Dr|Mr|Mrs|Ms|Prof|Sr|Jr)\.(?=\s)",
        r"\1<period>",
        text.strip(),
        flags=re.IGNORECASE,
    )
    return len(re.findall(r"[.!?।]+(?:[\"'”’)\]]*)", protected))
