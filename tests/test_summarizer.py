"""Speaker-summary behavior for evidence-grounded short transcripts."""

from typing import Any

from modules.summarizer import SpeakerSummarizer


class SummaryRouter:
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def stream(
        self,
        task: str,
        language: str,
        messages: list[dict[str, str]],
    ) -> tuple[list[str], Any]:
        _ = (task, language)
        self.prompts.append(messages[-1]["content"])
        return [self.response], None


def test_summarizer_uses_transcript_and_returns_short_summary() -> None:
    router = SummaryRouter(
        "The speaker described a school garden project and its weekly student teams. "
        "She thanked the volunteers and asked each class to bring native seeds."
    )
    summarizer = SpeakerSummarizer(router)
    result = summarizer.summarize(
        "The school began a garden project. Student teams meet every week. "
        "The speaker thanked volunteers and asked each class to bring native seeds.",
        language="en",
        speaker_name="Anjali",
    )
    assert "garden project" in result
    assert "native seeds" in result
    assert "Do not invent" in router.prompts[0]


def test_empty_or_short_transcript_returns_explicit_notice() -> None:
    summarizer = SpeakerSummarizer(SummaryRouter("unreachable"))
    assert "could not be generated" in summarizer.summarize(
        "Thank you.", language="en"
    ).lower()
    assert "സംഗ്രഹം" in summarizer.summarize("നന്ദി.", language="ml")


def test_long_transcript_uses_map_reduce() -> None:
    router = SummaryRouter("The speaker explained the program and thanked students.")
    seen_partials: list[str] = []

    def combine(partials: list[str]) -> str:
        seen_partials.extend(partials)
        return " ".join(partials)

    summarizer = SpeakerSummarizer(router, chunk_characters=40)
    transcript = (
        "The speaker described student learning, field visits, and teamwork. "
        "Students will share project notes next month and thanked the teachers. "
    ) * 8
    summarizer.summarize(transcript, language="en", map_reduce=combine)
    assert len(seen_partials) > 1
    assert len(router.prompts) > 1
