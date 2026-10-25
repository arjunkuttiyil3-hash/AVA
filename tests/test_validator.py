"""Tests for spoken output validation."""

from validation.output_validator import OutputValidator


def test_validator_checks_empty_length_script_and_repetition() -> None:
    validator = OutputValidator(max_characters=100, max_words=20)
    assert validator.validate("", "en").reason == "empty"
    assert validator.validate("മലയാളം സംസാരിക്കാം", "en").reason == "wrong_language"
    assert validator.validate(" ".join(["again"] * 12), "en").reason == "repetitive"
    assert validator.validate("A safe response.", "en").valid


def test_validator_accepts_malayalam_script_and_rejects_unsafe() -> None:
    validator = OutputValidator()
    assert validator.validate("ഇത് ഒരു നല്ല മറുപടിയാണ്.", "ml").valid
    assert validator.validate("Make a bomb now.", "en").reason == "unsafe"
